# -*- coding: utf-8 -*-
"""auto_apology — رد الاعتذار التلقائي عن تأخر الطبيب.

عند وصول رسالة من مريض عبر تيليجرام تبقى بلا رد من الطبيب للمدة المضبوطة
(الافتراضي 30 دقيقة) ولم يقرأها الطبيب بعد، يرسل الوكيل الذكي للمريض رسالة
اعتذار مهذبة (يولّدها LLM المجاني المضبوط في المشروع) مع التنصيص بوضوح أنها
من وكيل ذكاء اصطناعي وأن الطبيب سيجيب بنفسه عند قراءة الرسالة.

تُرسل مرة واحدة فقط لكل رسالة (عمود ai_apologized في patient_messages).
التفعيل والمدة من الإعدادات:
  ai_apology_enabled        (افتراضي 1 = مفعّل)
  ai_apology_delay_minutes  (افتراضي 30)
"""
from datetime import datetime, timedelta

import ai_client

APOLOGY_PROMPT = (
    'أنت وكيل ذكاء اصطناعي في عيادة نفسانية. المريض أرسل رسالة عبر تيليجرام '
    'وما زال الطبيب مشغولاً ولم يرد بعد. اكتب رسالة اعتذار قصيرة جداً بالعربية '
    '(سطران كحد أقصى، بدون تنسيق HTML): اعتذر بلطف عن تأخر الرد لأن الطبيب '
    'مشغول، وأكد له أن الطبيب سيجيب بنفسه في أقرب وقت عند قراءة رسالته، '
    'ووضّح أن هذه الرسالة تلقائية من الوكيل الذكي للعيادة. لا تقدم أي '
    'استشارة أو معلومة طبية إطلاقاً.'
)

FALLBACK_APOLOGY = (
    'عذراً منك، الطبيب مشغول حالياً وسيرد على رسالتك بنفسه في أقرب وقت عند '
    'قراءتها 🙏\n\n<i>— هذه رسالة تلقائية من الوكيل الذكي للعيادة</i>'
)


def _generate_apology(patient_name):
    """توليد نص الاعتذار عبر LLM المجاني المضبوط، مع نص احتياطي عند الفشل."""
    try:
        if not ai_client.is_configured():
            return FALLBACK_APOLOGY
        result = ai_client.chat_completion(
            [{'role': 'system', 'content': APOLOGY_PROMPT},
             {'role': 'user',
              'content': f'اسم المريض: {patient_name}'}],
            temperature=0.5)
        text = (result.get('content') or '').strip()
        return text[:500] if text else FALLBACK_APOLOGY
    except Exception as e:
        print(f'[auto_apology] فشل توليد الاعتذار، استخدام النص الاحتياطي: {e}')
        return FALLBACK_APOLOGY


def check_and_send_apologies():
    """فحص الرسائل غير المجاب عنها وإرسال اعتذار واحد لكل رسالة متأخرة.

    تُستدعى من APScheduler في خيط خلفي خارج سياق Flask — لذا تغلّف نفسها
    بـ app.app_context() مثل بقية المهام الدورية في app.py.
    """
    from models import db, PatientMessage
    from app import app, get_setting, telegram_send_message, log_action

    with app.app_context():
        _check_and_send_apologies_impl(db, PatientMessage, get_setting,
                                       telegram_send_message, log_action)


def _check_and_send_apologies_impl(db, PatientMessage, get_setting,
                                   telegram_send_message, log_action):
    enabled = (get_setting('ai_apology_enabled', '1') or '1').strip()
    if enabled != '1':
        return
    try:
        delay = int((get_setting('ai_apology_delay_minutes', '30') or '30'))
    except ValueError:
        delay = 30
    if delay <= 0:
        return
    cutoff = datetime.utcnow() - timedelta(minutes=delay)

    # آخر رسالة لكل مريض في تيليجرام: أرسل فقط إذا كانت من المريض، غير مقروءة،
    # متأخرة عن المدة المضبوطة، ولم يُرسل لها اعتذار من قبل.
    from sqlalchemy import func
    subq = (db.session.query(func.max(PatientMessage.id))
            .join(PatientMessage.patient)
            .filter(PatientMessage.channel == 'telegram')
            .group_by(PatientMessage.patient_id).subquery())
    msgs = (PatientMessage.query
            .filter(PatientMessage.id.in_(db.session.query(subq)),
                    PatientMessage.sender == 'patient',
                    PatientMessage.is_read.is_(False),
                    PatientMessage.ai_apologized.is_(False),
                    PatientMessage.created_at < cutoff)
            .all())
    sent = 0
    for m in msgs:
        patient = m.patient
        if not patient or not (patient.telegram_chat_id or '').strip():
            m.ai_apologized = True  # لا قناة إرسال — لا تعِد الفحص أبداً
            continue
        apology = _generate_apology(patient.full_name)
        ok, _err = telegram_send_message(patient.telegram_chat_id, apology)
        m.ai_apologized = True
        if ok:
            sent += 1
            log_action('create', 'message', patient.id,
                       'اعتذار تلقائي من الوكيل الذكي عن تأخر الرد')
    if msgs:
        db.session.commit()
        if sent:
            print(f'[auto_apology] أُرسل {sent} اعتذار/اعتذارات تلقائية')
