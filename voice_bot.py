# -*- coding: utf-8 -*-
"""voice_bot — الرسائل الصوتية للطبيب عبر تيليجرام.
المسار الكامل (كل المكونات مجانية):
رسالة صوتية من الطبيب في تيليجرام
→ تنزيل ملف OGG عبر getFile
→ تحويل صوت إلى نص عبر speech_to_text في ai_client (Groq Whisper مجاني)
→ تمرير النص لمسار رسائل الطبيب الموجود في app.py (أوامر/مساعد ذكي)
→ إرسال رد المساعد كـ ملاحظة صوتية (Edge TTS مجاني بصوت عصبي طبيعي،
مع تراجع تلقائي إلى gTTS) + النص معه.
الرسائل الصوتية من غير الطبيب تحصل على رد مهذب بأن الخدمة الصوتية للطبيب فقط.
"""
import base64
import io
import urllib.request

# نفس الصوت الافتراضي المستخدم في /api/assistant/voice/tts في app.py
DEFAULT_TTS_VOICE = 'ar-SA-HamedNeural'
TELEGRAM_API_BASE = 'https://api.telegram.org/bot'
# أقصى مدة رسالة صوتية مقبولة — حماية من استهلاك الذاكرة وحصة Groq المجانية
# ووقت حجب المعالجة (تيليجرام يرسل duration بالثواني ضمن بيانات الرسالة)
MAX_VOICE_DURATION_SECONDS = 60

# كل منطق تنظيف النص (رموز Markdown، تكرار مفرط، تحويل الوقت للنطق) موحَّد
# الآن في text_utils.py بدل تكراره هنا — استورده مباشرة لتفادي أي تعارض
# مستقبلي بين نسخ مختلفة من نفس المنطق (كما حدث سابقاً).
from text_utils import clean_text_for_speech as _clean_text_for_tts

def tts_to_mp3(text, voice=None):
    """تحويل نص إلى mp3: Edge TTS أولاً (صوت طبيعي مجاني) ثم gTTS احتياطاً.
    يعيد bytes للملف الصوتي أو None عند فشل الطريقتين.
    """
    # تطبيق التنظيف فوراً كخطوة أولى
    text = _clean_text_for_tts(text)
    
    text = (text or '').strip()
    if not text:
        return None
    text = text[:800]
    voice = voice or DEFAULT_TTS_VOICE
    # المحاولة 1: Edge TTS
    try:
        import asyncio
        import edge_tts
        async def _gen():
            communicate = edge_tts.Communicate(text, voice=voice)
            audio = b''
            async for chunk in communicate.stream():
                if chunk.get('type') == 'audio':
                    audio += chunk.get('data', b'')
            return audio
        audio = asyncio.run(_gen())
        if audio:
            return audio
    except Exception as e:
        print(f'[voice_bot] فشل Edge TTS، التراجع إلى gTTS: {e}')
    # المحاولة 2: gTTS
    try:
        from gtts import gTTS
        buf = io.BytesIO()
        gTTS(text=text, lang='ar').write_to_fp(buf)
        return buf.getvalue()
    except Exception as e:
        print(f'[voice_bot] فشل gTTS أيضاً: {e}')
        return None

def send_telegram_voice(chat_id, audio_bytes, caption=None):
    """إرسال ملاحظة صوتية (voice note) عبر sendVoice برفع multipart.
    يعيد (نجاح: bool، رسالة خطأ: str|None).
    """
    import app as clinic_app
    token = clinic_app.telegram_bot_token()
    if not token:
        return False, 'لا يوجد توكن بوت تيليجرام'
    boundary = 'ClinicVoiceBoundary7MA4YWxkTrZu0gW'
    parts = []
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; '
                 f'name="chat_id"\r\n\r\n{chat_id}\r\n'.encode())
    if caption:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; '
                     f'name="caption"\r\n\r\n{caption}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; '
                     f'name="parse_mode"\r\n\r\nHTML\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; '
                 f'name="voice"; filename="reply.ogg"\r\n'
                 f'Content-Type: audio/ogg\r\n\r\n'.encode())
    parts.append(audio_bytes)
    parts.append(f'\r\n--{boundary}--\r\n'.encode())
    req = urllib.request.Request(
        f'{TELEGRAM_API_BASE}{token}/sendVoice',
        data=b''.join(parts), method='POST',
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}',
                 'User-Agent': 'Mozilla/5.0 (compatible; ClinicApp/1.0)'})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = __import__('json').loads(resp.read().decode('utf-8'))
            if result.get('ok'):
                return True, None
            return False, result.get('description', 'خطأ غير معروف')
    except Exception as e:
        return False, str(e)

def _download_telegram_file(file_id):
    """تنزيل ملف من خوادم تيليجرام عبر getFile — يعيد bytes أو None."""
    import json
    import app as clinic_app
    token = clinic_app.telegram_bot_token()
    if not token:
        return None
    try:
        with urllib.request.urlopen(
            f'{TELEGRAM_API_BASE}{token}/getFile?file_id={file_id}',
            timeout=30) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            file_path = (result.get('result') or {}).get('file_path')
            if not file_path:
                return None
        with urllib.request.urlopen(
            f'https://api.telegram.org/file/bot{token}/{file_path}',
            timeout=60) as resp:
            return resp.read()
    except Exception as e:
        print(f'[voice_bot] فشل تنزيل الملف الصوتي: {e}')
        return None

def _stt_from_bytes(audio_bytes):
    """تحويل bytes صوتية إلى نص عبر Groq Whisper (ai_client.speech_to_text)."""
    import ai_client
    if not ai_client.is_configured():
        return None, 'لم يُضبط أي مفتاح AI (GROQ_API_KEY)'
    try:
        text = ai_client.speech_to_text(
            base64.b64encode(audio_bytes).decode('ascii'), language='ar')
        text = (text or '').strip()
        if not text:
            return None, 'لم يتعرّف Whisper على أي كلام في الرسالة'
        return text, None
    except ai_client.AIError as e:
        return None, str(e)

def _reply_processing(chat_id):
    """إرسال إشعار فوري بأن المعالجة جارية (قبل خيط المعالجة الثقيل)"""
    import app as clinic_app
    clinic_app.telegram_api_call('sendChatAction',
                                 {'chat_id': chat_id, 'action': 'typing'})

def _process_voice_payload(msg, chat_id, first_name):
    """المعالجة الفعلية للرسالة الصوتية (تُنفَّذ في خيط منفصل — عمليات شبكة متتالية:
    تنزيل OGG → Groq STT → معالجة الأمر → Edge TTS → رفع الصوت).

    الصوت متاح لطرفين فقط:
      1) الطبيب — دائماً، عبر مساعده الكامل (_handle_doctor_message).
      2) المريض — فقط أثناء جلسة نشطة مع «🤖 مساعد العيادة» المقيَّد
         (نفس الأداتين المتاحتين له كتابةً: استعلام/تأجيل)، عبر
         _handle_patient_ai_message. خارج هاتين الحالتين تُرفض الرسالة
         الصوتية بلطف، بما في ذلك أي مريض غير مرتبط بحساب أصلاً.
    """
    import app as clinic_app
    voice_obj = msg.get('voice') or msg.get('audio') or {}
    file_id = voice_obj.get('file_id')
    if not file_id:
        return

    is_doctor = chat_id == clinic_app.doctor_telegram_chat_id()
    patient = None
    if not is_doctor:
        patient = clinic_app.Patient.query.filter_by(telegram_chat_id=chat_id).first()
        if not patient or not clinic_app._patient_in_ai_session(chat_id):
            clinic_app.telegram_send_message(
                chat_id,
                '🎙️ عذراً، الخدمة الصوتية متاحة للطبيب دوماً، '
                'وللمريض فقط أثناء محادثة نشطة مع «🤖 مساعد العيادة».\n\n'
                'اضغط «🤖 مساعد العيادة» من القائمة الرئيسية أولاً، '
                'ثم أرسل رسالتك الصوتية.')
            return

    # تنزيل الصوت وتحويله إلى نص
    _reply_processing(chat_id)
    audio_bytes = _download_telegram_file(file_id)
    menu_buttons = clinic_app._doctor_menu_buttons() if is_doctor else None
    if not audio_bytes:
        clinic_app.telegram_send_message(
            chat_id, '⚠️ تعذّر تنزيل الرسالة الصوتية. حاول مجدداً.',
            buttons=menu_buttons)
        return
    text, err = _stt_from_bytes(audio_bytes)
    if err:
        clinic_app.telegram_send_message(
            chat_id, f'⚠️ تعذّر تحويل الصوت إلى نص: {err}\n'
                     f'يمكنك إرسال الأمر كتابةً.',
            buttons=menu_buttons)
        return

    if is_doctor:
        # تمرير النص لمسار رسائل الطبيب الموجود (أوامر، تعديل موعد، أو مساعد ذكي)
        # مع تفعيل الرد الصوتي: رد المساعد يصل أيضاً كملاحظة صوتية.
        clinic_app._handle_doctor_message(chat_id, text, first_name,
                                          voice_reply=True)
    else:
        # نفس مسار رسائل المريض المقيَّد (استعلام/تأجيل فقط)، مع تفعيل
        # الرد الصوتي أيضاً — بنفس صلاحياته الضيقة تماماً بغض النظر عمّا
        # يطلبه صوتياً (patient_assistant.py يرفض أي شيء خارج نطاقه).
        clinic_app._handle_patient_ai_message(chat_id, patient, text,
                                              voice_reply=True)

def handle_voice_message(msg):
    """معالجة رسالة صوتية واردة (voice أو audio) من process_telegram_update.
    المدقق السريع (المدة، chat_id) ينفَّذ فوراً حتى لا يحجب الـ Webhook،
    والمعالجة الثقيلة (شبكة + STT + TTS) تُنقل لخيط منفرد مع سياق التطبيق.
    """
    import threading
    import app as clinic_app
    chat_id = str((msg.get('chat') or {}).get('id') or '')
    first_name = ((msg.get('from') or {}).get('first_name')) or ''
    voice_obj = msg.get('voice') or msg.get('audio') or {}
    if not chat_id or not voice_obj.get('file_id'):
        return
    # رفض المدة الطويلة فوراً دون أي شبكة إضافية
    duration = voice_obj.get('duration') or 0
    if duration > MAX_VOICE_DURATION_SECONDS:
        clinic_app.telegram_send_message(
            chat_id,
            f'⚠️ الرسالة الصوتية طويلة جداً ({duration} ثانية). '
            f'حاول تسجيل رسالة أقصر (أقل من {MAX_VOICE_DURATION_SECONDS // 60} دقيقة).')
        return
    # المعالجة الثقيلة في خيط منفرد — لا تحجب استجابة الـ Webhook
    threading.Thread(
        target=_run_with_context, args=(msg, chat_id, first_name),
        daemon=True).start()

def _run_with_context(msg, chat_id, first_name):
    """تشغيل المعالجة داخل سياق تطبيق Flask (مطلوب لقاعدة البيانات والإعدادات)"""
    import app as clinic_app
    with clinic_app.app.app_context():
        try:
            _process_voice_payload(msg, chat_id, first_name)
        except Exception as e:
            print(f'[voice_bot] خطأ في معالجة الرسالة الصوتية: {e}')

def reply_with_voice(chat_id, text):
    """إرسال رد المساعد صوتياً + نصياً (يُستدعى من _handle_doctor_assistant)."""
    import app as clinic_app
    audio = tts_to_mp3(text)
    if not audio:
        return False
    ok, err = send_telegram_voice(chat_id, audio)
    if not ok:
        print(f'[voice_bot] فشل إرسال الصوت: {err}')
    return ok