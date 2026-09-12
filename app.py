"""التطبيق الرئيسي لإدارة عيادة أخصائي نفسي"""
import json
import os
import secrets
import threading
import time as _time
import urllib.error
import urllib.request
from datetime import datetime, date, timedelta, time as dtime
from decimal import Decimal, InvalidOperation
from functools import wraps

# تحميل متغيرات البيئة من ملف .env (إن وُجد) — يُحمّل مفاتيح AI للمساعد الذكي
# محاولات متعددة لضمان التحميل حتى لو لم يُثبت python-dotenv
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    load_dotenv(_env_path)
except ImportError:
    # python-dotenv غير مثبت — اقرأ .env يدوياً
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(_env_path):
        with open(_env_path, 'r', encoding='utf-8') as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith('#') or '=' not in _line:
                    continue
                _k, _, _v = _line.partition('=')
                _k = _k.strip()
                _v = _v.strip().strip('"').strip("'")
                if _k and _k not in os.environ:
                    os.environ[_k] = _v
except Exception as _e:
    print(f'تحذير: تعذّر تحميل .env: {_e}')

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, send_from_directory, abort, session,
                   has_request_context, Response)
from sqlalchemy import text
from werkzeug.utils import secure_filename
from models import (db, User, Patient, Appointment, Visit, MedicalImage,
                    Invoice, Setting, AuditLog, NotificationLog, DoctorAlert,
                    PatientMessage, Holiday, AppointmentMoveLog, utcnow)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'pdf', 'doc', 'docx', 'mp3', 'mp4'}
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 ميجابايت


def load_secret_key():
    """المفتاح من متغير البيئة، أو ملف محلي يُنشأ مرة واحدة"""
    env_key = os.environ.get('CLINIC_SECRET_KEY')
    if env_key:
        return env_key
    key_file = os.path.join(BASE_DIR, '.secret_key')
    if os.path.exists(key_file):
        with open(key_file) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(key_file, 'w') as f:
        f.write(key)
    return key


app = Flask(__name__)
app.config['SECRET_KEY'] = load_secret_key()
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///clinic.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE
app.config['DEBUG'] = os.environ.get('FLASK_DEBUG') == '1'
app.url_map.strict_slashes = False

db.init_app(app)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

APPOINTMENT_STATUSES = {
    'pending': 'بانتظار التأكيد',
    'scheduled': 'مجدول',
    'completed': 'مكتمل',
    'cancelled': 'ملغي',
    'no_show': 'لم يحضر',
}
WEEKDAY_NAMES = {0: 'الاثنين', 1: 'الثلاثاء', 2: 'الأربعاء', 3: 'الخميس',
                 4: 'الجمعة', 5: 'السبت', 6: 'الأحد'}


# ===================== أدوات مساعدة =====================

def to_decimal(value, default=Decimal('0')):
    try:
        return Decimal(str(value)).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError, TypeError):
        return default


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_setting(key, default=''):
    s = Setting.query.filter_by(key=key).first()
    return s.value if s and s.value is not None else default


def set_setting(key, value):
    s = Setting.query.filter_by(key=key).first()
    if s:
        s.value = value
    else:
        db.session.add(Setting(key=key, value=value))


def get_schedule():
    """جدول عمل العيادة من الإعدادات"""
    closed = set()
    for part in get_setting('closed_days', '4').split(','):
        part = part.strip()
        if part.isdigit() and int(part) in WEEKDAY_NAMES:
            closed.add(int(part))
    return {
        'work_start': get_setting('work_start', '09:00'),
        'work_end': get_setting('work_end', '17:00'),
        'slot_minutes': int(get_setting('slot_minutes', '60') or 60),
        'max_per_day': int(get_setting('max_per_day', '8') or 8),
        'closed_days': closed,
        'default_transport_fee': to_decimal(get_setting('transport_fee', '0')),
    }


def parse_hhmm(value, fallback='09:00'):
    try:
        hour, minute = value.strip().split(':')
        return dtime(int(hour), int(minute))
    except (ValueError, AttributeError):
        hour, minute = fallback.split(':')
        return dtime(int(hour), int(minute))


def available_slots(d):
    """الفترات المتاحة في يوم معين وفق جدول العمل والحجوزات القائمة"""
    sched = get_schedule()
    if d < date.today() or d.weekday() in sched['closed_days']:
        return []
    start_dt = datetime.combine(d, parse_hhmm(sched['work_start']))
    end_dt = datetime.combine(d, parse_hhmm(sched['work_end'], '17:00'))
    step = timedelta(minutes=max(15, sched['slot_minutes']))
    booked = {a.time for a in Appointment.query.filter(
        Appointment.date == d,
        Appointment.status.in_(['pending', 'scheduled'])).all()}
    if len(booked) >= sched['max_per_day']:
        return []
    slots, current = [], start_dt
    while current + step <= end_dt:
        t = current.time()
        if t not in booked:
            slots.append(t)
        current += step
    remaining = sched['max_per_day'] - len(booked)
    return slots[:remaining]


def has_time_conflict(d, t, duration, exclude_id=None):
    """هل يتداخل الموعد مع موعد آخر قائم في نفس اليوم؟"""
    start = datetime.combine(d, t)
    end = start + timedelta(minutes=duration)
    query = Appointment.query.filter(
        Appointment.date == d,
        Appointment.status.in_(['pending', 'scheduled']))
    if exclude_id:
        query = query.filter(Appointment.id != exclude_id)
    for a in query.all():
        a_start = datetime.combine(d, a.time)
        a_end = a_start + timedelta(minutes=a.duration or 60)
        if start < a_end and a_start < end:
            return a
    return None


def generate_ticket_code():
    while True:
        code = f"TK-{secrets.token_hex(3).upper()}"
        if not Appointment.query.filter_by(ticket_code=code).first():
            return code


def find_or_create_patient(full_name, phone, gender=None, email=None):
    """البحث عن المريض برقم الهاتف أو إنشاؤه من بيانات بوابة الحجز"""
    patient = Patient.query.filter_by(phone=phone).first() if phone else None
    if patient:
        if email and not patient.email:
            patient.email = email
        return patient
    parts = (full_name or '').strip().split()
    first = parts[0] if parts else 'مريض'
    last = ' '.join(parts[1:]) if len(parts) > 1 else '-'
    patient = Patient(code='P00000', first_name=first, last_name=last,
                      gender=gender, phone=phone or None, email=email or None)
    db.session.add(patient)
    db.session.flush()  # للحصول على id وتوليد كود فريد مبني عليه
    patient.code = f"P{patient.id + 1000:05d}"
    return patient


def log_action(action, entity, entity_id=None, details=''):
    """تسجيل العملية في سجل التدقيق (يعمل داخل الطلب وخارجه مثل تيليجرام)"""
    username = 'نظام'
    if has_request_context() and session.get('doctor_id'):
        u = User.query.get(session['doctor_id'])
        if u:
            username = u.username
    db.session.add(AuditLog(user=username, action=action, entity=entity,
                            entity_id=entity_id, details=details))


# ===================== إشعارات البريد الإلكتروني (Brevo) =====================

BREVO_API_URL = 'https://api.brevo.com/v3/smtp/email'


def brevo_send_email(api_key, sender_email, sender_name, to_email, subject, html):
    """إرسال بريد عبر Brevo API - يعيد (نجاح، رسالة خطأ)"""
    payload = json.dumps({
        'sender': {'name': sender_name or sender_email, 'email': sender_email},
        'to': [{'email': to_email}],
        'subject': subject,
        'htmlContent': html,
    }).encode('utf-8')
    req = urllib.request.Request(
        BREVO_API_URL, data=payload, method='POST',
        headers={'api-key': api_key, 'content-type': 'application/json',
                 'accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status in (200, 201), None
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode('utf-8', errors='replace')[:300]
        except Exception:
            detail = e.reason
        return False, f'HTTP {e.code}: {detail}'
    except Exception as e:
        return False, str(e)[:300]


def _send_email_worker(log_id, to_email, subject, html):
    """إرسال في خيط منفصل حتى لا يتعطل الطلب على الشبكة"""
    with app.app_context():
        log = db.session.get(NotificationLog, log_id)
        if not log:
            return
        api_key = get_setting('brevo_api_key', '')
        sender_email = get_setting('brevo_sender_email', '')
        sender_name = get_setting('brevo_sender_name', '') or 'العيادة'
        if not api_key or '@' not in sender_email:
            log.status = 'failed'
            log.error = 'لم يتم ضبط مفتاح Brevo أو بريد المرسل في الإعدادات'
        else:
            ok, err = brevo_send_email(api_key, sender_email, sender_name,
                                       to_email, subject, html)
            log.status = 'sent' if ok else 'failed'
            log.error = err
        db.session.commit()


def build_notification_html(title, message, appt, portal_root):
    status_color = {'confirmed': '#065f46', 'time_changed': '#854d0e',
                    'cancelled': '#991b1b'}.get(title[0], '#1e40af')
    return f"""
<div dir="rtl" style="font-family:Arial,sans-serif;max-width:560px;margin:auto;
border:1px solid #e2e8f0;border-radius:12px;overflow:hidden">
  <div style="background:#1e293b;color:#fff;padding:18px 24px">
    <h2 style="margin:0;font-size:18px">{clinic_name_safe()}</h2>
    {f'<div style="color:#94a3b8;font-size:13px">{clinic_doctor_safe()}</div>' if clinic_doctor_safe() else ''}
  </div>
  <div style="padding:24px;color:#1e293b;line-height:1.9">
    <h3 style="color:{status_color};margin-top:0">{title[1]}</h3>
    <p>{message}</p>
    <table style="width:100%;border-collapse:collapse;font-size:14px">
      <tr><td style="padding:6px 0;color:#64748b">التاريخ والساعة</td>
          <td style="padding:6px 0"><b>{appt.date.strftime('%Y/%m/%d')}</b>
          على الساعة <b>{appt.time.strftime('%H:%M')}</b></td></tr>
      {f"<tr><td style='padding:6px 0;color:#64748b'>رقم التذكرة</td><td style='padding:6px 0'><b>{appt.ticket_code or '-'}</b></td></tr>" if appt.ticket_code else ''}
    </table>
    <p style="margin-top:18px">
      <a href="{portal_root}/portal/ticket?code={appt.ticket_code or ''}"
         style="background:#4f46e5;color:#fff;padding:10px 20px;border-radius:8px;
                text-decoration:none;display:inline-block">متابعة حالة الحجز</a>
    </p>
  </div>
  <div style="background:#f1f5f9;color:#94a3b8;padding:12px 24px;font-size:12px;text-align:center">
    هذه رسالة تلقائية من نظام إدارة العيادة، يرجى عدم الرد عليها.
  </div>
</div>"""


def clinic_name_safe():
    return get_setting('clinic_name', 'عيادة الأخصائي النفساني')


def clinic_doctor_safe():
    return get_setting('doctor_name', '')


def queue_appointment_notification(appt, kind, old_dt=None, declined_dt=None):
    """إنشاء إشعار للمريض عند تغيير موعده (تأكيد / تغيير الوقت / إلغاء / رفض تأجيل).
    يُرسل عبر القناتين المتاحتين: البريد الإلكتروني (Brevo) وتيليجرام (البوت)،
    كل قناة حسب تفعيلها في الإعدادات وبيانات التواصل المتوفرة لدى المريض —
    ويُسجل كل شيء في notification_logs.
    """
    subject = {
        'confirmed': 'تأكيد موعدك',
        'time_changed': 'تغيير موعدك',
        'cancelled': 'إلغاء موعدك',
        'reschedule_declined': 'بخصوص طلب تأجيل موعدك',
    }.get(kind, 'تحديث موعدك')
    if kind == 'confirmed':
        title = ('confirmed', 'تم تأكيد موعدك ✔')
        message = 'يسرنا تأكيد موعدك. ننتظرك في التاريخ والساعة المذكورين أدناه.'
    elif kind == 'time_changed':
        title = ('time_changed', 'تم تغيير موعدك 🕐')
        old_txt = f'<b>{old_dt.strftime("%Y/%m/%d")}</b> على الساعة <b>{old_dt.strftime("%H:%M")}</b>' if old_dt else ''
        message = (f'نود إعلامك أن موعدك السابق ({old_txt}) '
                   'تم تعديله من طرف الطبيب إلى التاريخ والساعة الجديدين المذكورين أدناه.')
    elif kind == 'reschedule_declined':
        title = ('reschedule_declined', 'بقي موعدك كما كان 📌')
        declined_txt = (f'<b>{declined_dt.strftime("%Y/%m/%d")}</b> على الساعة '
                        f'<b>{declined_dt.strftime("%H:%M")}</b>') if declined_dt else 'الوقت الجديد الذي طلبته'
        message = (f'لم يوافق الطبيب على تأجيل موعدك إلى {declined_txt}. '
                   'موعدك الأصلي (المذكور أدناه) لا يزال قائماً كما كان — لا حاجة لأي إجراء إضافي منك. '
                   'يمكنك طلب تأجيل بتوقيت آخر من القائمة الرئيسية إن رغبت.')
    else:
        title = ('cancelled', 'تم إلغاء موعدك')
        message = ('نأسف لإبلاغك بأن الموعد المذكور أدناه تم إلغاؤه من طرف العيادة. '
                   'يمكنك حجز موعد جديد في أي وقت من بوابة المريض.')
    portal_root = request.url_root.rstrip('/') if request else ''
    # ---------- القناة الأولى: البريد الإلكتروني (Brevo) ----------
    if get_setting('notify_email_enabled') == '1':
        email = (appt.patient.email or '').strip() if appt.patient else ''
        if not email or '@' not in email:
            db.session.add(NotificationLog(
                appointment_id=appt.id, recipient=email or None,
                subject=subject, status='skipped',
                error='لا يوجد بريد إلكتروني لدى المريض'))
            db.session.commit()
        else:
            log = NotificationLog(appointment_id=appt.id, recipient=email,
                                  subject=subject, status='queued')
            db.session.add(log)
            db.session.flush()
            html = build_notification_html(title, message, appt, portal_root)
            db.session.commit()  # يجب حفظ السجل قبل بدء خيط الإرسال حتى يجده
            threading.Thread(target=_send_email_worker,
                             args=(log.id, email, subject, html), daemon=True).start()
    # ---------- القناة الثانية: تيليجرام (البوت) ----------
    if get_setting('notify_telegram_enabled') == '1':
        chat_id = (appt.patient.telegram_chat_id or '').strip() if appt.patient else ''
        if not chat_id:
            db.session.add(NotificationLog(
                appointment_id=appt.id, recipient=None, channel='telegram',
                subject=subject, status='skipped',
                error='المريض غير مرتبط ببوت تيليجرام (لا يوجد chat_id)'))
            db.session.commit()
        else:
            log = NotificationLog(appointment_id=appt.id, recipient=chat_id,
                                  channel='telegram', subject=subject, status='queued')
            db.session.add(log)
            db.session.flush()
            text = build_notification_text(title, message, appt, portal_root)
            buttons = None
            if portal_root and appt.ticket_code:
                buttons = [[{'text': '🎫 متابعة حالة الحجز',
                             'url': f'{portal_root}/portal/ticket?code={appt.ticket_code}'}]]
            db.session.commit()
            threading.Thread(target=_send_telegram_worker,
                             args=(log.id, chat_id, text, buttons), daemon=True).start()


def send_test_email(to_email):
    """بريد تجريبي للتحقق من إعدادات Brevo"""
    api_key = get_setting('brevo_api_key', '')
    sender_email = get_setting('brevo_sender_email', '')
    sender_name = get_setting('brevo_sender_name', '') or 'العيادة'
    if not api_key or '@' not in sender_email:
        return False, 'يرجى حفظ مفتاح Brevo وبريد المرسل أولاً'
    html = ('<div dir="rtl" style="font-family:Arial;padding:20px">'
            f'<h2>{clinic_name_safe()}</h2>'
            '<p>هذه رسالة تجريبية: إعدادات البريد الإلكتروني تعمل بنجاح ✅</p></div>')
    ok, err = brevo_send_email(api_key, sender_email, sender_name,
                               to_email, 'رسالة تجريبية من نظام العيادة', html)
    return ok, err


# ===================== إشعارات تيليجرام (البوت) =====================

TELEGRAM_API_BASE = 'https://api.telegram.org/bot'
# القيم الافتراضية لبوت العيادة، يمكن تغييرها من صفحة الإعدادات
DEFAULT_TELEGRAM_TOKEN = ''
DEFAULT_TELEGRAM_BOT = 'CLINIC_LARBI_bot'


def telegram_bot_token():
    return get_setting('telegram_bot_token', '') or DEFAULT_TELEGRAM_TOKEN


def telegram_bot_username():
    return get_setting('telegram_bot_username', '') or DEFAULT_TELEGRAM_BOT


def patient_telegram_deep_link(patient):
    """رابط عميق يربط المريض بالبوت: فتحه في تيليجرام يرسل /start <كود المريض> تلقائياً"""
    username = (telegram_bot_username() or '').lstrip('@')
    return f'https://t.me/{username}?start={patient.code}'


def telegram_api_call(method, payload=None):
    """استدعاء واجهة Telegram Bot API - يعيد (نجاح، رسالة خطأ)"""
    url = f'{TELEGRAM_API_BASE}{telegram_bot_token()}/{method}'
    data = json.dumps(payload).encode('utf-8') if payload else None
    req = urllib.request.Request(
        url, data=data, method='POST' if data else 'GET',
        headers={'content-type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            if result.get('ok'):
                return True, result.get('result')
            return False, result.get('description', 'خطأ غير معروف')
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode('utf-8', errors='replace'))
            return False, detail.get('description', f'HTTP {e.code}')
        except Exception:
            return False, f'HTTP {e.code}: {e.reason}'
    except Exception as e:
        return False, str(e)[:300]


def telegram_send_message(chat_id, text, buttons=None):
    """إرسال رسالة تيليجرام (HTML) مع أزرار Inline اختيارية - يعيد (نجاح، رسالة خطأ).
    buttons: قائمة صفوف، كل صف قائمة أزرار. كل زر إما {'text', 'callback_data'}
    لأزرار تفاعلية أو {'text', 'url'} لروابط خارجية.
    """
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}
    if buttons:
        payload['reply_markup'] = {'inline_keyboard': buttons}
    return telegram_api_call('sendMessage', payload)


def doctor_telegram_chat_id():
    """chat_id الخاص بالطبيب من الإعدادات (يُستخدم لإشعارات وتقارير الطبيب)."""
    return (get_setting('doctor_telegram_chat_id', '') or '').strip()


def notify_doctor(text, log_subject='إشعار للطبيب', buttons=None):
    """إرسال رسالة تيليجرام للطبيب عبر البوت (تذكيرات/تقارير/تنبيهات).
    يتطلب ضبط doctor_telegram_chat_id في صفحة الإعدادات. يُسجَّل الإرسال في
    notification_logs كأي قناة إشعار أخرى. يعيد True إذا حُاول الإرسال.
    buttons: قائمة صفوف أزرار inline (مثل telegram_send_message).
    """
    chat_id = doctor_telegram_chat_id()
    subject = log_subject
    if not chat_id:
        db.session.add(NotificationLog(
            recipient=None, channel='telegram', subject=subject, status='skipped',
            error='لم يُضبط chat_id الطبيب في الإعدادات'))
        db.session.commit()
        return False
    log = NotificationLog(recipient=chat_id, channel='telegram',
                          subject=subject, status='queued')
    db.session.add(log)
    db.session.flush()
    db.session.commit()
    threading.Thread(target=_send_telegram_worker,
                     args=(log.id, chat_id, text, buttons), daemon=True).start()
    return True


def _send_telegram_worker(log_id, chat_id, text, buttons=None):
    """إرسال تيليجرام في خيط منفصل حتى لا يتعطل الطلب على الشبكة"""
    with app.app_context():
        log = db.session.get(NotificationLog, log_id)
        if not log:
            return
        ok, err = telegram_send_message(chat_id, text, buttons=buttons)
        log.status = 'sent' if ok else 'failed'
        log.error = None if ok else str(err)[:500]
        db.session.commit()


def build_notification_text(title, message, appt, portal_root):
    """نسخة نصية (HTML) من إشعار الموعد مناسبة لتيليجرام"""
    lines = [
        f"<b>{clinic_name_safe()}</b>",
        f"<b>{title[1]}</b>",
        '',
        message,
        '',
        f"📅 التاريخ: <b>{appt.date.strftime('%Y/%m/%d')}</b>",
        f"🕐 الساعة: <b>{appt.time.strftime('%H:%M')}</b>",
    ]
    if appt.ticket_code:
        lines.append(f"🎟 رقم التذكرة: <b>{appt.ticket_code}</b>")
    if portal_root:
        lines.append(f"\nمتابعة الحجز: {portal_root}/portal/ticket?code={appt.ticket_code or ''}")
    return '\n'.join(lines)


def send_test_telegram(chat_id):
    """رسالة تيليجرام تجريبية للتحقق من إعدادات البوت"""
    ok, result = telegram_send_message(
        chat_id,
        f"<b>{clinic_name_safe()}</b>\n"
        'هذه رسالة تجريبية: إعدادات بوت تيليجرام تعمل بنجاح ✅')
    return ok, result if not ok else ''


def _telegram_error_hint(err):
    """تلميح عملي بالعربية لأخطاء تيليجرام الشائعة (يلحق برسالة الفشل)"""
    e = str(err or '').lower()
    if 'chat not found' in e:
        return ('المحادثة غير موجودة لدى تيليجرام: إما أن المعرف خاطئ، '
                'أو أن الشخص لم يضغط Start على هذا البوت أبدا، أو أنه حظره')
    if 'unauthorized' in e:
        return 'توكن البوت غير صحيح — راجع حقل توكن البوت في الإعدادات'
    if 'blocked' in e:
        return ('هذا المستخدم حظر البوت — '
                'عليه فتح البوت والضغط على Start مجددا')
    if 'timed out' in e or 'timeout' in e or 'urlopen error' in e:
        return 'تعذر الوصول إلى خوادم تيليجرام — تحقق من اتصال الإنترنت'
    return ''


# ===================== التذكير التلقائي بتأكيد الحضور =====================

def send_appointment_reminder(appt):
    """تذكير تيليجرام بموعد الغد مع زري تأكيد/اعتذار الحضور - يعيد النجاح"""
    patient = appt.patient
    chat_id = (patient.telegram_chat_id or '').strip() if patient else ''
    if not chat_id:
        db.session.add(NotificationLog(
            appointment_id=appt.id, channel='telegram', recipient=None,
            subject='تذكير بالموعد', status='skipped',
            error='المريض غير مرتبط ببوت تيليجرام'))
        db.session.commit()
        return False
    buttons = [[
        {'text': '✅ سأحضر', 'callback_data': f'confirm_yes_{appt.id}'},
        {'text': ' لن أحضر', 'callback_data': f'confirm_no_{appt.id}'},
    ]]
    ticket_line = (f"🎟 رقم التذكرة: <b>{appt.ticket_code}</b>\n\n"
                   if appt.ticket_code else '\n')
    message = (f"🔔 <b>تذكير بموعدك غداً</b>\n\n"
               f" التاريخ: <b>{appt.date.strftime('%Y/%m/%d')}</b>\n"
               f"🕐 الساعة: <b>{appt.time.strftime('%H:%M')}</b>\n"
               f"{ticket_line}"
               'هل ستحضر الموعد؟')
    ok, err = telegram_send_message(chat_id, message, buttons=buttons)
    db.session.add(NotificationLog(
        appointment_id=appt.id, channel='telegram', recipient=chat_id,
        subject='تذكير بالموعد', status='sent' if ok else 'failed',
        error=None if ok else str(err)[:500]))
    if ok:
        appt.reminder_sent = True
    db.session.commit()
    return ok


def check_and_send_reminders():
    """مهمة دورية: ترسل تذكير مواعيد الغد للمرضى المجدولين الذين لم يُرسل لهم بعد.
    تُنفذ كل 30 دقيقة، ولا ترسل شيئاً قبل ساعة التذكير المضبوطة في الإعدادات
    (telegram_reminder_hour، افتراضياً 18:00) — فلو نام الخادم على استضافة
    مجانية وأفاق متأخراً يبقى التذكير يُرسل.
    """
    with app.app_context():
        try:
            try:
                reminder_hour = int(get_setting('telegram_reminder_hour', '18') or 18)
            except ValueError:
                reminder_hour = 18
            if datetime.now().hour < reminder_hour:
                return
            tomorrow = date.today() + timedelta(days=1)
            appts = Appointment.query.filter(
                Appointment.date == tomorrow,
                Appointment.status == 'scheduled',
                db.or_(Appointment.reminder_sent.is_(False),
                       Appointment.reminder_sent.is_(None))).all()
            for appt in appts:
                try:
                    send_appointment_reminder(appt)
                except Exception as e:
                    print(f'فشل تذكير الموعد {appt.id}: {e}')
        except Exception as e:
            print(f'خطأ في مهمة التذكيرات: {e}')


def send_doctor_daily_report():
    """تقرير صباحي للطبيب بمواعيد اليوم + الطلبات المعلقة + الرسائل غير المقروءة.
    يُرسل عبر تيليجرام للطبيب (إذا ضُبط doctor_telegram_chat_id).
    يعيد True إذا أُرسل التقرير، False إذا لم يُضبط chat_id.
    """
    if not doctor_telegram_chat_id():
        return False
    today = date.today()
    today_appts = Appointment.query.filter_by(date=today).order_by(
        Appointment.time).all()
    pending = Appointment.query.filter_by(status='pending').order_by(
        Appointment.created_at.desc()).all()
    unread_msgs = PatientMessage.query.filter_by(
        sender='patient', is_read=False).count()
    lines = [f'🌅 <b>تقرير صباحي — {today.strftime("%Y/%m/%d")}</b>\n']
    lines.append(f'<b>مواعيد اليوم ({len(today_appts)})</b>')
    if today_appts:
        for a in today_appts[:10]:
            name = a.patient.full_name if a.patient else (a.contact_name or '—')
            status_emoji = {'scheduled': '🟢', 'pending': '🟡',
                            'completed': '✅', 'cancelled': '❌'}.get(a.status, '')
            lines.append(f'  {status_emoji} {a.time.strftime("%H:%M")} — {name}')
        if len(today_appts) > 10:
            lines.append(f'  <i>… و{len(today_appts) - 10} موعد آخر</i>')
    else:
        lines.append('  <i>لا توجد مواعيد اليوم</i>')
    lines.append(f'\n<b>طلبات حجز معلقة ({len(pending)})</b>')
    if pending:
        for a in pending[:5]:
            name = a.patient.full_name if a.patient else (a.contact_name or '—')
            lines.append(f'   {a.date.strftime("%Y/%m/%d")} {a.time.strftime("%H:%M")} — {name}')
        if len(pending) > 5:
            lines.append(f'  <i>… و{len(pending) - 5} طلب آخر</i>')
    else:
        lines.append('  <i>لا توجد طلبات معلقة</i> ✅')
    lines.append(f'\n📬 <b>رسائل غير مقروءة:</b> {unread_msgs}')
    lines.append(f'\n<i>— تقرير تلقائي من بوت العيادة</i>')
    return notify_doctor('\n'.join(lines), log_subject='تقرير صباحي للطبيب')


def check_and_send_doctor_report():
    """مهمة مجدولة: ترسل تقرير صباحي يومي للطبيب بين الساعة 7 و 10 صباحاً.
    تُنفذ كل 30 دقيقة لكنها تُرسل التقرير مرة واحدة فقط في اليوم (علم
    doctor_report_last_sent في الإعدادات يحفظ تاريخ آخر تقرير).
    """
    with app.app_context():
        try:
            now = datetime.now()
            if now.hour < 7 or now.hour >= 10:
                return
            today_str = date.today().strftime('%Y-%m-%d')
            last_sent = get_setting('doctor_report_last_sent', '')
            if last_sent == today_str:
                return
            if send_doctor_daily_report():
                set_setting('doctor_report_last_sent', today_str)
                db.session.commit()
        except Exception as e:
            print(f'خطأ في تقرير الطبيب الصباحي: {e}')


@app.route('/telegram/webhook', methods=['POST'])
def telegram_webhook():
    """استقبال تحديثات تيليجرام من خوادم تيليجرام (وضع الإنتاج مع webhook)"""
    try:
        process_telegram_update(request.get_json(silent=True) or {})
    except Exception as e:
        # نُرجع 200 دائماً للـ webhook مهما حدث داخلياً — وإلا يُعيد تيليجرام
        # محاولة تسليم نفس التحديث مراراً بعد كل خطأ 500، وقد يبدو للمستخدم
        # في هذه الأثناء وكأن شيئاً لا يعمل على الإطلاق.
        print(f'خطأ غير متوقع في معالجة تحديث تيليجرام (webhook): {e}')
    return {'ok': True}


def process_telegram_update(data):
    """معالجة تحديث تيليجرام واحد: ضغطة زر (callback_query) أو رسالة نصية"""
    callback = data.get('callback_query')
    if callback:
        _handle_telegram_callback(callback)
        return
    msg = data.get('message') or {}
    chat_id = str((msg.get('chat') or {}).get('id') or '')
    incoming = (msg.get('text') or '').strip()
    first_name = ((msg.get('from') or {}).get('first_name')) or ''
    # رسالة صوتية: للطبيب أمر صوتي كامل الصلاحيات، وللمريض مساعد محدود
    # النطاق (استعلام عن الموعد أو طلب تأجيل فقط) — إن كان قد بدأ محادثة
    # المساعد من القائمة الرئيسية؛ غير ذلك يحصل على رد مهذب (انظر voice_bot).
    if chat_id and (msg.get('voice') or msg.get('audio')):
        try:
            import voice_bot
            voice_bot.handle_voice_message(msg)
        except Exception as e:
            print(f'خطأ في معالجة الرسالة الصوتية: {e}')
        return
    if chat_id and incoming:
        try:
            _handle_telegram_message(chat_id, incoming, first_name)
        except Exception as e:
            print(f'خطأ غير متوقع في معالجة رسالة تيليجرام: {e}')
            try:
                telegram_send_message(
                    chat_id, '⚠️ حدث خطأ غير متوقع. حاول مجدداً، أو أرسل /start.')
            except Exception:
                pass


def _telegram_polling_loop():
    """استقبال تحديثات تيليجرام عبر getUpdates عندما لا يوجد webhook مضبوط.
    يتيح تجربة البوت محلياً دون رابط عام. وبعد ضبط webhook في الإنتاج يرد
    تيليجرام بخطأ 409 (تعارض الاثنين) فيتراجع الاستقبال المحلي تلقائياً.
    """
    import urllib.parse
    offset = None
    print("✅ بدء تشغيل مستقبل تحديثات تيليجرام (Polling Loop)...")
    while True:
        try:
            with app.app_context():  # مطلوب لقراءة التوكن من الإعدادات ومعالجة التحديثات
                params = {'timeout': 25}
                if offset:
                    params['offset'] = offset
                url = (f'{TELEGRAM_API_BASE}{telegram_bot_token()}/getUpdates?'
                       + urllib.parse.urlencode(params))
                with urllib.request.urlopen(url, timeout=30) as resp:
                    result = json.loads(resp.read().decode('utf-8'))
                for update in result.get('result') or []:
                    offset = update['update_id'] + 1
                    try:
                        process_telegram_update(update)
                    except Exception as e:
                        print(f'خطأ في معالجة تحديث تيليجرام: {e}')
        except urllib.error.HTTPError as e:
            if e.code == 409:  # webhook مفعل أو تطبيق آخر يستهلك التحديثات
                print("⚠️  خطأ 409: هناك webhook مفعل أو نسخة أخرى من التطبيق تعمل وتستهلك التحديثات")
                print("   الحل: أغلق النسخ الأخرى من التطبيق، أو احذف webhook من خلال:")
                print("   https://api.telegram.org/bot<TOKEN>/deleteWebhook")
                print("   الانتظار 30 ثانية قبل إعادة المحاولة...\n")
                _time.sleep(30)  # تم تقليل الوقت من 300 إلى 30 ثانية
            else:
                print(f'⚠️  توقف مؤقت لاستقبال تيليجرام: HTTP {e.code}')
                print(f'   التفاصيل: {e.reason}')
                _time.sleep(30)
        except Exception as e:
            print(f'⚠️  توقف مؤقت لاستقبال تيليجرام: {e}')
            print(f'   الانتظار 30 ثانية قبل إعادة المحاولة...\n')
            _time.sleep(30)


# ===================== المساعد الذكي للمريض (محدود النطاق) =====================
# جلسات المرضى النشطين حالياً في وضع "التحدث مع المساعد" — بالذاكرة فقط
# (نفس نمط جلسات assistant.py)، تُفعَّل بزر من القائمة الرئيسية وتُلغى بزر
# "إنهاء المحادثة"، أو بالضغط على أي زر تنقّل آخر، أو بأمر /start جديد.
# طالما الجلسة غير مفعّلة، تُعامَل كل رسالة نصية حرة كما كانت دوماً: تصل
# مباشرة للطبيب — لا تغيير هنا في السلوك الافتراضي المعتاد.
_patient_ai_sessions = set()


def _patient_in_ai_session(chat_id):
    return chat_id in _patient_ai_sessions


def _patient_ai_chat_menu():
    return [[{'text': '⬅️ إنهاء المحادثة والعودة للقائمة', 'callback_data': 'ai_chat_end'}]]


def _patient_ai_chat_start(chat_id, patient):
    if not patient:
        telegram_send_message(chat_id, '⚠️ حسابك غير مرتبط. أرسل '
                                       '<code>/start كود_المريض</code> للربط.')
        return
    _patient_ai_sessions.add(chat_id)
    telegram_send_message(
        chat_id,
        '🤖 <b>مرحباً بك، أنا مساعد العيادة</b>\n\n'
        'يمكنني مساعدتك فقط في أمرين:\n'
        '• الاستعلام عن حالة موعدك\n'
        '• طلب تأجيل موعد قائم لديك\n\n'
        'اكتب طلبك (نصاً أو رسالة صوتية) وسأجيبك — بالصوت أيضاً إن كتبت لي '
        'أو تحدثت صوتياً.\n'
        '<i>لأي أمر خارج هذين، تواصل مع الطبيب مباشرة من القائمة الرئيسية.</i>',
        buttons=_patient_ai_chat_menu())


def _patient_ai_chat_end(chat_id, patient):
    _patient_ai_sessions.discard(chat_id)
    try:
        import patient_assistant
        patient_assistant.clear_history(f'tg_patient_{chat_id}')
    except Exception:
        pass
    telegram_send_message(chat_id, 'تم إنهاء المحادثة مع المساعد. اختر من القائمة أدناه 👇',
                          buttons=_telegram_main_menu())


def _handle_patient_ai_message(chat_id, patient, incoming, voice_reply=False):
    """توجيه رسالة المريض (وهو داخل جلسة المساعد) للمساعد الذكي المحدود
    النطاق (استعلام عن الموعد أو طلب تأجيل فقط)، وإرسال الرد نصاً، وصوتاً
    أيضاً إن كانت الرسالة الواردة نفسها صوتية (voice_reply=True)."""
    import patient_assistant
    import ai_client
    if not ai_client.is_configured():
        telegram_send_message(
            chat_id, '⚠️ المساعد الذكي غير مفعّل حالياً في إعدادات العيادة. '
                     'يمكنك مراسلة الطبيب مباشرة من القائمة الرئيسية.',
            buttons=_telegram_main_menu())
        return
    telegram_api_call('sendChatAction', {'chat_id': chat_id, 'action': 'typing'})
    session_id = f'tg_patient_{chat_id}'
    try:
        result = patient_assistant.chat(session_id, patient, incoming)
        message = (result or {}).get('message') or 'عذراً، لم أفهم طلبك. حاول صياغته بشكل آخر.'
    except Exception as e:
        print(f'خطأ في المساعد الذكي للمريض: {e}')
        message = 'عذراً، حدث خطأ أثناء معالجة طلبك. يمكنك مراسلة الطبيب مباشرة من القائمة الرئيسية.'
    telegram_send_message(chat_id, message, buttons=_patient_ai_chat_menu())
    if voice_reply:
        try:
            import voice_bot
            voice_bot.reply_with_voice(chat_id, message)
        except Exception as e:
            print(f'خطأ في تحويل رد المساعد لصوت (مريض): {e}')


def _handle_telegram_message(chat_id, incoming, first_name=''):
    """رسائل المريض النصية: الربط بالكود، الأوامر، أو عرض القائمة"""
    # إذا كان chat_id هو طبيب، وجّه لـ handler الطبيب
    if chat_id and chat_id == doctor_telegram_chat_id():
        _handle_doctor_message(chat_id, incoming, first_name)
        return
    patient = Patient.query.filter_by(telegram_chat_id=chat_id).first()
    parts = incoming.split()
    cmd = parts[0].lower()
    # إن كان المريض داخل جلسة "التحدث مع المساعد" ورسالته نص حر (وليست
    # أمراً يبدأ بـ /) وجّهها للمساعد المحدود النطاق بدل مراسلة الطبيب مباشرة
    if patient and chat_id in _patient_ai_sessions and not incoming.startswith('/'):
        _handle_patient_ai_message(chat_id, patient, incoming, voice_reply=False)
        return
    if cmd.startswith('/start'):
        _patient_ai_sessions.discard(chat_id)
    # رسالة نصية حرة من مريض مرتبط = مراسلة الطبيب مباشرة
    if patient and not incoming.startswith('/'):
        new_msg = PatientMessage(patient_id=patient.id, sender='patient',
                                  body=incoming, channel='telegram')
        db.session.add(new_msg)
        log_action('create', 'message', patient.id, 'رسالة من تيليجرام')
        db.session.commit()
        db.session.refresh(new_msg)  # للحصول على id و created_at
        _emit_message_event('patient', new_msg)  # إشعار لحظي للطبيب
        # إشعار تيليجرام للطبيب برسالة المريض الجديدة
        preview = incoming if len(incoming) <= 200 else incoming[:200] + '…'
        notify_doctor(
            f'📩 <b>رسالة جديدة من مريض</b>\n\n'
            f'<b>{patient.full_name}</b> ({patient.code})\n'
            f'📞 {patient.phone or "—"}\n\n'
            f'{preview}\n\n'
            f'<i>— عبر تيليجرام</i>',
            log_subject='رسالة مريض جديدة')
        telegram_send_message(
            chat_id, '✅ وصلت رسالتك للطبيب، سيجيبك في أقرب وقت.\n'
                     'لعرض القائمة أرسل /start')
        return
    # كود المريض بعد أمر /start، أو الكود وحده إذا أُرسل بمفرده
    code = ''
    via_start_command = cmd.startswith('/start') and len(parts) > 1
    bare_word = len(parts) == 1 and not parts[0].startswith('/')
    if via_start_command:
        code = parts[1].strip().upper()
    elif bare_word:
        code = parts[0].strip().upper()

    # استعلام مباشر برقم تذكرة الموقع (TK-XXXXXX) عند إرساله بمفرده دون
    # /start — استعلام للقراءة فقط، لا يُغيّر أي ربط حساب قائم أو يُنشئ
    # واحداً جديداً؛ يفيد أي شخص يريد معرفة حالة حجز بعينه بسرعة من
    # تيليجرام دون المرور بموقع العيادة، تماماً كما يفعل من الموقع نفسه.
    if bare_word and code.startswith('TK'):
        appt = Appointment.query.filter_by(ticket_code=code).order_by(
            Appointment.id.desc()).first()
        if appt:
            hint = ''
            if not patient or patient.telegram_chat_id != chat_id:
                hint = ('\n\n🔗 لربط حسابك دائماً بإشعارات هذا الرقم، أرسل '
                        f'كود المريض عبر: <code>/start {appt.patient.code}</code>'
                        if appt.patient else '')
            telegram_send_message(chat_id, _ticket_status_text(appt) + hint)
        else:
            telegram_send_message(
                chat_id, f'⚠️ لا يوجد حجز بهذا الرقم: <code>{code}</code>\n'
                         'تأكد من نسخه بالكامل وبلا مسافات من صفحة نجاح الحجز.')
        return

    if code:
        # قبول نوعي الكود:
        # 1) رقم التذكرة TK-XXXXXX من الحجز الإلكتروني (الخطأ الشائع للمريض)
        # 2) كود المريض P01001 الرسمي للربط
        link_patient = None
        if code.startswith('TK'):
            appt = Appointment.query.filter_by(ticket_code=code).order_by(
                Appointment.id.desc()).first()
            if appt:
                link_patient = appt.patient
        if not link_patient:
            link_patient = Patient.query.filter_by(code=code).first()
        if link_patient:
            # افصل هذا الـ chat_id عن أي مريض آخر كان مرتبطاً به سابقاً —
            # وإلا يبقى مرتبطاً باثنين معاً، وكل استعلام لاحق (مواعيدي،
            # تأجيل...) يتم حله دوماً لأول مريض بينهما فقط عشوائياً حسب
            # الترتيب في القاعدة، وليس بالضرورة للمريض الذي يتحدث فعلاً.
            Patient.query.filter(
                Patient.telegram_chat_id == chat_id,
                Patient.id != link_patient.id).update({'telegram_chat_id': None})
            link_patient.telegram_chat_id = chat_id
            db.session.commit()
            _send_telegram_welcome(chat_id, link_patient, first_name)
        else:
            telegram_send_message(
                chat_id, '⚠️ الكود غير صحيح.\n\n'
                         '🔗 <b>للربط بالإشعارات</b> أرسل <b>كود المريض</b> '
                         '(يبدأ بحرف P): <code>/start P01001</code>\n'
                         'تجده في صفحة نجاح الحجز أو عند الطبيب.\n\n'
                         '🎫 أما رقم التذكرة (يبدأ بـ TK) فيُستخدم فقط '
                         'لمتابعة حالة الحجز من موقع العيادة.')
        return
    if cmd.startswith('/start'):
        if patient:
            _send_telegram_welcome(chat_id, patient, first_name)
        else:
            telegram_send_message(
                chat_id,
                'أهلاً بك 👋\n'
                f"لربط حسابك بإشعارات {clinic_name_safe()} أرسل:\n"
                '<code>/start كود_المريض</code>\n\n'
                'كود المريض يبدأ بحرف P وتجده عند الطبيب أو في صفحة '
                'نجاح الحجز (مثال: <code>/start P01001</code>).\n'
                ' رقم التذكرة TK-… لمتابعة الحجز من الموقع فقط، '
                'ولا يلزم للربط — ومع ذلك يُقبل هنا إذا كان لديك.')
    elif cmd.startswith('/appointments') and patient:
        telegram_send_message(chat_id, _my_appointments_text(patient),
                              buttons=_telegram_main_menu())
    elif cmd.startswith('/help'):
        telegram_send_message(
            chat_id,
            '🆘 <b>المساعدة</b>\n\n'
            '/start كود_المريض — ربط حسابك بالإشعارات\n'
            '/appointments — عرض مواعيدك القادمة\n'
            'للاستفسار تواصل مع العيادة مباشرة.')
    elif patient:
        telegram_send_message(chat_id,
                              'اختر من القائمة أدناه 👇',
                              buttons=_telegram_main_menu())


def _handle_telegram_callback(callback):
    """ضغطات أزرار Inline: نقطة الدخول الآمنة — تضمن استدعاء answerCallbackQuery
    دائماً (حتى لو حدث خطأ غير متوقع أثناء المعالجة)، وإلا يبقى الزر في حالة
    "تحميل" دائمة على هاتف المستخدم دون أي رد ظاهر — وهو بالضبط ما يبدو
    للمستخدم وكأن "الزر لا يعمل"."""
    try:
        _handle_telegram_callback_inner(callback)
    except Exception as e:
        print(f'خطأ غير متوقع أثناء معالجة ضغطة زر تيليجرام: {e}')
        chat_id = str(((callback.get('message') or {}).get('chat') or {}).get('id') or '')
        if chat_id:
            try:
                telegram_send_message(
                    chat_id, '⚠️ حدث خطأ غير متوقع أثناء تنفيذ هذا الإجراء. '
                             'حاول مجدداً، أو أرسل /start لإعادة عرض القائمة.')
            except Exception:
                pass
    finally:
        if callback.get('id'):
            telegram_api_call('answerCallbackQuery', {'callback_query_id': callback['id']})


def _handle_telegram_callback_inner(callback):
    """المنطق الفعلي لمعالجة ضغطات الأزرار: القائمة الرئيسية وتأكيد/اعتذار
    الحضور + أزرار الطبيب. لا تستدعِ هذه الدالة مباشرة — استخدم
    _handle_telegram_callback أعلاه الذي يضمن عدم بقاء الزر عالقاً عند الخطأ."""
    chat_id = str(((callback.get('message') or {}).get('chat') or {}).get('id') or '')
    action = (callback.get('data') or '').strip()
    # إذا كان chat_id هو طبيب، وجّه لـ handler الطبيب
    if chat_id and chat_id == doctor_telegram_chat_id():
        _handle_doctor_callback(callback, action, chat_id)
        return
    patient = Patient.query.filter_by(telegram_chat_id=chat_id).first() if chat_id else None
    # أي زر تنقّل آخر غير أزرار المساعد نفسه يُعتبر خروجاً ضمنياً من وضع
    # محادثة المساعد (كي لا تُفهم رسالة نصية حرة لاحقة على أنها موجّهة له)
    if chat_id in _patient_ai_sessions and action not in ('ai_chat_start', 'ai_chat_end'):
        _patient_ai_sessions.discard(chat_id)
    if not patient:
        if chat_id:
            telegram_send_message(chat_id, '⚠️ حسابك غير مرتبط. أرسل '
                                           '<code>/start كود_المريض</code> للربط.')
    elif action == 'my_appointments':
        telegram_send_message(chat_id, _my_appointments_text(patient),
                              buttons=_telegram_main_menu())
    elif action == 'last_status':
        telegram_send_message(chat_id, _last_booking_text(patient),
                              buttons=_telegram_main_menu())
    elif action == 'contact_clinic':
        telegram_send_message(chat_id, _contact_clinic_text(),
                              buttons=_telegram_back_menu())
    elif action == 'send_message':
        telegram_send_message(
            chat_id, '✍️ اكتب رسالتك الآن كنص عادي وسيصلها الطبيب مباشرة، '
                     'وستصلك إجابته هنا أو عبر البريد حسب الإعدادات.',
            buttons=_telegram_back_menu())
    elif action == 'main_menu':
        telegram_send_message(chat_id, 'اختر من القائمة أدناه 👇',
                              buttons=_telegram_main_menu())
    # ---------- حجز موعد جديد عبر البوت ----------
    elif action == 'book_new':
        _patient_show_new_booking_days(chat_id)
    elif action == 'pnd_restart':
        _patient_show_new_booking_days(chat_id)
    elif action.startswith('pnd:'):
        try:
            d = datetime.strptime(action.split(':', 1)[1], '%Y-%m-%d').date()
        except ValueError:
            return
        _patient_show_new_booking_times(chat_id, d)
    elif action.startswith('pnt:'):
        try:
            _, d_str, t_str = action.split(':', 2)
            d = datetime.strptime(d_str, '%Y-%m-%d').date()
            t = datetime.strptime(t_str, '%H:%M').time()
        except ValueError:
            return
        _patient_confirm_new_booking(chat_id, patient, d, t)
    # ---------- تأجيل موعد قائم عبر البوت ----------
    elif action == 'book_move':
        _patient_start_reschedule(chat_id, patient)
    elif action.startswith('pmv_pick:'):
        try:
            appt_id = int(action.split(':', 1)[1])
        except ValueError:
            return
        _patient_show_reschedule_days(chat_id, appt_id)
    elif action.startswith('pmd_restart:'):
        try:
            appt_id = int(action.split(':', 1)[1])
        except ValueError:
            return
        _patient_show_reschedule_days(chat_id, appt_id)
    elif action.startswith('pmd:'):
        try:
            _, appt_id_str, d_str = action.split(':', 2)
            appt_id = int(appt_id_str)
            d = datetime.strptime(d_str, '%Y-%m-%d').date()
        except ValueError:
            return
        _patient_show_reschedule_times(chat_id, appt_id, d)
    elif action.startswith('pmt:'):
        try:
            _, appt_id_str, d_str, t_str = action.split(':', 3)
            appt_id = int(appt_id_str)
            d = datetime.strptime(d_str, '%Y-%m-%d').date()
            t = datetime.strptime(t_str, '%H:%M').time()
        except ValueError:
            return
        _patient_apply_reschedule(chat_id, patient, appt_id, d, t)
    elif action.startswith('confirm_yes_') or action.startswith('confirm_no_'):
        _handle_attendance_callback(callback, action, chat_id, patient)
    elif action == 'ai_chat_start':
        _patient_ai_chat_start(chat_id, patient)
    elif action == 'ai_chat_end':
        _patient_ai_chat_end(chat_id, patient)
    # answerCallbackQuery يُستدعى دائماً من _handle_telegram_callback (انظر أعلاه)


def _handle_attendance_callback(callback, action, chat_id, patient):
    """تسجيل رد المريض على تذكير الحضور (✅ سأحضر / ❌ لن أحضر)"""
    yes = action.startswith('confirm_yes')
    try:
        appt_id = int(action.rsplit('_', 1)[1])
    except (ValueError, IndexError):
        return
    appt = db.session.get(Appointment, appt_id)
    if not appt or appt.patient_id != patient.id:
        return
    appt.attendance_confirmed = yes
    if yes:
        reply = '✅ تم تسجيل تأكيدك، بانتظارك في الموعد 🌿'
        db.session.commit()
    else:
        reply = ' تم استلام اعتذارك. سيتم إخطار العيادة، شكراً لإعلامنا '
        db.session.add(DoctorAlert(
            appointment_id=appt.id,
            kind='attendance_declined',
            message=(f"اعتذر المريض {patient.full_name} عن موعده "
                     f"{appt.date.strftime('%Y/%m/%d')} على الساعة "
                     f"{appt.time.strftime('%H:%M')}")))
        db.session.commit()
    telegram_send_message(chat_id, reply)
    # إزالة الأزرار من رسالة التذكير الأصلية بعد الرد
    message_id = (callback.get('message') or {}).get('message_id')
    if message_id:
        telegram_api_call('editMessageReplyMarkup', {
            'chat_id': chat_id, 'message_id': message_id,
            'reply_markup': {'inline_keyboard': []}})


# ===================== أوامر وأزرار الطبيب في تيليجرام =====================

def _doctor_menu_buttons():
    """القائمة الرئيسية للطبيب في البوت"""
    return [
        [{'text': '🟡 الطلبات المعلقة', 'callback_data': 'doc_pending'}],
        [{'text': '📅 مواعيد اليوم', 'callback_data': 'doc_today'}],
        [{'text': '📊 تقرير سريع', 'callback_data': 'doc_report'}],
    ]


def _doctor_pending_actions():
    """قراءة/كتابة حالة الإجراءات المعلقة للطبيب (مثل تعديل وقت موعد).
    تُخزَّن في settings بشكل JSON لت survived إعادة التشغيل.
    Structure: {'edit_appt': {appt_id: {'step': 'await_date'|'await_time'}}}
    """
    import json as _json
    raw = get_setting('doctor_pending_actions', '')
    try:
        return _json.loads(raw) if raw else {}
    except Exception:
        return {}


def _doctor_set_pending_actions(data):
    """حفظ حالة الإجراءات المعلقة للطبيب"""
    import json as _json
    set_setting('doctor_pending_actions', _json.dumps(data, ensure_ascii=False))
    db.session.commit()


def _doctor_appt_summary(appt):
    """نص ملخص موعد للطبيب (يُستخدم في القوائم والإشعارات)"""
    p = appt.patient
    name = p.full_name if p else (appt.contact_name or '—')
    phone = p.phone if p else (appt.contact_phone or '—')
    special_tag = ' ⭐ جلسة خاصة' if appt.is_special else ''
    return (f'🎫 <code>{appt.ticket_code}</code>{special_tag}\n'
            f'<b>{name}</b>\n'
            f' {phone}\n'
            f' {appt.date.strftime("%Y/%m/%d")} على الساعة {appt.time.strftime("%H:%M")}\n'
            f'⏱ {appt.duration} دقيقة'
            + (f'\n📝 {appt.reason}' if appt.reason else ''))


def _doctor_appt_buttons(appt_id, include_cancel=False):
    """أزرار إجراءات موعد للطبيب"""
    rows = [
        [{'text': '✅ تأكيد', 'callback_data': f'doc_confirm_{appt_id}'},
         {'text': '❌ رفض', 'callback_data': f'doc_reject_{appt_id}'}],
        [{'text': '📅 تعديل الوقت', 'callback_data': f'doc_edit_{appt_id}'}],
    ]
    if include_cancel:
        rows.append([{'text': '🗑 إلغاء الموعد', 'callback_data': f'doc_cancel_{appt_id}'}])
    return rows


def _notify_patient_appt_decision(appt, decision, new_dt=None):
    """إشعار المريض بقرار الطبيب (تأكيد/رفض/تعديل) عبر تيليجرام والبريد"""
    if decision == 'confirmed':
        subject = 'تأكيد موعدك'
        title = ('confirmed', 'تم تأكيد موعدك ✔')
        message = 'يسرنا تأكيد موعدك. ننتظرك في التاريخ والساعة المذكورين أدناه.'
        kind = 'confirmed'
    elif decision == 'rejected':
        # اعتبره إلغاءً من طرف العيادة
        subject = 'إلغاء موعدك'
        title = ('cancelled', 'تم إلغاء موعدك')
        message = ('نأسف لإبلاغك بأن طلب حجزك المذكور أدناه لم يُقبل. '
                   'يمكنك حجز موعد جديد في أي وقت من بوابة المريض.')
        kind = 'cancelled'
    elif decision == 'edited' and new_dt:
        subject = 'تغيير موعدك'
        title = ('time_changed', 'تم تغيير موعدك 🕐')
        old_dt = datetime.combine(appt.date, appt.time)
        old_txt = (f'<b>{old_dt.strftime("%Y/%m/%d")}</b> على الساعة '
                   f'<b>{old_dt.strftime("%H:%M")}</b>')
        message = (f'نود إعلامك أن موعدك السابق ({old_txt}) تم تعديله من طرف الطبيب '
                   'إلى التاريخ والساعة الجديدين المذكورين أدناه.')
        kind = 'time_changed'
    else:
        return
    # استخدم نفس آلية الإشعارات الموجودة (queue_appointment_notification)
    # لكن نحتاج request context لـ portal_root — نحاول، وإلا نستخدم إرسال مباشر
    try:
        from flask import request as _request
        has_ctx = has_request_context()
    except Exception:
        has_ctx = False
    portal_root = _request.url_root.rstrip('/') if has_ctx else ''
    # إعادة استخدام queue_appointment_notification (تُرسل بريد + تيليجرام للمريض)
    if decision == 'edited' and new_dt:
        appt.date = new_dt.date()
        appt.time = new_dt.time()
        db.session.commit()
        try:
            queue_appointment_notification(appt, 'time_changed', old_dt=datetime.combine(appt.date, appt.time))
        except Exception as e:
            print(f'خطأ في إشعار تعديل الموعد: {e}')
    else:
        try:
            queue_appointment_notification(appt, kind)
        except Exception as e:
            print(f'خطأ في إشعار قرار الموعد: {e}')


def _notify_patient_reschedule_declined(appt, declined_date, declined_time):
    """إشعار المريض بأن الطبيب رفض طلب تأجيله، وأن موعده الأصلي (المذكور في
    appt الآن بعد استعادته) لا يزال قائماً كما كان."""
    try:
        queue_appointment_notification(
            appt, 'reschedule_declined',
            declined_dt=datetime.combine(declined_date, declined_time))
    except Exception as e:
        print(f'خطأ في إشعار رفض التأجيل: {e}')


def _handle_doctor_callback(callback, action, chat_id):
    """معالجة ضغطات أزرار الطبيب في تيليجرام (تأكيد/رفض/تعديل/إلغاء + مساعد)"""
    message_id = (callback.get('message') or {}).get('message_id')
    if action == 'doc_pending':
        _doctor_send_pending_list(chat_id)
        return
    if action == 'doc_today':
        _doctor_send_today_appts(chat_id)
        return
    if action == 'doc_report':
        _doctor_send_quick_report(chat_id)
        return
    # أزرار المساعد الذكي: doc_aistart_exec_<id>, doc_aistart_cancel_<id>
    if action.startswith('doc_aistart_'):
        _handle_assistant_callback(callback, action, chat_id)
        return
    # أزرار الموعد: doc_confirm_<id>, doc_reject_<id>, doc_edit_<id>, doc_cancel_<id>
    if action.startswith('doc_confirm_'):
        try: appt_id = int(action[len('doc_confirm_'):])
        except ValueError: return
        _doctor_confirm_appt(chat_id, appt_id, message_id)
    elif action.startswith('doc_reject_'):
        try: appt_id = int(action[len('doc_reject_'):])
        except ValueError: return
        _doctor_reject_appt(chat_id, appt_id, message_id)
    elif action.startswith('doc_edit_'):
        try: appt_id = int(action[len('doc_edit_'):])
        except ValueError: return
        _doctor_start_edit_appt(chat_id, appt_id, message_id)
    elif action.startswith('doc_cancel_'):
        try: appt_id = int(action[len('doc_cancel_'):])
        except ValueError: return
        _doctor_cancel_appt(chat_id, appt_id, message_id)
    elif action == 'doc_edit_cancel':
        # إلغاء وضع التعديل
        actions = _doctor_pending_actions()
        actions.pop('edit_appt', None)
        _doctor_set_pending_actions(actions)
        telegram_send_message(chat_id, 'تم إلغاء وضع تعديل الموعد.')


def _doctor_confirm_appt(chat_id, appt_id, message_id=None):
    """تأكيد موعد من الطبيب عبر تيليجرام"""
    appt = db.session.get(Appointment, appt_id)
    if not appt:
        telegram_send_message(chat_id, '⚠️ الموعد غير موجود (ربما حُذف).')
        return
    if appt.status not in ('pending',):
        telegram_send_message(chat_id, f'⚠️ الموعد {appt.ticket_code} حالته الحالية: '
                                       f'{APPOINTMENT_STATUSES.get(appt.status, appt.status)} '
                                       f'— لا يمكن تأكيده مجدداً.')
        return
    appt.status = 'scheduled'
    # اعتُمد التأجيل (إن كان هذا الطلب ناتجاً عن تأجيل) — لا حاجة بعد الآن
    # للحالة السابقة المحفوظة لغرض الاستعادة عند الرفض
    appt.resched_prev_date = None
    appt.resched_prev_time = None
    appt.resched_prev_status = None
    log_action('update', 'appointment', appt.id, 'تأكيد موعد من تيليجرام')
    db.session.commit()
    # إشعار المريض
    _notify_patient_appt_decision(appt, 'confirmed')
    # تأكيد للطبيب
    telegram_send_message(
        chat_id,
        f'✅ <b>تم تأكيد الموعد</b>\n\n{_doctor_appt_summary(appt)}\n\n'
        f'تم إشعار المريض عبر القنوات المفعلة.')
    # إزالة الأزرار من الرسالة الأصلية
    if message_id:
        telegram_api_call('editMessageReplyMarkup', {
            'chat_id': chat_id, 'message_id': message_id,
            'reply_markup': {'inline_keyboard': []}})


def _doctor_reject_appt(chat_id, appt_id, message_id=None):
    """رفض موعد من الطبيب عبر تيليجرام.

    - إذا كان هذا طلب حجز/جلسة جديد (لا توجد حالة سابقة محفوظة) → يُلغى
      بالكامل، كالسابق.
    - إذا كان هذا طلب تأجيل لموعد كان قائماً أصلاً (resched_prev_* محفوظة)
      → قرار تصميم: يبقى الموعد الأصلي قائماً بتاريخه/ساعته/حالته السابقة
      بدل إلغائه، ويُخطر المريض أن طلب التأجيل رُفض وأن موعده الأصلي باقٍ.
    """
    appt = db.session.get(Appointment, appt_id)
    if not appt:
        telegram_send_message(chat_id, '️ الموعد غير موجود (ربما حُذف).')
        return
    if appt.status in ('cancelled', 'completed'):
        telegram_send_message(chat_id, f'⚠️ الموعد {appt.ticket_code} بالفعل '
                                       f'{APPOINTMENT_STATUSES.get(appt.status)}.')
        return
    if appt.resched_prev_date and appt.resched_prev_time:
        # طلب تأجيل — أعد الموعد لتاريخه/ساعته/حالته الأصليين بدل إلغائه
        declined_date, declined_time = appt.date, appt.time
        appt.date = appt.resched_prev_date
        appt.time = appt.resched_prev_time
        appt.status = appt.resched_prev_status or 'scheduled'
        appt.resched_prev_date = None
        appt.resched_prev_time = None
        appt.resched_prev_status = None
        log_action('update', 'appointment', appt.id,
                   'رفض طلب تأجيل من تيليجرام — إبقاء الموعد الأصلي')
        db.session.commit()
        _notify_patient_reschedule_declined(appt, declined_date, declined_time)
        telegram_send_message(
            chat_id,
            f'↩️ <b>تم رفض طلب التأجيل — الموعد الأصلي باقٍ كما كان</b>\n\n'
            f'{_doctor_appt_summary(appt)}\n\n'
            f'تم إشعار المريض ببقاء موعده على حاله.')
    else:
        # طلب حجز/جلسة جديد — لا يوجد موعد أصلي لإعادته، فيُلغى كالسابق
        appt.status = 'cancelled'
        log_action('update', 'appointment', appt.id, 'رفض/إلغاء موعد من تيليجرام')
        db.session.commit()
        _notify_patient_appt_decision(appt, 'rejected')
        telegram_send_message(
            chat_id,
            f'❌ <b>تم رفض/إلغاء الموعد</b>\n\n{_doctor_appt_summary(appt)}\n\n'
            f'تم إشعار المريض بإلغاء الموعد.')
    if message_id:
        telegram_api_call('editMessageReplyMarkup', {
            'chat_id': chat_id, 'message_id': message_id,
            'reply_markup': {'inline_keyboard': []}})


def _doctor_start_edit_appt(chat_id, appt_id, message_id=None):
    """بدء وضع تعديل وقت موعد: يطلب من الطبيب إرسال التاريخ الجديد"""
    appt = db.session.get(Appointment, appt_id)
    if not appt:
        telegram_send_message(chat_id, '⚠️ الموعد غير موجود (ربما حُذف).')
        return
    actions = _doctor_pending_actions()
    actions['edit_appt'] = {'appt_id': appt_id, 'step': 'await_date'}
    _doctor_set_pending_actions(actions)
    telegram_send_message(
        chat_id,
        f'📅 <b>تعديل وقت الموعد</b>\n\n{_doctor_appt_summary(appt)}\n\n'
        f'أرسل الآن <b>التاريخ الجديد</b> بأحد الصيغ التالية:\n'
        f'• <code>2026/09/25</code> أو <code>2026-09-25</code>\n'
        f'• <code>25/09/2026</code>\n'
        f'• أو <code>غداً</code> / <code>بعد غد</code>\n\n'
        f'لإلغاء وضع التعديل أرسل <code>/cancel</code>',
        buttons=[[{'text': '✖ إلغاء التعديل', 'callback_data': 'doc_edit_cancel'}]])
    if message_id:
        telegram_api_call('editMessageReplyMarkup', {
            'chat_id': chat_id, 'message_id': message_id,
            'reply_markup': {'inline_keyboard': []}})


def _doctor_cancel_appt(chat_id, appt_id, message_id=None):
    """إلغاء موعد مؤكد سابق من الطبيب عبر تيليجرام"""
    appt = db.session.get(Appointment, appt_id)
    if not appt:
        telegram_send_message(chat_id, '⚠️ الموعد غير موجود (ربما حُذف).')
        return
    if appt.status in ('cancelled',):
        telegram_send_message(chat_id, '️ هذا الموعد ملغى بالفعل.')
        return
    appt.status = 'cancelled'
    log_action('update', 'appointment', appt.id, 'إلغاء موعد مؤكد من تيليجرام')
    db.session.commit()
    # إشعار المريض بالإلغاء
    _notify_patient_appt_decision(appt, 'rejected')
    telegram_send_message(
        chat_id,
        f'🗑 <b>تم إلغاء الموعد</b>\n\n{_doctor_appt_summary(appt)}\n\n'
        f'تم إشعار المريض بإلغاء الموعد.')
    if message_id:
        telegram_api_call('editMessageReplyMarkup', {
            'chat_id': chat_id, 'message_id': message_id,
            'reply_markup': {'inline_keyboard': []}})


def _doctor_send_pending_list(chat_id):
    """عرض الطلبات المعلقة للطبيب"""
    pending = Appointment.query.filter_by(status='pending').order_by(
        Appointment.created_at.desc()).limit(10).all()
    if not pending:
        telegram_send_message(chat_id, '✅ لا توجد طلبات حجز معلقة حالياً.',
                              buttons=_doctor_menu_buttons())
        return
    telegram_send_message(chat_id, f'🟡 <b>الطلبات المعلقة ({len(pending)})</b>\n\n'
                                   'اضغط زر إجراء تحت كل طلب:', buttons=[])
    for appt in pending:
        telegram_send_message(chat_id, _doctor_appt_summary(appt),
                              buttons=_doctor_appt_buttons(appt.id))


def _doctor_send_today_appts(chat_id):
    """عرض مواعيد اليوم للطبيب مع زر إلغاء لكل موعد"""
    today = date.today()
    appts = Appointment.query.filter_by(date=today).filter(
        Appointment.status.in_(['scheduled', 'pending'])).order_by(
        Appointment.time).all()
    if not appts:
        telegram_send_message(chat_id, ' لا توجد مواعيد اليوم.',
                              buttons=_doctor_menu_buttons())
        return
    telegram_send_message(chat_id, f'📅 <b>مواعيد اليوم ({len(appts)})</b>', buttons=[])
    for appt in appts:
        telegram_send_message(chat_id, _doctor_appt_summary(appt),
                              buttons=_doctor_appt_buttons(appt.id, include_cancel=True))


def _doctor_send_quick_report(chat_id):
    """تقرير سريع للطبيب (نفس التقرير الصباحي لكن عند الطلب)"""
    today = date.today()
    today_appts = Appointment.query.filter_by(date=today).order_by(
        Appointment.time).all()
    pending = Appointment.query.filter_by(status='pending').order_by(
        Appointment.created_at.desc()).all()
    unread_msgs = PatientMessage.query.filter_by(
        sender='patient', is_read=False).count()
    lines = [f'📊 <b>تقرير سريع — {today.strftime("%Y/%m/%d")}</b>\n']
    lines.append(f'<b>مواعيد اليوم ({len(today_appts)})</b>')
    if today_appts:
        for a in today_appts[:10]:
            name = a.patient.full_name if a.patient else (a.contact_name or '—')
            se = {'scheduled': '🟢', 'pending': '🟡', 'completed': '✅',
                  'cancelled': '❌'}.get(a.status, '⚪')
            lines.append(f'  {se} {a.time.strftime("%H:%M")} — {name}')
    else:
        lines.append('  <i>لا توجد</i>')
    lines.append(f'\n<b>طلبات معلقة:</b> {len(pending)}')
    lines.append(f'<b>رسائل غير مقروءة:</b> {unread_msgs}')
    telegram_send_message(chat_id, '\n'.join(lines), buttons=_doctor_menu_buttons())


def _handle_doctor_message(chat_id, incoming, first_name='', voice_reply=False):
    """رسائل الطبيب النصية في تيليجرام: أوامر أو إدخال في وضع التعديل
    voice_reply=True (من الأوامر الصوتية) يجعل رد المساعد الذكي يصل أيضاً
    كملاحظة صوتية عبر voice_bot.reply_with_voice.
    """
    incoming = (incoming or '').strip()
    if not incoming:
        return
    parts = incoming.split()
    cmd = parts[0].lower() if parts else ''
    # معالجة وضع تعديل الموعد أولاً
    actions = _doctor_pending_actions()
    edit_state = actions.get('edit_appt') if actions else None
    if edit_state:
        if cmd == '/cancel':
            actions.pop('edit_appt', None)
            _doctor_set_pending_actions(actions)
            telegram_send_message(chat_id, 'تم إلغاء وضع تعديل الموعد. ✖',
                                  buttons=_doctor_menu_buttons())
            return
        appt = db.session.get(Appointment, edit_state.get('appt_id'))
        if not appt:
            actions.pop('edit_appt', None)
            _doctor_set_pending_actions(actions)
            telegram_send_message(chat_id, '⚠️ الموعد غير موجود. تم إلغاء وضع التعديل.')
            return
        step = edit_state.get('step')
        if step == 'await_date':
            new_date = _parse_doctor_date(incoming)
            if not new_date:
                telegram_send_message(
                    chat_id,
                    '️ لم أفهم التاريخ. جرّب:\n'
                    '<code>2026/09/25</code> أو <code>25/09/2026</code> '
                    'أو <code>غداً</code> / <code>بعد غد</code>\n\n'
                    'أو أرسل <code>/cancel</code> للإلغاء.')
                return
            if new_date < date.today():
                telegram_send_message(chat_id, '⚠️ لا يمكن اختيار تاريخ في الماضي. أرسل تاريخاً صحيحاً أو <code>/cancel</code>.')
                return
            edit_state['new_date'] = new_date.strftime('%Y-%m-%d')
            edit_state['step'] = 'await_time'
            actions['edit_appt'] = edit_state
            _doctor_set_pending_actions(actions)
            telegram_send_message(
                chat_id,
                f'✅ التاريخ الجديد: <b>{new_date.strftime("%Y/%m/%d")}</b>\n\n'
                f'الآن أرسل <b>الوقت الجديد</b> بصيغة <code>HH:MM</code> (مثل <code>14:30</code>):\n\n'
                f'أو أرسل <code>/cancel</code> للإلغاء.')
            return
        if step == 'await_time':
            new_time = _parse_doctor_time(incoming)
            if not new_time:
                telegram_send_message(
                    chat_id,
                    '⚠️ لم أفهم الوقت. جرّب صيغة <code>HH:MM</code> مثل <code>14:30</code>.\n\n'
                    'أو أرسل <code>/cancel</code> للإلغاء.')
                return
            try:
                new_date = datetime.strptime(edit_state['new_date'], '%Y-%m-%d').date()
            except Exception:
                telegram_send_message(chat_id, '⚠️ خطأ داخلي. أعد المحاولة.')
                actions.pop('edit_appt', None)
                _doctor_set_pending_actions(actions)
                return
            # تطبيق التعديل
            old_dt = datetime.combine(appt.date, appt.time)
            appt.date = new_date
            appt.time = new_time
            appt.status = 'scheduled'  # التعديل يُؤكد الموعد أيضاً
            log_action('update', 'appointment', appt.id, 'تعديل وقت موعد من تيليجرام')
            db.session.commit()
            actions.pop('edit_appt', None)
            _doctor_set_pending_actions(actions)
            # إشعار المريض بالتعديل (old_dt يجب أن يكون التاريخ القديم قبل التعديل)
            # (appt.date الآن الجديد، لكن old_dt حُفظ قبل التعديل)
            try:
                _notify_patient_appt_decision_edit(appt, old_dt)
            except Exception as e:
                print(f'خطأ في إشعار تعديل الموعد: {e}')
            telegram_send_message(
                chat_id,
                f'✅ <b>تم تعديل الموعد وتأكيده</b>\n\n{_doctor_appt_summary(appt)}\n\n'
                f'تم إشعار المريض بالموعد الجديد.',
                buttons=_doctor_menu_buttons())
            return
    # الأوامر
    if cmd == '/start':
        telegram_send_message(
            chat_id,
            f'👋 <b>أهلاً د. {get_setting("doctor_name","")}</b>\n'
            f'هذه لوحة تحكم الطبيب في بوت {clinic_name_safe()}.\n\n'
            f'اختر من القائمة أدناه:',
            buttons=_doctor_menu_buttons())
    elif cmd == '/pending':
        _doctor_send_pending_list(chat_id)
    elif cmd == '/today':
        _doctor_send_today_appts(chat_id)
    elif cmd == '/report':
        _doctor_send_quick_report(chat_id)
    elif cmd == '/cancel':
        telegram_send_message(chat_id, 'لا يوجد إجراء معلق لإلغائه.',
                              buttons=_doctor_menu_buttons())
    elif cmd == '/help' or cmd == '/menu':
        telegram_send_message(
            chat_id,
            '🆘 <b>أوامر الطبيب</b>\n\n'
            '/start — عرض القائمة الرئيسية\n'
            '/pending — الطلبات المعلقة\n'
            '/today — مواعيد اليوم\n'
            '/report — تقرير سريع\n'
            '/cancel — إلغاء الإجراء الجاري (مثل تعديل موعد)',
            buttons=_doctor_menu_buttons())
    else:
        # رسالة حرة = المساعد الذكي (GLM-5.3)
        _handle_doctor_assistant(chat_id, incoming, voice_reply=voice_reply)


# ===================== المساعد الذكي في تيليجرام =====================

import uuid as _uuid
_pending_assistant_calls = {}  # {call_id: {'tool': str, 'args': dict, 'client_id': str}}


def _handle_doctor_assistant(chat_id, incoming, voice_reply=False):
    """توجيه رسالة الطبيب الحرة للمساعد الذكي ومعالجة الرد."""
    import assistant
    import ai_client
    if not ai_client.is_configured():
        telegram_send_message(
            chat_id,
            '️ المساعد الذكي غير مفعّل — لم يُضبط أي مفتاح AI (GROQ_API_KEY أو GEMINI_API_KEY) في ملف .env',
            buttons=_doctor_menu_buttons())
        return
    # إظهار مؤشر "يكتب..."
    telegram_api_call('sendChatAction', {'chat_id': chat_id, 'action': 'typing'})
    client_id = f"tg_{chat_id}"
    result = assistant.chat(client_id, incoming)
    rtype = result.get('type')
    if rtype == 'error':
        telegram_send_message(chat_id, f'⚠️ {result.get("message", "خطأ")}',
                              buttons=_doctor_menu_buttons())
        return
    if rtype == 'text':
        telegram_send_message(chat_id, result.get('message', ''),
                              buttons=_doctor_menu_buttons())
        if voice_reply:
            try:
                import voice_bot
                voice_bot.reply_with_voice(chat_id, result.get('message', ''))
            except Exception as e:
                print(f'خطأ في الرد الصوتي: {e}')
        return
    if rtype == 'tool_call':
        # أداة كتابة — اعرض معاينة بأزرار تأكيد
        call_id = str(_uuid.uuid4())[:8]
        _pending_assistant_calls[call_id] = {
            'tool': result['tool'],
            'args': result['args'],
            'client_id': client_id,
        }
        preview = result.get('preview', {})
        summary = result.get('summary', '')
        msg = result.get('message', '') or summary
        # بنِ نص المعاينة
        lines = [f'🤖 <b>المساعد:</b>\n{msg}']
        if preview:
            lines.append('\n<b>التفاصيل:</b>')
            for k, v in preview.items():
                lines.append(f'  • {k}: {v}')
        lines.append('\n<i>اضغط للتأكيد أو الإلغاء:</i>')
        buttons = [
            [{'text': '✅ تنفيذ', 'callback_data': f'doc_aistart_exec_{call_id}'},
             {'text': '❌ إلغاء', 'callback_data': f'doc_aistart_cancel_{call_id}'}],
        ]
        telegram_send_message(chat_id, '\n'.join(lines), buttons=buttons)
        if voice_reply:
            try:
                import voice_bot
                voice_bot.reply_with_voice(chat_id, msg)
            except Exception as e:
                print(f'خطأ في الرد الصوتي: {e}')
        return


def _handle_assistant_callback(callback, action, chat_id):
    """معالجة أزرار تأكيد/إلغاء المساعد في تيليجرام."""
    import assistant
    message_id = (callback.get('message') or {}).get('message_id')
    if action.startswith('doc_aistart_exec_'):
        call_id = action[len('doc_aistart_exec_'):]
        pending = _pending_assistant_calls.pop(call_id, None)
        if not pending:
            telegram_send_message(chat_id, '⚠️ انتهت صلاحية هذا الطلب. أعد المحاولة.',
                                  buttons=_doctor_menu_buttons())
        else:
            telegram_api_call('sendChatAction', {'chat_id': chat_id, 'action': 'typing'})
            result = assistant.execute_confirmed_tool(
                pending['client_id'], pending['tool'], pending['args'])
            telegram_send_message(
                chat_id,
                f"{'✅' if result.get('success') else '⚠️'} {result.get('message', 'تم.')}",
                buttons=_doctor_menu_buttons())
    elif action.startswith('doc_aistart_cancel_'):
        call_id = action[len('doc_aistart_cancel_'):]
        _pending_assistant_calls.pop(call_id, None)
        telegram_send_message(chat_id, ' تم إلغاء العملية.',
                              buttons=_doctor_menu_buttons())
    # إزالة الأزرار من الرسالة الأصلية
    if message_id:
        telegram_api_call('editMessageReplyMarkup', {
            'chat_id': chat_id, 'message_id': message_id,
            'reply_markup': {'inline_keyboard': []}})


def _notify_patient_appt_decision_edit(appt, old_dt):
    """إشعار المريض بتعديل موعد عبر queue_appointment_notification (time_changed)"""
    try:
        queue_appointment_notification(appt, 'time_changed', old_dt=old_dt)
    except Exception as e:
        print(f'خطأ في إشعار تعديل الموعد: {e}')


def _parse_doctor_date(text):
    """تحليل تاريخ من نص حر. يعيد date أو None."""
    text = (text or '').strip()
    if not text:
        return None
    low = text.lower()
    today = date.today()
    if low in ('اليوم', 'today'):
        return today
    if low in ('غداً', 'غدا', 'غدًا', 'tomorrow'):
        return today + timedelta(days=1)
    if low in ('بعد غد', 'بعد غدٍ', 'after tomorrow'):
        return today + timedelta(days=2)
    # صيغ: YYYY-MM-DD, YYYY/MM/DD, DD/MM/YYYY, DD-MM-YYYY
    for fmt, sep_check in [
        ('%Y-%m-%d', '-'), ('%Y/%m/%d', '/'),
        ('%d-%m-%Y', '-'), ('%d/%m/%Y', '/'),
        ('%d-%m-%y', '-'), ('%d/%m/%y', '/'),
    ]:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_doctor_time(text):
    """تحليل وقت من نص حر. يعيد time أو None."""
    text = (text or '').strip()
    if not text:
        return None
    for fmt in ('%H:%M', '%H:%M:%S', '%H%M', '%I:%M %p', '%I:%M%p'):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _telegram_main_menu():
    """القائمة التفاعلية الرئيسية للبوت"""
    return [
        [{'text': '🗓️ استعلام عن المواعيد المتاحة', 'callback_data': 'book_new'}],
        [{'text': '🔁 تأجيل موعد', 'callback_data': 'book_move'}],
        [{'text': '📅 مواعيدي القادمة', 'callback_data': 'my_appointments'}],
        [{'text': '🎫 حالة آخر حجز', 'callback_data': 'last_status'}],
        [{'text': '🤖 التحدث مع المساعد', 'callback_data': 'ai_chat_start'}],
        [{'text': '✉️ مراسلة الطبيب', 'callback_data': 'send_message'}],
        [{'text': '☎️ التواصل مع العيادة', 'callback_data': 'contact_clinic'}],
    ]


def _telegram_back_menu():
    """قائمة فرعية للعودة من الشاشات الثانوية إلى القائمة الرئيسية"""
    return [
        [{'text': '✉️ اكتب رسالة للطبيب', 'callback_data': 'send_message'}],
        [{'text': '⬅️ القائمة الرئيسية', 'callback_data': 'main_menu'}],
    ]


def _send_telegram_welcome(chat_id, patient, first_name=''):
    """رسالة الترحيب بعد الربط الناجح مع القائمة التفاعلية"""
    name = first_name or patient.first_name
    telegram_send_message(
        chat_id,
        f'✅ أهلاً {name}!\n'
        f'تم ربط حسابك بـ {clinic_name_safe()} بنجاح.\n'
        'ستصلك هنا إشعارات تأكيد مواعيدك وتغييرها أو إلغائها.\n\n'
        'اختر من القائمة أدناه:',
        buttons=_telegram_main_menu())


def _my_appointments_text(patient):
    """نص المواعيد القادمة للمريض"""
    appts = Appointment.query.filter(
        Appointment.patient_id == patient.id,
        Appointment.date >= date.today(),
        Appointment.status.in_(['pending', 'scheduled'])
    ).order_by(Appointment.date, Appointment.time).all()
    if not appts:
        return 'لا توجد مواعيد قادمة حالياً.'
    lines = ['📅 <b>مواعيدك القادمة:</b>', '']
    for a in appts:
        state = APPOINTMENT_STATUSES.get(a.status, a.status)
        line = (f"• {a.date.strftime('%Y/%m/%d')} على الساعة "
                f"{a.time.strftime('%H:%M')} — {state}")
        if a.ticket_code:
            line += f"\n   {a.ticket_code}"
        lines.append(line)
    return '\n'.join(lines)


def _ticket_status_text(appt):
    """نص حالة حجز محدد بمعرفة رقم تذكرته (استعلام مباشر، بلا ربط حساب)."""
    state = APPOINTMENT_STATUSES.get(appt.status, appt.status)
    lines = [f"🎫 <b>حالة التذكرة {appt.ticket_code}:</b>",
             f"الحالة: <b>{state}</b>",
             f"التاريخ: {appt.date.strftime('%Y/%m/%d')}",
             f"الساعة: {appt.time.strftime('%H:%M')}"]
    return '\n'.join(lines)


def _last_booking_text(patient):
    """نص حالة آخر حجز للمريض"""
    last = Appointment.query.filter_by(patient_id=patient.id).order_by(
        Appointment.id.desc()).first()
    if not last:
        return 'لا يوجد سجل حجوزات.'
    state = APPOINTMENT_STATUSES.get(last.status, last.status)
    lines = ['🎫 <b>آخر حجز:</b>',
             f"الحالة: <b>{state}</b>",
             f"التاريخ: {last.date.strftime('%Y/%m/%d')}",
             f"الساعة: {last.time.strftime('%H:%M')}"]
    if last.ticket_code:
        lines.append(f"رقم التذكرة: {last.ticket_code}")
    return '\n'.join(lines)


def _contact_clinic_text():
    """نص بيانات التواصل مع العيادة من الإعدادات"""
    phone = get_setting('clinic_phone', '')
    address = get_setting('clinic_address', '')
    lines = ['☎️ <b>التواصل مع العيادة</b>']
    if phone:
        lines.append(f'📞 {phone}')
    if address:
        lines.append(f'📍 {address}')
    if len(lines) == 1:
        lines.append('لم تُضبط بيانات الهاتف والعنوان بعد.')
    lines.append('\n✉️ لإرسال رسالة مباشرة للطبيب من البوت: اضغط الزر أدناه '
                 '«️ اكتب رسالة للطبيب» ثم اكتب نصك.')
    return '\n'.join(lines)


# ===================== حجز/تأجيل المواعيد للمريض عبر تيليجرام =====================
# نفس منطق الحجز في /portal/booking (available_slots + has_time_conflict)
# لكن عبر أزرار Inline بدل نموذج ويب: اختيار يوم ← اختيار ساعة ← تأكيد.
# التأجيل يعيد استخدام نفس مسار الاختيار، ثم يحوّل الموعد القائم لحالة
# "pending" ويُخطر الطبيب بنفس أزرار تأكيد/رفض/تعديل الموجودة أصلاً — دون
# أي تعديل على الجزء الخاص بالطبيب.

PATIENT_BOOKING_HORIZON_DAYS = 30  # نفس أفق /portal/booking (max_date)
PATIENT_BOOKING_DAY_BUTTONS_LIMIT = 12  # أقصى عدد أزرار أيام في شاشة واحدة
PATIENT_BOOKING_START_OFFSET = 1  # يبدأ العرض من الغد (نفس افتراضي البوابة)


def _short_weekday(d):
    """اسم يوم مختصر + التاريخ، لعرضه على زر ضيق (مثال: الأحد 15/09)"""
    return f'{WEEKDAY_NAMES.get(d.weekday(), "")} {d.strftime("%d/%m")}'


def _patient_day_buttons(prefix, back_callback='main_menu'):
    """أزرار الأيام القريبة التي بها مواعيد متاحة فعلياً (تُحسب عبر available_slots).

    prefix: بادئة callback_data لكل يوم، مثل 'pnd' (حجز جديد) أو 'pmd:<appt_id>' (تأجيل).
    يعيد (buttons, found: bool) — found=False إذا لم يوجد أي يوم متاح ضمن الأفق.
    """
    rows, found_any = [], False
    d = date.today() + timedelta(days=PATIENT_BOOKING_START_OFFSET)
    end = date.today() + timedelta(days=PATIENT_BOOKING_HORIZON_DAYS)
    row = []
    while d <= end and len(rows) < PATIENT_BOOKING_DAY_BUTTONS_LIMIT:
        if available_slots(d):
            found_any = True
            row.append({'text': _short_weekday(d), 'callback_data': f'{prefix}:{d.isoformat()}'})
            if len(row) == 2:
                rows.append(row)
                row = []
        d += timedelta(days=1)
    if row:
        rows.append(row)
    rows.append([{'text': '⬅️ القائمة الرئيسية', 'callback_data': back_callback}])
    return rows, found_any


def _patient_time_buttons(d, prefix, back_callback):
    """أزرار الأوقات المتاحة ليوم مُختار مسبقاً."""
    rows, row = [], []
    for t in available_slots(d):
        row.append({'text': t.strftime('%H:%M'), 'callback_data': f'{prefix}:{t.strftime("%H:%M")}'})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{'text': '⬅️ اختيار يوم آخر', 'callback_data': back_callback}])
    return rows


def _patient_show_new_booking_days(chat_id):
    """شاشة استعلام عن المواعيد المتاحة: قائمة الأيام القريبة التي بها فراغ"""
    buttons, found = _patient_day_buttons('pnd')
    if not found:
        telegram_send_message(
            chat_id,
            '⚠️ لا توجد مواعيد متاحة حالياً خلال الفترة القادمة.\n'
            'يرجى التواصل مع العيادة مباشرة لمعرفة أقرب موعد ممكن.',
            buttons=_telegram_back_menu())
        return
    telegram_send_message(
        chat_id, '🗓️ <b>اختر اليوم المناسب:</b>\n(الأيام المعروضة فقط بها مواعيد متاحة)',
        buttons=buttons)


def _patient_show_new_booking_times(chat_id, d):
    """شاشة اختيار الساعة بعد اختيار اليوم (حجز جديد)"""
    slots = available_slots(d)
    if not slots:
        telegram_send_message(
            chat_id, '⚠️ عذراً، امتلأ هذا اليوم للتو. اختر يوماً آخر:',
            buttons=_patient_day_buttons('pnd')[0])
        return
    telegram_send_message(
        chat_id,
        f'🕐 <b>مواعيد {_short_weekday(d)} المتاحة:</b>\nاختر الساعة المناسبة:',
        buttons=_patient_time_buttons(d, f'pnt:{d.isoformat()}', 'pnd_restart'))


def _patient_confirm_new_booking(chat_id, patient, d, t):
    """إنشاء طلب حجز جديد بعد اختيار اليوم والساعة (نفس منطق /portal/booking)"""
    if d < date.today() or t not in available_slots(d):
        telegram_send_message(
            chat_id, '⚠️ عذراً، هذا الوقت لم يعد متاحاً (رُبما حجزه مريض آخر للتو).\n'
                     'اختر وقتاً آخر:',
            buttons=_patient_time_buttons(d, f'pnt:{d.isoformat()}', 'pnd_restart'))
        return
    sched = get_schedule()
    duration = sched['slot_minutes']
    if has_time_conflict(d, t, duration):
        telegram_send_message(
            chat_id, '⚠️ عذراً، هذا الوقت حُجز للتو. اختر وقتاً آخر:',
            buttons=_patient_time_buttons(d, f'pnt:{d.isoformat()}', 'pnd_restart'))
        return
    appt = Appointment(
        patient_id=patient.id, date=d, time=t, duration=duration,
        status='pending', source='patient', ticket_code=generate_ticket_code(),
        contact_name=patient.full_name, contact_phone=patient.phone)
    db.session.add(appt)
    log_action('create', 'appointment', details='طلب حجز عبر بوت تيليجرام')
    db.session.commit()
    appt_buttons = [
        [{'text': '✅ تأكيد', 'callback_data': f'doc_confirm_{appt.id}'},
         {'text': '❌ رفض', 'callback_data': f'doc_reject_{appt.id}'}],
        [{'text': '📅 تعديل الوقت', 'callback_data': f'doc_edit_{appt.id}'}],
    ]
    notify_doctor(
        f'🆕 <b>طلب حجز جديد (عبر البوت)</b>\n\n'
        f'<b>{patient.full_name}</b> ({patient.code})\n'
        f'📞 {patient.phone or "—"}\n'
        f'📅 {d.strftime("%Y/%m/%d")} على الساعة {t.strftime("%H:%M")}\n'
        f'⏱ {duration} دقيقة\n\n'
        f'🎟 رقم التذكرة: <code>{appt.ticket_code}</code>\n'
        f'<i>— بانتظار التأكيد من الطبيب</i>',
        log_subject='طلب حجز جديد عبر تيليجرام', buttons=appt_buttons)
    telegram_send_message(
        chat_id,
        f'✅ <b>تم إرسال طلبك بنجاح</b>\n\n'
        f'📅 {d.strftime("%Y/%m/%d")} — 🕐 {t.strftime("%H:%M")}\n'
        f'🎫 رقم التذكرة: <code>{appt.ticket_code}</code>\n\n'
        f'بانتظار تأكيد الطبيب، وستصلك رسالة فور اعتماد الموعد.',
        buttons=_telegram_main_menu())


def _patient_reschedulable_appts(patient):
    """مواعيد المريض القابلة للتأجيل (قادمة وغير ملغاة)"""
    return Appointment.query.filter(
        Appointment.patient_id == patient.id,
        Appointment.date >= date.today(),
        Appointment.status.in_(['pending', 'scheduled'])
    ).order_by(Appointment.date, Appointment.time).all()


def _patient_start_reschedule(chat_id, patient):
    """بداية مسار التأجيل: اختيار الموعد المراد تأجيله إن كان لدى المريض أكثر من واحد"""
    appts = _patient_reschedulable_appts(patient)
    if not appts:
        telegram_send_message(
            chat_id, 'لا يوجد لديك حالياً موعد قائم يمكن تأجيله.\n'
                     'يمكنك حجز موعد جديد من القائمة الرئيسية.',
            buttons=_telegram_main_menu())
        return
    if len(appts) == 1:
        _patient_show_reschedule_days(chat_id, appts[0].id)
        return
    rows = []
    for a in appts:
        label = f"{a.date.strftime('%Y/%m/%d')} {a.time.strftime('%H:%M')}"
        rows.append([{'text': label, 'callback_data': f'pmv_pick:{a.id}'}])
    rows.append([{'text': '⬅️ القائمة الرئيسية', 'callback_data': 'main_menu'}])
    telegram_send_message(chat_id, '🔁 <b>اختر الموعد الذي تريد تأجيله:</b>', buttons=rows)


def _patient_show_reschedule_days(chat_id, appt_id):
    """شاشة اختيار اليوم الجديد لتأجيل موعد مُحدَّد"""
    buttons, found = _patient_day_buttons(f'pmd:{appt_id}')
    if not found:
        telegram_send_message(
            chat_id,
            '⚠️ لا توجد مواعيد متاحة حالياً خلال الفترة القادمة للتأجيل إليها.\n'
            'يرجى التواصل مع العيادة مباشرة.',
            buttons=_telegram_back_menu())
        return
    telegram_send_message(
        chat_id, '🗓️ <b>اختر اليوم الجديد للموعد:</b>\n(الأيام المعروضة فقط بها فراغ)',
        buttons=buttons)


def _patient_show_reschedule_times(chat_id, appt_id, d):
    """شاشة اختيار الساعة الجديدة بعد اختيار اليوم (تأجيل)"""
    appt = db.session.get(Appointment, appt_id)
    slots = [t for t in available_slots(d)]
    # اسمح أيضاً بالساعة الأصلية لنفس الموعد إن وقعت ضمن نفس اليوم (كانت "محجوزة" بذاته)
    if appt and appt.date == d and appt.time not in slots:
        slots = sorted(slots + [appt.time])
    if not slots:
        telegram_send_message(
            chat_id, '⚠️ عذراً، امتلأ هذا اليوم للتو. اختر يوماً آخر:',
            buttons=_patient_day_buttons(f'pmd:{appt_id}')[0])
        return
    rows, row = [], []
    for t in slots:
        row.append({'text': t.strftime('%H:%M'),
                    'callback_data': f'pmt:{appt_id}:{d.isoformat()}:{t.strftime("%H:%M")}'})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{'text': '⬅️ اختيار يوم آخر', 'callback_data': f'pmd_restart:{appt_id}'}])
    telegram_send_message(
        chat_id, f'🕐 <b>اختر الساعة الجديدة ليوم {_short_weekday(d)}:</b>', buttons=rows)


def _reschedule_appt_core(patient, appt, d, t):
    """المنطق المشترك لتطبيق طلب تأجيل موعد قائم لمريض (يستخدمه مسار الأزرار
    ومسار المساعد الذكي على حدٍ سواء): يتحقق من الملكية والصلاحية، يحفظ
    الحالة/التاريخ/الساعة الأصليين في resched_prev_* (لإعادتها إن رفض الطبيب
    لاحقاً بدل إلغاء الموعد بالكامل)، يحوّل الموعد لحالة pending بالتاريخ/الساعة
    الجديدين، ويُخطر الطبيب بنفس أزرار تأكيد/رفض/تعديل الموجودة أصلاً.

    يعيد dict: {'ok': True, 'old_date':, 'old_time':, 'appt':} عند النجاح،
    أو {'ok': False, 'error': 'نص عربي يوضّح سبب الرفض'} عند الفشل — بلا أي
    إرسال تيليجرام (تاركاً صياغة الرسالة النهائية لطرف الاستدعاء).
    """
    if not appt or appt.patient_id != patient.id or appt.status not in ('pending', 'scheduled'):
        return {'ok': False, 'error': 'هذا الموعد لم يعد قابلاً للتأجيل.'}
    if d < date.today() or (t not in available_slots(d) and t != appt.time):
        return {'ok': False, 'error': 'عذراً، هذا الوقت لم يعد متاحاً. اختر وقتاً آخر.'}
    if has_time_conflict(d, t, appt.duration or 60, exclude_id=appt.id):
        return {'ok': False, 'error': 'عذراً، هذا الوقت حُجز للتو. اختر وقتاً آخر.'}
    old_date, old_time = appt.date, appt.time
    # احفظ الحالة الأصلية حتى يمكن إعادتها إن رفض الطبيب هذا التأجيل لاحقاً
    appt.resched_prev_date = old_date
    appt.resched_prev_time = old_time
    appt.resched_prev_status = appt.status
    appt.date, appt.time = d, t
    appt.status = 'pending'  # التأجيل من طرف المريض يحتاج إعادة تأكيد الطبيب
    log_action('update', 'appointment', appt.id, 'طلب تأجيل من المريض عبر تيليجرام')
    db.session.commit()
    appt_buttons = [
        [{'text': '✅ تأكيد', 'callback_data': f'doc_confirm_{appt.id}'},
         {'text': '❌ رفض', 'callback_data': f'doc_reject_{appt.id}'}],
        [{'text': '📅 تعديل الوقت', 'callback_data': f'doc_edit_{appt.id}'}],
    ]
    notify_doctor(
        f'🔄 <b>طلب تأجيل موعد (من المريض)</b>\n\n'
        f'<b>{patient.full_name}</b> ({patient.code})\n'
        f'📞 {patient.phone or "—"}\n'
        f'من: {old_date.strftime("%Y/%m/%d")} الساعة {old_time.strftime("%H:%M")}\n'
        f'إلى: <b>{d.strftime("%Y/%m/%d")} الساعة {t.strftime("%H:%M")}</b>\n\n'
        f'🎟 رقم التذكرة: <code>{appt.ticket_code}</code>\n'
        f'<i>— بانتظار موافقة الطبيب على التوقيت الجديد</i>',
        log_subject='طلب تأجيل عبر تيليجرام', buttons=appt_buttons)
    return {'ok': True, 'old_date': old_date, 'old_time': old_time, 'appt': appt}


def _patient_apply_reschedule(chat_id, patient, appt_id, d, t):
    """تنفيذ طلب التأجيل عبر مسار الأزرار التفاعلية، ثم صياغة رسالة للمريض."""
    appt = db.session.get(Appointment, appt_id)
    res = _reschedule_appt_core(patient, appt, d, t)
    if not res['ok']:
        # نفس شاشات الاختيار السابقة حسب سبب الرفض حتى يعيد المريض المحاولة بسهولة
        if appt and 'حُجز للتو' in res['error']:
            telegram_send_message(chat_id, f"⚠️ {res['error']}",
                                  buttons=_patient_day_buttons(f'pmd:{appt_id}')[0])
        elif appt and 'لم يعد متاحاً' in res['error']:
            telegram_send_message(
                chat_id, f"⚠️ {res['error']} اختر وقتاً آخر:",
                buttons=_patient_time_buttons(d, f'pmt:{appt_id}:{d.isoformat()}',
                                              f'pmd_restart:{appt_id}'))
        else:
            telegram_send_message(chat_id, f"⚠️ {res['error']}", buttons=_telegram_main_menu())
        return
    telegram_send_message(
        chat_id,
        f'✅ <b>تم إرسال طلب التأجيل</b>\n\n'
        f'من {res["old_date"].strftime("%Y/%m/%d")} {res["old_time"].strftime("%H:%M")}\n'
        f'إلى {d.strftime("%Y/%m/%d")} {t.strftime("%H:%M")}\n'
        f'🎫 رقم التذكرة: <code>{appt.ticket_code}</code>\n\n'
        f'بانتظار موافقة الطبيب، وستصلك رسالة فور اعتماد الموعد الجديد.\n'
        f'إن رفض الطبيب هذا التوقيت الجديد يبقى موعدك الأصلي كما كان.',
        buttons=_telegram_main_menu())


# ===================== حماية CSRF =====================

@app.before_request
def csrf_protect():
    # webhook تيليجرام يستقبال POST خارجي من خوادم تيليجرام دون جلسة
    if request.method == 'POST' and request.path == '/telegram/webhook':
        return
    if request.method == 'POST':
        token = session.get('_csrf_token')
        # الرمز قد يأتي من حقول النموذج (POST form) أو من ترويسة X-CSRF-Token
        # (الثانية ضرورية لطلبات AJAX التي ترسل FormData/JSON عبر fetch).
        submitted = request.form.get('_csrf_token') or request.headers.get('X-CSRF-Token')
        if not token or not submitted or submitted != token:
            abort(400, 'رمز الحماية (CSRF) غير صالح، يرجى تحديث الصفحة والمحاولة مجدداً')


def csrf_token():
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(16)
    return session['_csrf_token']


app.jinja_env.globals['csrf_token'] = csrf_token


# حقن رمز CSRF من جانب الخادم في كل نماذج POST (يعمل حتى بدون جافاسكربت)
import re as _re
_FORM_RE = _re.compile(r'<form\b[^>]*method=["\']post["\'][^>]*>', _re.IGNORECASE)


@app.after_request
def inject_csrf_input(response):
    if (response.status_code == 200
            and response.content_type.startswith('text/html')
            and not response.direct_passthrough):
        token = csrf_token()
        html = response.get_data(as_text=True)
        if 'name="_csrf_token"' not in html:
            html = _FORM_RE.sub(lambda m: m.group(0) +
                                f'<input type="hidden" name="_csrf_token" value="{token}">',
                                html)
            response.set_data(html)
    return response


# ===================== مصادقة الطبيب =====================

def doctor_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('doctor_id'):
            return redirect(url_for('login', next=request.path))
        return view(*args, **kwargs)
    return wrapped


# ===================== وسيط الرسائل اللحظية (SSE pub/sub) =====================

# وسيط بسيط في الذاكرة: عند ورود رسالة مريض جديد أو رد طبيب، يُبث الحدث إلى كل
# مشتركي SSE (صفحات الطبيب المفتوحة) ليظهر فوراً دون تحديث الصفحة.
# يعمل ضمن عملية واحدة (يكفي لعيادة بطبيب واحد)، ولا يحتاج Redis أو قاعدة بيانات.

import queue as _queue

_message_subscribers_lock = threading.Lock()
_message_subscribers = []  # قائمة كائنات queue.Queue (مرة لكل اتصال SSE)


def _messages_subscribe():
    """تسجيل مشترك جديد وإعادة طابوره لتلّقي الأحداث"""
    q = _queue.Queue()
    with _message_subscribers_lock:
        _message_subscribers.append(q)
    return q


def _messages_unsubscribe(q):
    """إلغاء اشتراك طابور عند إغلاق اتصال SSE"""
    with _message_subscribers_lock:
        try:
            _message_subscribers.remove(q)
        except ValueError:
            pass


def _messages_broadcast(event):
    """بث حدث إلى كل المشتركين (نسخة مستقلة لكل طابور)"""
    with _message_subscribers_lock:
        subs = list(_message_subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except _queue.Full:
                pass  # تجاهل إذا امتلأ الطابور (SSE معاد الاتصال تلقائياً)


def _emit_message_event(kind, message):
    """بني حدث رسالة لحظية وابعثه للأطباء.
    kind: 'patient' (رسالة من مريض) أو 'doctor' (رد طبيب حديث).
    message: كائن PatientMessage.
    """
    try:
        patient = message.patient
        event = {
            'kind': kind,
            'id': message.id,
            'patient_id': message.patient_id,
            'patient_name': patient.full_name if patient else 'مريض',
            'patient_code': patient.code if patient else '',
            'patient_initials': ((patient.first_name or '?')[:1] if patient else '?'),
            'sender': message.sender,
            'body': message.body or '',
            'channel': message.channel or 'portal',
            'time': message.created_at.strftime('%H:%M') if message.created_at else '',
            'timestamp': int((message.created_at or _time.time()).timestamp()) if message.created_at else int(_time.time()),
        }
        _messages_broadcast(event)
    except Exception as e:
        print(f'خطأ في بث حدث الرسالة: {e}')


@app.route('/messages/stream')
@doctor_required
def messages_stream():
    """نقطة نهاية Server-Sent Events: تدفق رسائل لحظية للطبيب.
    المتصفح يفتح اتصال EventSource على هذا المسار وييتلقى الأحداث مباشرة.
    نرسل نبضة كل 25 ثانية لإبقاء الاتصال حياً ومنع الوسائط من إغلاقه.
    """
    q = _messages_subscribe()

    def stream():
        try:
            # إرسال حدث ترحيب فوري لتأكيد نجاح الاتصال
            yield 'event: hello\ndata: {"ok":true}\n\n'
            while True:
                try:
                    event = q.get(timeout=25)
                    payload = json.dumps(event, ensure_ascii=False)
                    # اسم الحدث: 'message_new' حتى يميزه JS عن نبضات الاتصال
                    yield f'event: message_new\ndata: {payload}\n\n'
                except _queue.Empty:
                    # نبضة بقاء حياة (comment) — SSE يتجاهلها
                    yield ': keep-alive\n\n'
        finally:
            _messages_unsubscribe(q)

    headers = {
        'Cache-Control': 'no-cache, no-transform',
        'X-Accel-Buffering': 'no',  # تعطيل تخزين Caddy/nginx لإخراج SSE
        'Connection': 'keep-alive',
    }
    return Response(stream(), mimetype='text/event-stream', headers=headers)


@app.route('/messages/threads.json')
@doctor_required
def messages_threads_json():
    """قائمة مختصرة لآخر المحادثات لاستخدامها في الفقاعات العائمة.
    تُرجع آخر N محادثة مع آخر رسالة + عداد غير المقروء، مرتبة بحديث آخر رسالة.
    """
    limit = request.args.get('limit', 8, type=int)
    # أحدث رسالة لكل مريض (subquery)
    latest_ids = db.session.query(
        PatientMessage.patient_id,
        db.func.max(PatientMessage.id).label('max_id')
    ).group_by(PatientMessage.patient_id).subquery()
    rows = db.session.query(PatientMessage).join(
        latest_ids,
        (PatientMessage.id == latest_ids.c.max_id)
    ).order_by(PatientMessage.created_at.desc()).limit(limit).all()
    items = []
    for m in rows:
        p = m.patient
        if not p:
            continue
        unread = PatientMessage.query.filter_by(
            patient_id=p.id, sender='patient', is_read=False).count()
        body = m.body or ''
        if len(body) > 80:
            body = body[:80] + '…'
        items.append({
            'patient_id': p.id,
            'patient_name': p.full_name,
            'patient_code': p.code,
            'patient_initials': (p.first_name or '?')[:1],
            'last_body': body,
            'last_sender': m.sender,
            'last_time': m.created_at.strftime('%H:%M') if m.created_at else '',
            'last_timestamp': int(m.created_at.timestamp()) if m.created_at else 0,
            'unread': unread,
            'has_telegram': bool(p.telegram_chat_id),
            'url': url_for('message_thread', patient_id=p.id),
        })
    total_unread = PatientMessage.query.filter_by(
        sender='patient', is_read=False).count()
    return {'threads': items, 'unread_count': total_unread}


@app.route('/messages/<int:patient_id>/messages.json')
@doctor_required
def messages_json(patient_id):
    """رسائل محادثة مريض معين بصيغة JSON — لاستخدامها في الـ floating chatbox.
    تُعلّم رسائل المريض كمقروءة عند الجلب (تماماً مثل فتح صفحة المحادثة الكاملة).
    """
    patient = db.session.get(Patient, patient_id)
    if not patient:
        abort(404)
    msgs = PatientMessage.query.filter_by(patient_id=patient_id).order_by(
        PatientMessage.created_at).all()
    # تعليم رسائل المريض كمقروءة
    PatientMessage.query.filter_by(patient_id=patient_id, sender='patient',
                                   is_read=False).update({'is_read': True})
    db.session.commit()
    return jsonify({
        'patient': {
            'id': patient.id,
            'full_name': patient.full_name,
            'code': patient.code,
            'first_name': patient.first_name,
            'phone': patient.phone or '',
            'email': patient.email or '',
            'has_telegram': bool(patient.telegram_chat_id),
        },
        'messages': [{
            'id': m.id,
            'sender': m.sender,
            'body': m.body or '',
            'channel': m.channel or '',
            'time': m.created_at.strftime('%H:%M') if m.created_at else '',
            'date': m.created_at.strftime('%Y/%m/%d') if m.created_at else '',
        } for m in msgs],
    })


# حماية بسيطة ضد تجربة كلمات المرور: 10 محاولات فاشلة لكل عنوان IP
_login_attempts = {}


@app.route('/login', methods=['GET'])
def login():
    if session.get('doctor_id'):
        return redirect(url_for('dashboard'))
    return render_template('login.html')


@app.route('/login/doctor', methods=['POST'])
def login_doctor():
    ip = request.remote_addr or 'unknown'
    now = _time.time()
    attempts = _login_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < 300]
    if len(attempts) >= 10:
        flash('تم تجاوز عدد المحاولات المسموح، حاول بعد 5 دقائق', 'error')
        return redirect(url_for('login'))
    if len(attempts) >= 5:
        _time.sleep(2)
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    user = User.query.filter_by(username=username).first()
    if user and user.check_password(password):
        session['doctor_id'] = user.id
        _login_attempts.pop(ip, None)
        log_action('login', 'user', user.id, 'تسجيل دخول ناجح')
        db.session.commit()
        flash(f'مرحباً {user.name or user.username}', 'success')
        dest = request.args.get('next') or url_for('dashboard')
        if not dest.startswith('/'):
            dest = url_for('dashboard')
        return redirect(dest)
    attempts.append(now)
    _login_attempts[ip] = attempts
    flash('اسم المستخدم أو كلمة المرور غير صحيحة', 'error')
    return render_template('login.html', login_error=True)


@app.route('/logout', methods=['POST'])
def logout():
    session.pop('doctor_id', None)
    flash('تم تسجيل الخروج', 'success')
    return redirect(url_for('login'))


# ===================== بوابة المريض (عامة) =====================

@app.route('/portal')
def portal_home():
    return render_template('portal/menu.html')


@app.route('/portal/availability')
def portal_availability():
    """جدول المواعيد المتاحة لأول 14 يوماً"""
    today = date.today()
    days = []
    for i in range(14):
        d = today + timedelta(days=i)
        slots = available_slots(d)
        days.append({'date': d, 'slots': slots, 'count': len(slots),
                     'closed': d.weekday() in get_schedule()['closed_days']})
    return render_template('portal/availability.html', days=days,
                           weekday_names=WEEKDAY_NAMES)


@app.route('/portal/booking', methods=['GET', 'POST'])
def portal_booking():
    """طلب حجز موعد من المريض - يظهر له الأوقات المتاحة فقط"""
    selected_date = date.today() + timedelta(days=1)
    if request.method == 'GET' and request.args.get('date'):
        try:
            selected_date = datetime.strptime(
                request.args['date'], '%Y-%m-%d').date()
        except ValueError:
            pass
    slots = available_slots(selected_date)
    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        phone = request.form.get('phone', '').strip()
        try:
            d = datetime.strptime(request.form['date'], '%Y-%m-%d').date()
        except (KeyError, ValueError):
            flash('يرجى اختيار تاريخ صحيح', 'error')
            return redirect(url_for('portal_booking'))
        try:
            t = datetime.strptime(request.form['time'], '%H:%M').time()
        except (KeyError, ValueError):
            flash('يرجى اختيار وقت الموعد', 'error')
            return redirect(url_for('portal_booking', date=d.strftime('%Y-%m-%d')))
        if not full_name or not phone:
            flash('يرجى إدخال الاسم ورقم الهاتف', 'error')
            return redirect(url_for('portal_booking', date=d.strftime('%Y-%m-%d')))
        if d < date.today() or t not in available_slots(d):
            flash('هذا الوقت لم يعد متاحاً، يرجى اختيار وقت آخر', 'error')
            return redirect(url_for('portal_booking', date=d.strftime('%Y-%m-%d')))
        patient = find_or_create_patient(full_name, phone,
                                         request.form.get('gender'),
                                         email=request.form.get('email', '').strip() or None)
        appt = Appointment(
            patient_id=patient.id, date=d, time=t,
            duration=int(request.form.get('duration', 60) or 60),
            reason=request.form.get('reason'), notes=request.form.get('notes'),
            status='pending', source='patient', ticket_code=generate_ticket_code(),
            contact_name=full_name, contact_phone=phone,
        )
        if has_time_conflict(d, t, appt.duration):
            flash('هذا الوقت محجوز للتو، يرجى اختيار وقت آخر', 'error')
            return redirect(url_for('portal_booking', date=d.strftime('%Y-%m-%d')))
        db.session.add(appt)
        log_action('create', 'appointment', details='طلب حجز إلكتروني')
        db.session.commit()
        # إشعار الطبيب بطلب الحجز الإلكتروني الجديد مع أزرار تأكيد/رفض/تعديل
        appt_buttons = [
            [{'text': '✅ تأكيد', 'callback_data': f'doc_confirm_{appt.id}'},
             {'text': '❌ رفض', 'callback_data': f'doc_reject_{appt.id}'}],
            [{'text': '📅 تعديل الوقت', 'callback_data': f'doc_edit_{appt.id}'}],
        ]
        notify_doctor(
            f'🆕 <b>طلب حجز إلكتروني جديد</b>\n\n'
            f'<b>{full_name}</b>\n'
            f'📞 {phone}\n'
            f'📅 {d.strftime("%Y/%m/%d")} على الساعة {t.strftime("%H:%M")}\n'
            f'⏱ {appt.duration} دقيقة\n'
            + (f'📝 {appt.reason}\n' if appt.reason else '')
            + f'\n🎟 رقم التذكرة: <code>{appt.ticket_code}</code>\n'
            f'<i>— بانتظار التأكيد من الطبيب</i>',
            log_subject='طلب حجز جديد', buttons=appt_buttons)
        flash(f'تم إرسال طلب الحجز بنجاح! رقم تذكرتك: {appt.ticket_code}', 'success')
        return render_template('portal/success.html', appt=appt,
                               special=False)
    max_date = (date.today() + timedelta(days=30)).strftime('%Y-%m-%d')
    return render_template('portal/booking.html',
                           selected_date=selected_date, slots=slots,
                           max_date=max_date, weekday_names=WEEKDAY_NAMES)


@app.route('/portal/special', methods=['GET', 'POST'])
def portal_special():
    """طلب جلسة خاصة خارج أوقات العمل مع التكفل بمصاريف النقل"""
    sched = get_schedule()
    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        phone = request.form.get('phone', '').strip()
        if not full_name or not phone:
            flash('يرجى إدخال الاسم ورقم الهاتف', 'error')
            return redirect(url_for('portal_special'))
        try:
            d = datetime.strptime(request.form['date'], '%Y-%m-%d').date()
        except (KeyError, ValueError):
            flash('يرجى اختيار تاريخ صحيح', 'error')
            return redirect(url_for('portal_special'))
        try:
            t = datetime.strptime(request.form['time'], '%H:%M').time()
        except (KeyError, ValueError):
            flash('يرجى تحديد الوقت المفضل', 'error')
            return redirect(url_for('portal_special'))
        if d < date.today():
            flash('لا يمكن اختيار تاريخ في الماضي', 'error')
            return redirect(url_for('portal_special'))
        patient = find_or_create_patient(full_name, phone,
                                         email=request.form.get('email', '').strip() or None)
        appt = Appointment(
            patient_id=patient.id, date=d, time=t, duration=60,
            reason='جلسة خاصة خارج أوقات العمل',
            notes=request.form.get('notes'),
            status='pending', source='patient', is_special=True,
            transport_fee=sched['default_transport_fee'],
            ticket_code=generate_ticket_code(),
            contact_name=full_name, contact_phone=phone,
        )
        db.session.add(appt)
        log_action('create', 'appointment', details='طلب جلسة خاصة')
        db.session.commit()
        # إشعار الطبيب بطلب الجلسة الخاصة الجديد مع أزرار تأكيد/رفض/تعديل
        notes_preview = (appt.notes or '').strip()
        if len(notes_preview) > 150:
            notes_preview = notes_preview[:150] + '…'
        appt_buttons = [
            [{'text': '✅ تأكيد', 'callback_data': f'doc_confirm_{appt.id}'},
             {'text': '❌ رفض', 'callback_data': f'doc_reject_{appt.id}'}],
            [{'text': '📅 تعديل الوقت', 'callback_data': f'doc_edit_{appt.id}'}],
        ]
        notify_doctor(
            f'⭐ <b>طلب جلسة خاصة جديدة</b>\n\n'
            f'<b>{full_name}</b>\n'
            f' {phone}\n'
            f' {d.strftime("%Y/%m/%d")} على الساعة {t.strftime("%H:%M")}\n'
            f'💰 مصاريف النقل: {appt.transport_fee or 0}\n'
            + (f'📝 {notes_preview}\n' if notes_preview else '')
            + f'\n🎟 رقم التذكرة: <code>{appt.ticket_code}</code>\n'
            f'<i>— جلسة خارج أوقات العمل، بانتظار التأكيد</i>',
            log_subject='طلب جلسة خاصة', buttons=appt_buttons)
        flash(f'تم إرسال طلب الجلسة الخاصة! رقم تذكرتك: {appt.ticket_code}', 'success')
        return render_template('portal/success.html', appt=appt, special=True)
    return render_template('portal/special.html',
                           transport_fee=sched['default_transport_fee'])


@app.route('/portal/ticket', methods=['GET', 'POST'])
def portal_ticket():
    """متابعة حالة الحجز برقم التذكرة - متاح من واجهة الدخول"""
    appt = None
    searched = False
    if request.method == 'POST':
        code = request.form.get('ticket_code', '').strip().upper()
        searched = True
        if code:
            appt = Appointment.query.filter_by(ticket_code=code).first()
    elif request.args.get('code'):
        appt = Appointment.query.filter_by(
            ticket_code=request.args['code'].strip().upper()).first()
        searched = appt is not None
    return render_template('portal/ticket.html', appt=appt, searched=searched,
                           statuses=APPOINTMENT_STATUSES)


@app.route('/portal/link-qr/<code>')
def portal_link_qr(code):
    """رمز QR لربط حساب المريض بالبوت — يظهر له في صفحة نجاح الحجز.
    الرمز يحمل رابطاً عميقاً (t.me/البوت?start=كود المريض) يفتح البوت
    ويرسل أمر الربط تلقائياً عند مسحه بكاميرا الهاتف.
    """
    patient = Patient.query.filter_by(code=code.strip().upper()).first_or_404()
    try:
        import qrcode
    except ImportError:
        abort(500, 'مكتبة qrcode غير مثبتة — نفّذ: pip install qrcode[pil]')
    import io
    qr = qrcode.QRCode(box_size=8, border=2,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(patient_telegram_deep_link(patient))
    qr.make(fit=True)
    img = qr.make_image(fill_color='#4f46e5', back_color='white')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    resp = app.response_class(buf.getvalue(), mimetype='image/png')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


# ===================== لوحة التحكم =====================

@app.route('/')
@doctor_required
def dashboard():
    today = date.today()
    total_patients = Patient.query.filter_by(is_active=True).count()
    today_appointments = Appointment.query.filter_by(date=today).order_by(
        Appointment.time).all()
    completed_visits = Visit.query.count()
    pending_invoices = Invoice.query.filter(Invoice.status != 'paid').count()
    pending_requests = Appointment.query.filter_by(status='pending').count()
    total_revenue = db.session.query(
        db.func.coalesce(db.func.sum(Invoice.paid_amount), 0)).scalar()
    pending_balance = db.session.query(
        db.func.coalesce(db.func.sum(Invoice.amount - Invoice.paid_amount), 0)).scalar()
    # إحصائيات آخر 7 أيام في استعلامين فقط بدل 14 استعلاماً
    week_start = today - timedelta(days=6)
    days, visits_counts, revenue_counts = [], [], []
    visit_map = dict(db.session.query(
        Visit.visit_date, db.func.count()).filter(
        Visit.visit_date >= week_start).group_by(Visit.visit_date).all())
    revenue_map = dict(db.session.query(
        Invoice.invoice_date,
        db.func.coalesce(db.func.sum(Invoice.paid_amount), 0)).filter(
        Invoice.invoice_date >= week_start).group_by(Invoice.invoice_date).all())
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        days.append(d.strftime('%a'))
        visits_counts.append(visit_map.get(d, 0))
        revenue_counts.append(float(revenue_map.get(d, 0) or 0))
    upcoming = Appointment.query.filter(
        Appointment.date >= today, Appointment.status == 'scheduled'
    ).order_by(Appointment.date, Appointment.time).limit(5).all()
    reminder_date = today + timedelta(days=2)
    reminders = Appointment.query.filter(
        Appointment.date <= reminder_date,
        Appointment.date >= today,
        Appointment.status == 'scheduled'
    ).order_by(Appointment.date, Appointment.time).all()
    # طلبات الحجز الإلكتروني الجديدة
    pending_appointments = Appointment.query.filter_by(status='pending').order_by(
        Appointment.date, Appointment.time).limit(5).all()
    # رسائل المرضى: غير المقروء + آخر الرسائل
    unread_msgs = PatientMessage.query.filter_by(
        sender='patient', is_read=False).count()
    latest_msgs = PatientMessage.query.filter_by(sender='patient').order_by(
        PatientMessage.created_at.desc()).limit(5).all()
    return render_template('dashboard.html',
                           total_patients=total_patients,
                           today_appointments=today_appointments,
                           completed_visits=completed_visits,
                           pending_invoices=pending_invoices,
                           pending_requests=pending_requests,
                           pending_appointments=pending_appointments,
                           total_revenue=total_revenue,
                           pending_balance=pending_balance,
                           days=days,
                           visits_counts=visits_counts,
                           revenue_counts=revenue_counts,
                           upcoming=upcoming,
                           reminders=reminders,
                           tomorrow_stats=get_tomorrow_confirmation_stats(),
                           unread_msgs=unread_msgs,
                           latest_msgs=latest_msgs,
                           today=today)


# ===================== المرضى =====================

@app.route('/patients')
@doctor_required
def patients_list():
    search = request.args.get('q', '')
    archived = request.args.get('archived') == '1'
    page = request.args.get('page', 1, type=int)
    query = Patient.query.filter_by(is_active=not archived)
    if search:
        like = f"%{search}%"
        query = query.filter(
            db.or_(Patient.first_name.ilike(like),
                   Patient.last_name.ilike(like),
                   Patient.phone.ilike(like),
                   Patient.code.ilike(like)))
    patients = query.order_by(Patient.created_at.desc()).paginate(
        page=page, per_page=15, error_out=False)
    return render_template('patients/list.html', patients=patients,
                           search=search, archived=archived)


@app.route('/patients/new', methods=['GET', 'POST'])
@doctor_required
def patient_new():
    if request.method == 'POST':
        patient = Patient(
            code='P00000',
            first_name=request.form['first_name'],
            last_name=request.form['last_name'],
            gender=request.form.get('gender'),
            birth_date=datetime.strptime(request.form['birth_date'], '%Y-%m-%d').date()
            if request.form.get('birth_date') else None,
            phone=request.form.get('phone'),
            email=request.form.get('email'),
            telegram_chat_id=request.form.get('telegram_chat_id', '').strip() or None,
            address=request.form.get('address'),
            job=request.form.get('job'),
            marital_status=request.form.get('marital_status'),
            emergency_contact=request.form.get('emergency_contact'),
            emergency_phone=request.form.get('emergency_phone'),
            medical_history=request.form.get('medical_history'),
            chronic_diseases=request.form.get('chronic_diseases'),
            current_medications=request.form.get('current_medications'),
            notes=request.form.get('notes'),
        )
        db.session.add(patient)
        db.session.flush()  # كود فريد مبني على المفتاح الأساسي، غير متأثر بالحذف
        patient.code = f"P{patient.id + 1000:05d}"
        log_action('create', 'patient', patient.id, patient.full_name)
        db.session.commit()
        flash('تم إضافة المريض بنجاح', 'success')
        return redirect(url_for('patient_view', id=patient.id))
    return render_template('patients/form.html', patient=None)


@app.route('/patients/<int:id>')
@doctor_required
def patient_view(id):
    patient = Patient.query.get_or_404(id)
    visits = Visit.query.filter_by(patient_id=id).order_by(
        Visit.visit_date.desc()).limit(20).all()
    images = MedicalImage.query.filter_by(patient_id=id).order_by(
        MedicalImage.upload_date.desc()).all()
    invoices = Invoice.query.filter_by(patient_id=id).order_by(
        Invoice.invoice_date.desc()).limit(20).all()
    appointments = Appointment.query.filter_by(patient_id=id).order_by(
        Appointment.date.desc()).limit(10).all()
    return render_template('patients/view.html', patient=patient, visits=visits,
                           images=images, invoices=invoices, appointments=appointments)


@app.route('/patients/<int:id>/qr')
@doctor_required
def patient_qr(id):
    """رمز QR لربط المريض غير المرتبط بالبوت.
    الرمز يحمل رابطاً عميقاً (t.me/البوت?start=كود المريض) يمسحه المريض
    بكاميرا هاتفه فيفتح البوت ويرسل أمر الربط تلقائياً دون كتابة أي أمر.
    """
    patient = Patient.query.get_or_404(id)
    if (patient.telegram_chat_id or '').strip():
        flash('هذا المريض مرتبط ببوت تيليجرام بالفعل', 'info')
        return redirect(url_for('patient_view', id=patient.id))
    try:
        import qrcode
    except ImportError:
        flash('مكتبة qrcode غير مثبتة — لتوليد رموز QR نفّذ: pip install qrcode[pil]',
              'error')
        return redirect(url_for('patient_view', id=patient.id))
    import io
    qr = qrcode.QRCode(box_size=8, border=2,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(patient_telegram_deep_link(patient))
    qr.make(fit=True)
    img = qr.make_image(fill_color='#4f46e5', back_color='white')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    resp = app.response_class(buf.getvalue(), mimetype='image/png')
    resp.headers['Cache-Control'] = 'no-store'
    if request.args.get('download'):
        resp.headers['Content-Disposition'] = (
            f'attachment; filename="qr-{patient.code}.png"')
    return resp


@app.route('/patients/<int:id>/edit', methods=['GET', 'POST'])
@doctor_required
def patient_edit(id):
    patient = Patient.query.get_or_404(id)
    if request.method == 'POST':
        patient.first_name = request.form['first_name']
        patient.last_name = request.form['last_name']
        patient.gender = request.form.get('gender')
        patient.birth_date = datetime.strptime(
            request.form['birth_date'], '%Y-%m-%d').date() if request.form.get('birth_date') else None
        patient.phone = request.form.get('phone')
        patient.email = request.form.get('email')
        patient.telegram_chat_id = request.form.get('telegram_chat_id', '').strip() or None
        patient.address = request.form.get('address')
        patient.job = request.form.get('job')
        patient.marital_status = request.form.get('marital_status')
        patient.emergency_contact = request.form.get('emergency_contact')
        patient.emergency_phone = request.form.get('emergency_phone')
        patient.medical_history = request.form.get('medical_history')
        patient.chronic_diseases = request.form.get('chronic_diseases')
        patient.current_medications = request.form.get('current_medications')
        patient.notes = request.form.get('notes')
        log_action('update', 'patient', patient.id, patient.full_name)
        db.session.commit()
        flash('تم تحديث بيانات المريض', 'success')
        return redirect(url_for('patient_view', id=patient.id))
    return render_template('patients/form.html', patient=patient)


@app.route('/patients/<int:id>/delete', methods=['POST'])
@doctor_required
def patient_delete(id):
    """أرشفة المريض بدل الحذف النهائي حفاظاً على السجل الطبي"""
    patient = Patient.query.get_or_404(id)
    patient.is_active = False
    log_action('archive', 'patient', patient.id, patient.full_name)
    db.session.commit()
    flash('تم أرشفة المريض (لم تُحذف بياناته الطبية)', 'success')
    return redirect(url_for('patients_list'))


@app.route('/patients/<int:id>/restore', methods=['POST'])
@doctor_required
def patient_restore(id):
    patient = Patient.query.get_or_404(id)
    patient.is_active = True
    log_action('restore', 'patient', patient.id, patient.full_name)
    db.session.commit()
    flash('تم استرجاع المريض من الأرشيف', 'success')
    return redirect(url_for('patient_view', id=patient.id))


@app.route('/api/search')
@doctor_required
def api_search():
    """بحث سريع عن المرضى"""
    q = request.args.get('q', '')
    if len(q) < 1:
        return jsonify([])
    like = f"%{q}%"
    patients = Patient.query.filter(
        Patient.is_active.is_(True),
        db.or_(Patient.first_name.ilike(like),
               Patient.last_name.ilike(like),
               Patient.phone.ilike(like),
               Patient.code.ilike(like))).limit(10).all()
    return jsonify([{
        'id': p.id,
        'code': p.code,
        'name': p.full_name,
        'phone': p.phone or ''
    } for p in patients])


# ===================== المواعيد =====================

# ===================== العطلات وإعادة الجدولة =====================

def _compute_resume_date(end_date, closed_days):
    """أول يوم عمل بعد end_date: نتخطى أيام الإجازة الأسبوعية."""
    d = end_date + timedelta(days=1)
    while d.weekday() in closed_days:
        d += timedelta(days=1)
    return d


def _next_work_day(d, closed_days):
    """اليوم التالي غير الإجازة الأسبوعية بعد d."""
    nd = d + timedelta(days=1)
    while nd.weekday() in closed_days:
        nd += timedelta(days=1)
    return nd


def _has_appt_at(dt_date, dt_time, duration, exclude_appt_id=None):
    """هل يوجد موعد scheduled/pending يتقاطع مع هذا التاريخ/الوقت؟"""
    end_time = (datetime.combine(dt_date, dt_time) + timedelta(minutes=duration)).time()
    appts = Appointment.query.filter_by(date=dt_date, time=dt_time).filter(
        Appointment.status.in_(['scheduled', 'pending'])).all()
    if exclude_appt_id:
        appts = [a for a in appts if a.id != exclude_appt_id]
    return len(appts) > 0


def _reschedule_for_holiday(holiday):
    """إعادة جدولة كل المواعيد (scheduled + pending) الواقعة في نطاق العطلة.
    المنطق:
    - نجمع المواعيد مرتبة حسب (date, time) — للحفاظ على ترتيب الحجز.
    - لكل يوم عطلة، نحسب اليوم المقابل له بعد الاستئناف (بالتسلسل، نتخطى الإجازات الأسبوعية).
    - مواعيد يوم N → يوم العمل N المقابل، بنفس الوقت.
    - عند التعارض (يوم الاستئناف لديه موعد سابق في نفس الوقت):
      ننقل الموعد المتعارض إلى ما بعد نهاية سلسلة الأيام المنقولة.
    - نُسجّل كل نقل في AppointmentMoveLog.
    - نُرسل إشعار لكل مريض متأثر.
    - نُرسل ملخص للطبيب.
    """
    closed_days = get_schedule()['closed_days']
    # 1) احسب يوم الاستئناف
    resume_date = _compute_resume_date(holiday.end_date, closed_days)
    holiday.resume_date = resume_date
    # 2) اجمع كل المواعيد في نطاق العطلة (scheduled + pending)
    appts = Appointment.query.filter(
        Appointment.date >= holiday.start_date,
        Appointment.date <= holiday.end_date,
        Appointment.status.in_(['scheduled', 'pending'])
    ).order_by(Appointment.date, Appointment.time).all()
    if not appts:
        holiday.moved_count = 0
        db.session.commit()
        return 0
    # 3) بنِ خريطة: كل يوم عطلة → يوم العمل المقابل بعد الاستئناف
    # نبدأ من resume_date، ولكل يوم عطلة نخصص يوم عمل (نتخطى الإجازات الأسبوعية).
    # ترتيب أيام العطلة تصاعدياً = ترتيب أيام العمل تصاعدياً.
    holiday_days = []
    d = holiday.start_date
    while d <= holiday.end_date:
        holiday_days.append(d)
        d += timedelta(days=1)
    # أيام العمل المقابلة (بالتسلسل من resume_date، نتخطى الإجازات)
    work_days = []
    wd = resume_date
    for _ in holiday_days:
        work_days.append(wd)
        wd = _next_work_day(wd, closed_days)
    # آخر يوم في السلسلة (لحل التعارضات ننقل لما بعده)
    last_work_day = work_days[-1]
    day_after_last = _next_work_day(last_work_day, closed_days)
    # 4) نقل كل موعد
    moved_count = 0
    conflict_moves = []  # المواعيد التي نُقلت بسبب التعارض
    for appt in appts:
        old_date = appt.date
        old_time = appt.time
        # ابحث عن يوم العمل المقابل ليوم العطلة
        try:
            idx = holiday_days.index(old_date)
            target_date = work_days[idx]
        except ValueError:
            # الموعد ليس في أيام العطلة المحددة (لا يجب أن يحدث) — تخطى
            continue
        target_time = old_time  # نفس الوقت
        # تحقق من التعارض (هل يوجد موعد سابق في target_date/time؟)
        conflict = _has_appt_at(target_date, target_time, appt.duration,
                                exclude_appt_id=appt.id)
        if conflict:
            # انقل إلى ما بعد نهاية السلسلة: ابحث عن أول يوم عمل فارغ لهذا الوقت
            # ابدأ من day_after_last وتقدّم حتى تجد يوماً بلا تعارض
            search_date = day_after_last
            attempts = 0
            while _has_appt_at(search_date, target_time, appt.duration,
                               exclude_appt_id=appt.id) and attempts < 60:
                search_date = _next_work_day(search_date, closed_days)
                attempts += 1
            target_date = search_date
            conflict_moves.append(appt.id)
        # طبّق النقل
        appt.date = target_date
        appt.time = target_time
        # سجّل النقل
        db.session.add(AppointmentMoveLog(
            appointment_id=appt.id, holiday_id=holiday.id,
            old_date=old_date, old_time=old_time,
            new_date=target_date, new_time=target_time,
            conflict_resolved=(appt.id in conflict_moves)))
        moved_count += 1
        # إشعار المريض بالنقل
        _notify_patient_holiday_move(appt, old_date, old_time, target_date,
                                     target_time, holiday,
                                     is_conflict=(appt.id in conflict_moves))
    holiday.moved_count = moved_count
    db.session.commit()
    # 5) ملخص للطبيب
    summary = (
        f'🏖 <b>تم ضبط عطلة ونقل المواعيد</b>\n\n'
        f'📅 العطلة: {holiday.start_date.strftime("%Y/%m/%d")} → '
        f'{holiday.end_date.strftime("%Y/%m/%d")}\n'
        f'▶ استئناف العمل: {resume_date.strftime("%Y/%m/%d")}\n'
        f'🔄 مواعيد نُقلت: {moved_count}\n'
    )
    if conflict_moves:
        summary += (f'⚠ من بينها {len(conflict_moves)} موعد نُقل لما بعد سلسلة '
                    f'الاستئناف بسبب تعارض الأوقات.\n')
    summary += '\nتم إشعار كل المرضى المتأثرين برسالة تيليجرام/بريد.'
    notify_doctor(summary, log_subject='ضبط عطلة ونقل مواعيد')
    return moved_count


def _notify_patient_holiday_move(appt, old_date, old_time, new_date, new_time,
                                 holiday, is_conflict=False):
    """إشعار مريض بنقل موعده بسبب عطلة"""
    p = appt.patient
    if not p:
        return
    clinic = clinic_name_safe()
    ticket = appt.ticket_code or '—'
    old_txt = f'{old_date.strftime("%Y/%m/%d")} على الساعة {old_time.strftime("%H:%M")}'
    new_txt = f'{new_date.strftime("%Y/%m/%d")} على الساعة {new_time.strftime("%H:%M")}'
    # قناة تيليجرام
    if get_setting('notify_telegram_enabled') == '1' and p.telegram_chat_id:
        text = (
            f'🏖 <b>تنبيه: نقل موعدك بسبب عطلة العيادة</b>\n\n'
            f'مرحباً {p.first_name or ""},\n'
            f'نود إعلامك بأن موعدك في {old_txt} نُقل إلى:\n'
            f'📅 <b>{new_txt}</b>\n\n'
        )
        if is_conflict:
            text += (
                f'نعتذر عن نقل موعدك إلى تاريخ أبعد — كان ذلك لوجود حجوزات '
                f'تم تأجيلها بسبب العطلة في نفس الوقت. هذا الإجراء كان للحفاظ '
                f'على تنظيم المواعيد السابقة والمقبلة.\n\n'
            )
        text += (
            f'السبب: عطلة العيادة من {holiday.start_date.strftime("%Y/%m/%d")} '
            f'إلى {holiday.end_date.strftime("%Y/%m/%d")}.\n'
            f'يُستأنف العمل في {holiday.resume_date.strftime("%Y/%m/%d")}.\n\n'
            f'🎟 رقم التذكرة: <code>{ticket}</code>\n'
            f'— {clinic}'
        )
        log = NotificationLog(recipient=p.telegram_chat_id, channel='telegram',
                              subject='نقل موعد بسبب عطلة', status='queued')
        db.session.add(log)
        db.session.flush()
        db.session.commit()
        threading.Thread(target=_send_telegram_worker,
                         args=(log.id, p.telegram_chat_id, text), daemon=True).start()
    # قناة البريد
    if get_setting('notify_email_enabled') == '1' and p.email and '@' in p.email:
        html = (
            f'<div dir="rtl" style="font-family:Arial;padding:20px">'
            f'<h3>🏖 نقل موعدك بسبب عطلة العيادة</h3>'
            f'<p>مرحباً {p.first_name or ""},</p>'
            f'<p>نود إعلامك بأن موعدك في <b>{old_txt}</b> نُقل إلى:</p>'
            f'<p style="font-size:18px">📅 <b>{new_txt}</b></p>'
        )
        if is_conflict:
            html += (
                f'<p style="background:#fff3cd;padding:10px;border-radius:8px">'
                f'نعتذر عن نقل موعدك إلى تاريخ أبعد — كان ذلك لوجود حجوزات '
                f'تم تأجيلها بسبب العطلة في نفس الوقت. هذا الإجراء كان للحفاظ '
                f'على تنظيم المواعيد السابقة والمقبلة.</p>'
            )
        html += (
            f'<p><b>السبب:</b> عطلة العيادة من {holiday.start_date.strftime("%Y/%m/%d")} '
            f'إلى {holiday.end_date.strftime("%Y/%m/%d")}.<br>'
            f'يُستأنف العمل في {holiday.resume_date.strftime("%Y/%m/%d")}.</p>'
            f'<p>🎟 رقم التذكرة: {ticket}</p>'
            f'<hr><small>{clinic}</small></div>'
        )
        api_key = get_setting('brevo_api_key', '')
        sender_email = get_setting('brevo_sender_email', '')
        sender_name = get_setting('brevo_sender_name', '') or clinic
        if api_key and '@' in sender_email:
            log = NotificationLog(recipient=p.email, subject='نقل موعد بسبب عطلة', status='queued')
            db.session.add(log)
            db.session.flush()
            db.session.commit()
            threading.Thread(target=_send_email_worker,
                             args=(log.id, p.email, 'نقل موعد بسبب عطلة', html),
                             daemon=True).start()


def check_and_send_holiday_resume_reminders():
    """مهمة مجدولة: قبل يوم من استئناف العمل، أرسل تذكير لكل المرضى المنقولين.
    تُنفذ يومياً (مرة واحدة لكل عطلة عبر علم notification_sent).
    """
    with app.app_context():
        try:
            today = date.today()
            # ابحث عن العطلات النشطة التي resume_date = today + 1 (غداً)
            tomorrow = today + timedelta(days=1)
            holidays = Holiday.query.filter_by(
                status='active', notification_sent=False
            ).filter(Holiday.resume_date == tomorrow).all()
            for holiday in holidays:
                # كل المواعيد المنقولة لهذه العطلة
                move_logs = AppointmentMoveLog.query.filter_by(
                    holiday_id=holiday.id).all()
                for ml in move_logs:
                    appt = ml.appointment
                    if not appt or appt.status not in ('scheduled', 'pending'):
                        continue
                    _notify_patient_resume_reminder(appt, holiday)
                holiday.notification_sent = True
                db.session.commit()
                # ملخص للطبيب
                notify_doctor(
                    f' <b>تذكير: استئناف العمل غداً</b>\n\n'
                    f'يستأنف العمل غداً {holiday.resume_date.strftime("%Y/%m/%d")}.\n'
                    f'تم إرسال تذكير لـ {len(move_logs)} مريض منقول.',
                    log_subject='تذكير استئناف العمل')
        except Exception as e:
            print(f'خطأ في تذكير استئناف العمل: {e}')


def _notify_patient_resume_reminder(appt, holiday):
    """تذكير مريض باستئناف العمل غداً + موعده الجديد"""
    p = appt.patient
    if not p:
        return
    clinic = clinic_name_safe()
    new_txt = f'{appt.date.strftime("%Y/%m/%d")} على الساعة {appt.time.strftime("%H:%M")}'
    if get_setting('notify_telegram_enabled') == '1' and p.telegram_chat_id:
        text = (
            f' <b>تذكير: استئناف عمل العيادة غداً</b>\n\n'
            f'مرحباً {p.first_name or ""},\n'
            f'نُذكرك بأن العيادة تستأنف عملها غداً '
            f'{holiday.resume_date.strftime("%Y/%m/%d")}.\n'
            f'موعدك الجديد: 📅 <b>{new_txt}</b>\n'
            f'🎟 رقم التذكرة: <code>{appt.ticket_code or "—"}</code>\n\n'
            f'— {clinic}'
        )
        log = NotificationLog(recipient=p.telegram_chat_id, channel='telegram',
                              subject='تذكير استئناف العمل', status='queued')
        db.session.add(log)
        db.session.flush()
        db.session.commit()
        threading.Thread(target=_send_telegram_worker,
                         args=(log.id, p.telegram_chat_id, text), daemon=True).start()


@app.route('/holidays')
@doctor_required
def holidays_list():
    """قائمة العطلات (النشطة + السابقة)"""
    today = date.today()
    # تحديث حالة العطلات المنتهية تلقائياً
    Holiday.query.filter(
        Holiday.status == 'active',
        Holiday.end_date < today
    ).update({'status': 'past'})
    db.session.commit()
    active = Holiday.query.filter_by(status='active').order_by(
        Holiday.start_date).all()
    past = Holiday.query.filter_by(status='past').order_by(
        Holiday.end_date.desc()).all()
    return render_template('holidays/list.html', active=active, past=past, today=today)


@app.route('/holidays/new', methods=['GET', 'POST'])
@doctor_required
def holiday_new():
    """إنشاء عطلة جديدة وإعادة جدولة المواعيد المتأثرة"""
    if request.method == 'POST':
        start_str = (request.form.get('start_date') or '').strip()
        end_str = (request.form.get('end_date') or '').strip()
        days_count = (request.form.get('days_count') or '').strip()
        reason = (request.form.get('reason') or '').strip()
        # طريقتان: (أ) تاريخ بداية + نهاية، (ب) تاريخ بداية + عدد أيام
        try:
            start_date = datetime.strptime(start_str, '%Y-%m-%d').date()
        except ValueError:
            flash('تاريخ بداية العطلة غير صحيح', 'error')
            return redirect(url_for('holiday_new'))
        if end_str:
            try:
                end_date = datetime.strptime(end_str, '%Y-%m-%d').date()
            except ValueError:
                flash('تاريخ نهاية العطلة غير صحيح', 'error')
                return redirect(url_for('holiday_new'))
        elif days_count:
            try:
                n = int(days_count)
                if n < 1:
                    raise ValueError
                end_date = start_date + timedelta(days=n - 1)
            except ValueError:
                flash('عدد أيام العطلة يجب أن يكون رقماً موجباً', 'error')
                return redirect(url_for('holiday_new'))
        else:
            flash('حدّد تاريخ نهاية العطلة أو عدد الأيام', 'error')
            return redirect(url_for('holiday_new'))
        if end_date < start_date:
            flash('تاريخ النهاية يجب أن يكون بعد (أو يساوي) تاريخ البداية', 'error')
            return redirect(url_for('holiday_new'))
        if start_date < today_date():
            flash('لا يمكن ضبط عطلة في الماضي', 'error')
            return redirect(url_for('holiday_new'))
        # تحقق من عدم التداخل مع عطلة نشطة أخرى
        overlap = Holiday.query.filter(
            Holiday.status == 'active',
            Holiday.start_date <= end_date,
            Holiday.end_date >= start_date
        ).first()
        if overlap:
            flash(f'تتداخل هذه العطلة مع عطلة نشطة أخرى ({overlap.start_date} → {overlap.end_date}). '
                  'احذف أو عدّل العطلة السابقة أولاً.', 'error')
            return redirect(url_for('holiday_new'))
        # أنشئ العطلة وأعد الجدولة
        closed_days = get_schedule()['closed_days']
        resume_date = _compute_resume_date(end_date, closed_days)
        holiday = Holiday(start_date=start_date, end_date=end_date,
                          resume_date=resume_date, reason=reason)
        db.session.add(holiday)
        db.session.flush()  # للحصول على holiday.id
        moved = _reschedule_for_holiday(holiday)
        db.session.commit()
        flash(f'✅ تم ضبط العطلة ({start_date} → {end_date}). نُقل {moved} موعد. '
              'تم إشعار المرضى والطبيب.', 'success')
        return redirect(url_for('holidays_list'))
    return render_template('holidays/form.html', today=today_date())


@app.route('/holidays/<int:holiday_id>/moves')
@doctor_required
def holiday_moves(holiday_id):
    """عرض سجل نقل المواعيد لعطلة معينة"""
    holiday = db.session.get(Holiday, holiday_id)
    if not holiday:
        abort(404)
    moves = AppointmentMoveLog.query.filter_by(holiday_id=holiday_id).order_by(
        AppointmentMoveLog.new_date, AppointmentMoveLog.new_time).all()
    return render_template('holidays/moves.html', holiday=holiday, moves=moves)


@app.route('/holidays/<int:holiday_id>/delete', methods=['POST'])
@doctor_required
def holiday_delete(holiday_id):
    """حذف عطلة — يحذف سجلات النقل المرتبطة فقط (لا يُعكس النقل تلقائياً)"""
    holiday = db.session.get(Holiday, holiday_id)
    if not holiday:
        abort(404)
    AppointmentMoveLog.query.filter_by(holiday_id=holiday_id).delete()
    db.session.delete(holiday)
    db.session.commit()
    flash('تم حذف العطلة وسجل نقل مواعيدها. (المواعيد المنقولة تبقى كما هي — '
          'يمكنك تعديلها يدوياً من تبويب المواعيد.)', 'success')
    return redirect(url_for('holidays_list'))


def today_date():
    """مساعد بسيط (لتجنب استيراد date في كل مكان)"""
    return date.today()


@app.route('/appointments')
@doctor_required
def appointments_list():
    """المواعيد الأسبوعية: تعرض مواعيد 7 أيام متتالية ابتداءً من اليوم.
    لا يُعرض أي يوم ماضٍ — إذا حاول المستخدم فتح تاريخ سابق، يُعامل كـ «اليوم».
    الـ param `date` يحدّد بداية الأسبوع (افتراضياً اليوم)؛ `prev`/`next` للتنقل
    بين الأسابيع محفوظة عبر روابط في القالب.
    """
    today = date.today()
    selected_date = request.args.get('date')
    if selected_date:
        try:
            start_date = datetime.strptime(selected_date, '%Y-%m-%d').date()
        except ValueError:
            start_date = today
    else:
        start_date = today
    # منع عرض أيام ماضية: إذا كان تاريخ البداية قبل اليوم، ابدأ من اليوم
    if start_date < today:
        start_date = today
    end_date = start_date + timedelta(days=6)  # 7 أيام متتالية
    # جلب كل المواعيد في النطاق (يوم واحد = الموعد يبدأ في ذلك اليوم)
    appointments = Appointment.query.filter(
        Appointment.date >= start_date,
        Appointment.date <= end_date
    ).order_by(Appointment.date, Appointment.time).all()
    # تجميع المواعيد حسب اليوم (لعرض كل يوم في قسم منفصل)
    days = []
    for i in range(7):
        d = start_date + timedelta(days=i)
        day_appts = [a for a in appointments if a.date == d]
        days.append({
            'date': d,
            'weekday': WEEKDAY_NAMES.get(d.weekday(), ''),
            'is_today': d == today,
            'appointments': day_appts,
            'count': len(day_appts),
        })
    return render_template('appointments/list.html', days=days,
                           appointments=appointments,  # للتوافق مع أي منطق قديم
                           selected_date=start_date, today=today,
                           week_start=start_date, week_end=end_date,
                           prev_week=start_date - timedelta(days=7),
                           next_week=start_date + timedelta(days=7),
                           statuses=APPOINTMENT_STATUSES)


@app.route('/appointments/new', methods=['GET', 'POST'])
@app.route('/appointments/new/<int:patient_id>', methods=['GET', 'POST'])
@doctor_required
def appointment_new(patient_id=None):
    if request.method == 'POST':
        try:
            d = datetime.strptime(request.form['date'], '%Y-%m-%d').date()
            t = datetime.strptime(request.form['time'], '%H:%M').time()
        except (KeyError, ValueError):
            flash('يرجى إدخال تاريخ ووقت صحيحين', 'error')
            patients = Patient.query.filter_by(is_active=True).order_by(
                Patient.first_name).all()
            return render_template('appointments/form.html', patients=patients,
                                   preselect=patient_id, today=date.today())
        duration = int(request.form.get('duration', 60) or 60)
        conflict = has_time_conflict(d, t, duration)
        if conflict:
            flash(f'تعارض مع موعد آخر في نفس الوقت ({conflict.time.strftime("%H:%M")})، '
                  'يرجى اختيار وقت مختلف', 'error')
        else:
            appt = Appointment(
                patient_id=request.form['patient_id'], date=d, time=t,
                duration=duration, reason=request.form.get('reason'),
                notes=request.form.get('notes'), status='scheduled',
                source='doctor',
            )
            db.session.add(appt)
            log_action('create', 'appointment', appt.id, f'حجز من الطبيب {d} {t}')
            db.session.commit()
            flash('تم حجز الموعد بنجاح', 'success')
            return redirect(url_for('appointments_list', date=d.strftime('%Y-%m-%d')))
    patients = Patient.query.filter_by(is_active=True).order_by(
        Patient.first_name).all()
    preselect = patient_id
    return render_template('appointments/form.html', patients=patients,
                           preselect=preselect, today=date.today())


@app.route('/appointments/<int:id>/status', methods=['POST'])
@doctor_required
def appointment_status(id):
    appt = Appointment.query.get_or_404(id)
    status = request.form.get('status', 'scheduled')
    if status not in APPOINTMENT_STATUSES:
        status = 'scheduled'
    old_status = appt.status
    appt.status = status
    log_action('update', 'appointment', appt.id, f'تغيير الحالة إلى {status}')
    db.session.commit()
    # إشعار المريض عند التأكيد أو الإلغاء فقط
    if status != old_status:
        if status == 'scheduled':
            queue_appointment_notification(appt, 'confirmed')
        elif status == 'cancelled':
            queue_appointment_notification(appt, 'cancelled')
    flash('تم تحديث حالة الموعد', 'success')
    return redirect(url_for('appointments_list', date=appt.date.strftime('%Y-%m-%d')))


@app.route('/appointments/<int:id>/edit', methods=['GET', 'POST'])
@doctor_required
def appointment_edit(id):
    """تغيير موعد: التاريخ والوقت والمدة (صلاحية الطبيب حسب جدول المواعيد)"""
    appt = Appointment.query.get_or_404(id)
    if request.method == 'POST':
        try:
            d = datetime.strptime(request.form['date'], '%Y-%m-%d').date()
            t = datetime.strptime(request.form['time'], '%H:%M').time()
        except (KeyError, ValueError):
            flash('يرجى إدخال تاريخ ووقت صحيحين', 'error')
            return render_template('appointments/edit.html', appt=appt,
                                   statuses=APPOINTMENT_STATUSES)
        duration = int(request.form.get('duration', appt.duration or 60) or 60)
        conflict = has_time_conflict(d, t, duration, exclude_id=appt.id)
        if conflict:
            flash(f'تعارض مع موعد آخر في نفس الوقت ({conflict.time.strftime("%H:%M")})',
                  'error')
        else:
            old_dt = datetime.combine(appt.date, appt.time)
            time_changed = (appt.date, appt.time) != (d, t)
            appt.date, appt.time, appt.duration = d, t, duration
            if request.form.get('transport_fee') is not None and appt.is_special:
                appt.transport_fee = to_decimal(request.form.get('transport_fee'))
            log_action('update', 'appointment', appt.id,
                       f'تغيير الموعد إلى {d} {t.strftime("%H:%M")}')
            db.session.commit()
            if time_changed:
                # إشعار المريض بتغيير الموعد
                queue_appointment_notification(appt, 'time_changed', old_dt=old_dt)
            flash('تم تعديل الموعد بنجاح', 'success')
            return redirect(url_for('appointments_list', date=d.strftime('%Y-%m-%d')))
    return render_template('appointments/edit.html', appt=appt,
                           statuses=APPOINTMENT_STATUSES)


@app.route('/appointments/<int:id>/delete', methods=['POST'])
@doctor_required
def appointment_delete(id):
    appt = Appointment.query.get_or_404(id)
    appt_date = appt.date
    had_ticket = bool(appt.ticket_code)
    # إشعار المريض بالإلغاء قبل حذف السجل (يُرسل في خيط منفصل)
    if appt.status in ('pending', 'scheduled'):
        queue_appointment_notification(appt, 'cancelled')
    log_action('delete', 'appointment', appt.id, f'حذف موعد {appt_date}')
    db.session.delete(appt)
    db.session.commit()
    flash('تم حذف الموعد' + (' وأُبلغ المريض إن كان له بريد مسجل' if had_ticket else ''),
          'success')
    return redirect(url_for('appointments_list', date=appt_date.strftime('%Y-%m-%d')))


@app.route('/appointments/reminders')
@doctor_required
def reminders():
    """صفحة التذكيرات التلقائية"""
    today = date.today()
    soon = today + timedelta(days=3)
    upcoming = Appointment.query.filter(
        Appointment.date >= today,
        Appointment.date <= soon,
        Appointment.status.in_(['scheduled', 'pending'])
    ).order_by(Appointment.date, Appointment.time).all()
    return render_template('appointments/reminders.html', appointments=upcoming,
                           today=today)


@app.route('/appointments/<int:id>/send_reminder', methods=['POST'])
@doctor_required
def send_reminder(id):
    """إرسال تذكير: عبر بوت تيليجرام إن كان المريض مرتبطاً، وإلا تسجيل يدوي"""
    appt = Appointment.query.get_or_404(id)
    if (get_setting('notify_telegram_enabled') == '1'
            and (appt.patient.telegram_chat_id or '').strip()):
        ok = send_appointment_reminder(appt)
        flash('تم إرسال تذكير تيليجرام للمريض: ' + appt.patient.full_name if ok
              else 'فشل إرسال تذكير تيليجرام، تحقق من سجل الإشعارات',
              'success' if ok else 'error')
    else:
        appt.reminder_sent = True
        db.session.commit()
        flash(f'تم تسجيل إرسال تذكير للمريض: {appt.patient.full_name}', 'success')
    return redirect(url_for('reminders'))


# ===================== الزيارات =====================

@app.route('/visits')
@doctor_required
def visits_list():
    page = request.args.get('page', 1, type=int)
    visits = Visit.query.order_by(Visit.visit_date.desc()).paginate(
        page=page, per_page=15, error_out=False)
    return render_template('visits/list.html', visits=visits)


@app.route('/visits/new', methods=['GET', 'POST'])
@app.route('/visits/new/<int:patient_id>', methods=['GET', 'POST'])
@doctor_required
def visit_new(patient_id=None):
    if request.method == 'POST':
        visit = Visit(
            patient_id=request.form['patient_id'],
            appointment_id=request.form.get('appointment_id') or None,
            visit_date=datetime.strptime(request.form['visit_date'], '%Y-%m-%d').date()
            if request.form.get('visit_date') else date.today(),
            chief_complaint=request.form.get('chief_complaint'),
            diagnosis=request.form.get('diagnosis'),
            treatment=request.form.get('treatment'),
            prescription=request.form.get('prescription'),
            session_notes=request.form.get('session_notes'),
            mood_assessment=request.form.get('mood_assessment'),
            next_appointment=datetime.strptime(
                request.form['next_appointment'], '%Y-%m-%d').date()
            if request.form.get('next_appointment') else None,
            fee=to_decimal(request.form.get('fee', 0)),
        )
        db.session.add(visit)
        if visit.appointment_id:
            appt = Appointment.query.get(visit.appointment_id)
            if appt:
                appt.status = 'completed'
        log_action('create', 'visit', visit.id)
        db.session.commit()
        flash('تم تسجيل الزيارة بنجاح', 'success')
        return redirect(url_for('patient_view', id=visit.patient_id))
    patients = Patient.query.filter_by(is_active=True).order_by(
        Patient.first_name).all()
    return render_template('visits/form.html', patients=patients,
                           preselect=patient_id, today=date.today())


@app.route('/visits/<int:id>')
@doctor_required
def visit_view(id):
    visit = Visit.query.get_or_404(id)
    return render_template('visits/view.html', visit=visit)


@app.route('/visits/<int:id>/delete', methods=['POST'])
@doctor_required
def visit_delete(id):
    visit = Visit.query.get_or_404(id)
    pid = visit.patient_id
    log_action('delete', 'visit', visit.id)
    db.session.delete(visit)
    db.session.commit()
    flash('تم حذف الزيارة', 'success')
    return redirect(url_for('patient_view', id=pid))


# ===================== الأشعات والصور =====================

@app.route('/imaging')
@doctor_required
def imaging_list():
    images = MedicalImage.query.order_by(MedicalImage.upload_date.desc()).all()
    return render_template('imaging/list.html', images=images)


@app.route('/imaging/new', methods=['GET', 'POST'])
@app.route('/imaging/new/<int:patient_id>', methods=['GET', 'POST'])
@doctor_required
def imaging_new(patient_id=None):
    if request.method == 'POST':
        file = request.files.get('file')
        if not file or file.filename == '':
            flash('يرجى اختيار ملف', 'error')
            return redirect(request.url)
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            ts = datetime.now().strftime('%Y%m%d%H%M%S')
            filename = f"{ts}{secrets.token_hex(4)}{filename}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            image = MedicalImage(
                patient_id=request.form['patient_id'],
                filename=filename,
                original_name=file.filename,
                file_path=filename,
                file_type=request.form.get('file_type'),
                description=request.form.get('description'),
            )
            db.session.add(image)
            log_action('create', 'medical_image', image.id, file.filename)
            db.session.commit()
            flash('تم رفع الملف بنجاح', 'success')
            return redirect(url_for('patient_view', id=image.patient_id))
        flash('نوع الملف غير مدعوم', 'error')
        return redirect(request.url)
    patients = Patient.query.filter_by(is_active=True).order_by(
        Patient.first_name).all()
    return render_template('imaging/form.html', patients=patients, preselect=patient_id)


@app.route('/uploads/<filename>')
@doctor_required
def uploaded_file(filename):
    """الملفات الطبية متاحة للطبيب المسجل فقط"""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/imaging/<int:id>/delete', methods=['POST'])
@doctor_required
def imaging_delete(id):
    img = MedicalImage.query.get_or_404(id)
    pid = img.patient_id
    try:
        os.remove(os.path.join(app.config['UPLOAD_FOLDER'], img.file_path))
    except OSError:
        pass
    log_action('delete', 'medical_image', img.id, img.original_name)
    db.session.delete(img)
    db.session.commit()
    flash('تم حذف الملف', 'success')
    return redirect(url_for('patient_view', id=pid))


# ===================== الفواتير =====================

@app.route('/billing')
@doctor_required
def billing_list():
    status = request.args.get('status')
    page = request.args.get('page', 1, type=int)
    query = Invoice.query
    if status and status != 'all':
        query = query.filter_by(status=status)
    invoices = query.order_by(Invoice.invoice_date.desc()).paginate(
        page=page, per_page=15, error_out=False)
    # الإجماليات على كل النتائج المفلترة (وليس الصفحة الحالية فقط)
    base = Invoice.query
    if status and status != 'all':
        base = base.filter_by(status=status)
    totals = base.with_entities(
        db.func.coalesce(db.func.sum(Invoice.amount), 0),
        db.func.coalesce(db.func.sum(Invoice.paid_amount), 0),
    ).one()
    total_amount, total_paid = totals
    total_balance = (total_amount or 0) - (total_paid or 0)
    return render_template('billing/list.html', invoices=invoices,
                           status=status or 'all',
                           total_amount=total_amount or 0, total_paid=total_paid or 0,
                           total_balance=total_balance or 0)


@app.route('/billing/new', methods=['GET', 'POST'])
@app.route('/billing/new/<int:patient_id>', methods=['GET', 'POST'])
@doctor_required
def billing_new(patient_id=None):
    if request.method == 'POST':
        amount = to_decimal(request.form.get('amount', 0))
        paid = to_decimal(request.form.get('paid_amount', 0))
        status = 'paid' if paid >= amount else ('partial' if paid > 0 else 'unpaid')
        invoice = Invoice(
            patient_id=request.form['patient_id'],
            invoice_number='TEMP',
            invoice_date=datetime.strptime(request.form['invoice_date'], '%Y-%m-%d').date()
            if request.form.get('invoice_date') else date.today(),
            description=request.form.get('description'),
            amount=amount,
            paid_amount=paid,
            status=status,
            payment_method=request.form.get('payment_method'),
            notes=request.form.get('notes'),
        )
        db.session.add(invoice)
        db.session.flush()  # رقم فاتورة فريد مبني على المفتاح الأساسي
        invoice.invoice_number = f"INV-{invoice.id:05d}"
        log_action('create', 'invoice', invoice.id, str(invoice.invoice_number))
        db.session.commit()
        flash('تم إنشاء الفاتورة بنجاح', 'success')
        return redirect(url_for('billing_view', id=invoice.id))
    patients = Patient.query.filter_by(is_active=True).order_by(
        Patient.first_name).all()
    return render_template('billing/form.html', patients=patients, preselect=patient_id,
                           today=date.today())


@app.route('/billing/<int:id>')
@doctor_required
def billing_view(id):
    invoice = Invoice.query.get_or_404(id)
    return render_template('billing/view.html', invoice=invoice)


@app.route('/billing/<int:id>/pay', methods=['POST'])
@doctor_required
def billing_pay(id):
    invoice = Invoice.query.get_or_404(id)
    pay = to_decimal(request.form.get('payment', 0))
    invoice.paid_amount = (invoice.paid_amount or 0) + pay
    if invoice.paid_amount >= invoice.amount:
        invoice.status = 'paid'
    elif invoice.paid_amount > 0:
        invoice.status = 'partial'
    invoice.payment_method = request.form.get('payment_method', invoice.payment_method)
    log_action('pay', 'invoice', invoice.id, f'دفعة {pay}')
    db.session.commit()
    flash('تم تسجيل الدفعة', 'success')
    return redirect(url_for('billing_view', id=invoice.id))


@app.route('/billing/<int:id>/delete', methods=['POST'])
@doctor_required
def billing_delete(id):
    invoice = Invoice.query.get_or_404(id)
    log_action('delete', 'invoice', invoice.id, invoice.invoice_number)
    db.session.delete(invoice)
    db.session.commit()
    flash('تم حذف الفاتورة', 'success')
    return redirect(url_for('billing_list'))


# ===================== الإعدادات =====================

_tg_webhook_cache = {'t': 0.0, 'value': (False, 'لم يفحص بعد')}


def _get_webhook_info_cached(max_age=60):
    """getWebhookInfo مع ذاكرة مؤقتة قصيرة حتى لا يؤخر تحميل صفحة الإعدادات"""
    if _time.time() - _tg_webhook_cache['t'] > max_age:
        _tg_webhook_cache['value'] = telegram_api_call('getWebhookInfo')
        _tg_webhook_cache['t'] = _time.time()
    return _tg_webhook_cache['value']


@app.route('/settings', methods=['GET', 'POST'])
@doctor_required
def settings():
    if request.method == 'POST':
        set_setting('clinic_name', request.form.get('clinic_name', ''))
        set_setting('doctor_name', request.form.get('doctor_name', ''))
        set_setting('clinic_phone', request.form.get('clinic_phone', ''))
        set_setting('clinic_address', request.form.get('clinic_address', ''))
        set_setting('default_fee', request.form.get('default_fee', '0'))
        set_setting('work_start', request.form.get('work_start', '09:00'))
        set_setting('work_end', request.form.get('work_end', '17:00'))
        set_setting('slot_minutes', request.form.get('slot_minutes', '60'))
        set_setting('max_per_day', request.form.get('max_per_day', '8'))
        set_setting('transport_fee', request.form.get('transport_fee', '0'))
        closed = request.form.getlist('closed_days')
        set_setting('closed_days', ','.join(closed) if closed else '-1')
        set_setting('notify_email_enabled', '1' if request.form.get('notify_email_enabled') else '0')
        set_setting('brevo_api_key', request.form.get('brevo_api_key', '').strip())
        set_setting('brevo_sender_email', request.form.get('brevo_sender_email', '').strip())
        set_setting('brevo_sender_name', request.form.get('brevo_sender_name', '').strip())
        set_setting('notify_telegram_enabled',
                    '1' if request.form.get('notify_telegram_enabled') else '0')
        set_setting('telegram_bot_token', request.form.get('telegram_bot_token', '').strip())
        set_setting('telegram_bot_username', request.form.get('telegram_bot_username', '').strip())
        set_setting('telegram_reminder_hour', request.form.get('telegram_reminder_hour', '18').strip() or '18')
        db.session.commit()
        flash('تم حفظ الإعدادات', 'success')
        return redirect(url_for('settings'))
    # تشخيص بوت تيليجرام: حالة الويبهوك + عدد المربوطين + آخر أخطاء الإرسال
    webhook_ok, webhook_info = _get_webhook_info_cached()
    info = webhook_info if webhook_ok and isinstance(webhook_info, dict) else {}
    tg_diag = {
        'bot_username': telegram_bot_username(),
        'token_is_default': not get_setting('telegram_bot_token', ''),
        'webhook_ok': bool(webhook_ok),
        'webhook_url': info.get('url') or '',
        'webhook_pending': info.get('pending_update_count') or 0,
        'webhook_error': (info.get('last_error_message') if webhook_ok
                          else (webhook_info if not webhook_ok else '')) or '',
        'linked_count': Patient.query.filter(
            Patient.telegram_chat_id.isnot(None),
            Patient.telegram_chat_id != '').count(),
        'recent_errors': NotificationLog.query.filter_by(
            channel='telegram', status='failed').order_by(
            NotificationLog.created_at.desc()).limit(5).all(),
    }
    user = User.query.get(session['doctor_id'])
    return render_template('settings.html',
                           clinic_name=get_setting('clinic_name', 'عيادة الأخصائي النفساني'),
                           doctor_name=get_setting('doctor_name', ''),
                           clinic_phone=get_setting('clinic_phone', ''),
                           clinic_address=get_setting('clinic_address', ''),
                           default_fee=get_setting('default_fee', '0'),
                           work_start=get_setting('work_start', '09:00'),
                           work_end=get_setting('work_end', '17:00'),
                           slot_minutes=get_setting('slot_minutes', '60'),
                           max_per_day=get_setting('max_per_day', '8'),
                           transport_fee=get_setting('transport_fee', '0'),
                           closed_days=get_schedule()['closed_days'],
                           weekday_names=WEEKDAY_NAMES,
                           user=user,
                           notify_email_enabled=get_setting('notify_email_enabled') == '1',
                           brevo_api_key=get_setting('brevo_api_key', ''),
                           brevo_sender_email=get_setting('brevo_sender_email', ''),
                           brevo_sender_name=get_setting('brevo_sender_name', ''),
                           notify_telegram_enabled=get_setting('notify_telegram_enabled') == '1',
                           telegram_bot_token=get_setting('telegram_bot_token', ''),
                           telegram_bot_username=telegram_bot_username(),
                           telegram_token_is_default=not get_setting('telegram_bot_token', ''),
                           telegram_reminder_hour=get_setting('telegram_reminder_hour', '18'),
                           telegram_bot_link=f"https://t.me/{telegram_bot_username()}",
                           tg_diag=tg_diag,
                           doctor_telegram_chat_id=get_setting('doctor_telegram_chat_id', ''),
                           doctor_report_enabled=get_setting('doctor_report_enabled') == '1')


@app.route('/settings/test_email', methods=['POST'])
@doctor_required
def test_email():
    to_email = request.form.get('test_email', '').strip()
    if '@' not in to_email:
        flash('أدخل بريداً إلكترونياً صحيحاً للاختبار', 'error')
        return redirect(url_for('settings'))
    ok, err = send_test_email(to_email)
    db.session.add(NotificationLog(recipient=to_email, subject='رسالة تجريبية',
                                   status='sent' if ok else 'failed', error=err))
    db.session.commit()
    flash('تم إرسال البريد التجريبي بنجاح، تحقق من صندوق الوصول' if ok
          else f'فشل إرسال البريد التجريبي: {err}', 'success' if ok else 'error')
    return redirect(url_for('settings'))


@app.route('/settings/test_telegram', methods=['POST'])
@doctor_required
def test_telegram():
    """رسالة تيليجرام تجريبية بذكاء: تقبل chat_id رقمي أو كود مريض P... أو رقم تذكرة TK-...
    التذكرة تترجم تلقائيا إلى مريضها عبر الموعد، وكود المريض يستخدم الربط المحفوظ،
    وأي إدخال آخر يرفض برسالة عربية واضحة قبل استدعاء API تيليجرام.
    """
    value = (request.form.get('test_chat') or '').strip()
    upper = value.upper()
    if not value:
        flash('أدخل chat_id أو كود مريض أو رقم تذكرة أولاً', 'error')
        return redirect(url_for('settings'))
    patient = None
    if upper.startswith('P') and upper[1:].isdigit():
        # كود مريض: نستخدم الربط المحفوظ لديه
        patient = Patient.query.filter_by(code=upper).first()
        if not patient:
            flash(f'لا يوجد مريض بالكود {upper} في النظام', 'error')
            return redirect(url_for('settings'))
    elif upper.startswith('TK'):
        # رقم تذكرة: نصله إلى مريضه عبر الموعد
        appt = Appointment.query.filter_by(ticket_code=upper).order_by(
            Appointment.id.desc()).first()
        if not appt:
            flash(f'لا يوجد حجز برقم التذكرة {upper}', 'error')
            return redirect(url_for('settings'))
        patient = appt.patient
    elif not (value.lstrip('-').isdigit() or value.startswith('@')):
        flash('إدخال غير مفهوم: أدخل chat_id رقمي أو كود مريض مثل P01001 '
              'أو رقم تذكرة مثل TK-CCA520', 'error')
        return redirect(url_for('settings'))
    if patient is not None:
        label = f'{patient.full_name} ({patient.code})'
        chat_id = (patient.telegram_chat_id or '').strip()
        if not chat_id:
            db.session.add(NotificationLog(recipient=None, channel='telegram',
                                           subject='رسالة تجريبية',
                                           status='skipped',
                                           error=f'{label} غير مرتبط بالبوت'))
            db.session.commit()
            flash(f'المريض {label} غير مرتبط ببوت تيليجرام بعد — '
                  f'اطلب منه فتح البوت وإرسال /start {patient.code}', 'error')
            return redirect(url_for('settings'))
    else:
        label = ''
        chat_id = value
    ok, err = send_test_telegram(chat_id)
    db.session.add(NotificationLog(recipient=chat_id, channel='telegram',
                                   subject='رسالة تجريبية',
                                   status='sent' if ok else 'failed', error=err))
    db.session.commit()
    dest = f'إلى {label}' if label else f'إلى {chat_id}'
    if ok:
        flash(f'تم إرسال رسالة تيليجرام التجريبية بنجاح {dest}, '
              'تحقق من محادثة البوت', 'success')
    else:
        hint = _telegram_error_hint(err)
        flash(f'فشل إرسال رسالة تيليجرام: {err}'
              + (f' — {hint}' if hint else ''), 'error')
    return redirect(url_for('settings'))


@app.route('/settings/doctor_chat', methods=['POST'])
@doctor_required
def save_doctor_chat():
    """حفظ chat_id الطبيب وتفعيل التقرير الصباحي فقط (دون مس بقية الإعدادات)."""
    set_setting('doctor_telegram_chat_id',
                (request.form.get('doctor_telegram_chat_id') or '').strip())
    set_setting('doctor_report_enabled',
                '1' if request.form.get('doctor_report_enabled') else '0')
    db.session.commit()
    flash('تم حفظ إعدادات إشعارات الطبيب', 'success')
    return redirect(url_for('settings'))


# ===================== إعدادات المساعد الذكي =====================

@app.route('/settings/assistant', methods=['GET', 'POST'])
@doctor_required
def assistant_settings():
    """صفحة إعدادات المساعد الذكي — إدخال مفاتيح API واختبارها."""
    import ai_client
    # سجّل DB getter مرة واحدة (ليقرأ ai_client المفاتيح من DB)
    ai_client.set_db_getter(get_setting)
    if request.method == 'POST':
        # حفظ المفاتيح في DB
        groq_key = (request.form.get('groq_api_key') or '').strip()
        gemini_key = (request.form.get('gemini_api_key') or '').strip()
        zai_key = (request.form.get('zai_api_key') or '').strip()
        cerebras_key = (request.form.get('cerebras_api_key') or '').strip()
        openrouter_key = (request.form.get('openrouter_api_key') or '').strip()
        preferred = (request.form.get('preferred_provider') or '').strip()
        set_setting('ai_groq_api_key', groq_key)
        set_setting('ai_gemini_api_key', gemini_key)
        set_setting('ai_zai_api_key', zai_key)
        set_setting('ai_cerebras_api_key', cerebras_key)
        set_setting('ai_openrouter_api_key', openrouter_key)
        set_setting('ai_preferred_provider', preferred)
        db.session.commit()
        flash('تم حفظ مفاتيح المساعد الذكي', 'success')
        return redirect(url_for('assistant_settings'))
    # GET: اعرض الصفحة مع المفاتيح الحالية
    return render_template('assistant_settings.html',
                           groq_api_key=get_setting('ai_groq_api_key', ''),
                           gemini_api_key=get_setting('ai_gemini_api_key', ''),
                           zai_api_key=get_setting('ai_zai_api_key', ''),
                           cerebras_api_key=get_setting('ai_cerebras_api_key', ''),
                           openrouter_api_key=get_setting('ai_openrouter_api_key', ''),
                           preferred_provider=get_setting('ai_preferred_provider', ''),
                           providers_status=ai_client.get_status(),
                           any_configured=ai_client.is_configured())


@app.route('/settings/assistant/test', methods=['POST'])
@doctor_required
def assistant_test_provider():
    """اختبار مفتاح مزوّد معين."""
    import ai_client
    ai_client.set_db_getter(get_setting)
    provider_name = (request.form.get('provider') or '').strip()
    api_key = (request.form.get('api_key') or '').strip() or None
    result = ai_client.test_provider(provider_name, api_key)
    return jsonify(result)


@app.route('/settings/detect_doctor_chat', methods=['POST'])
@doctor_required
def detect_doctor_chat():
    """يكشف chat_id للطبيب تلقائياً من آخر تحديثات البوت (getUpdates).
    يطلب من الطبيب إرسال أي رسالة للبوت أولاً، ثم الضغط على الزر.
    إذا وُجد تحديث، يضبط doctor_telegram_chat_id تلقائياً ويعرضه.
    يتطلب ألا يكون webhook مفعّلاً (وإلا getUpdates يرجع 409).
    """
    ok, result = telegram_api_call('getUpdates', {'limit': 10, 'timeout': 0})
    if not ok:
        hint = _telegram_error_hint(result)
        flash(f'تعذّر جلب تحديثات البوت: {result}'
              + (f' — {hint}' if hint else '')
              + ' (إذا كان webhook مفعّلاً، عطّله مؤقتاً أو استخدم @userinfobot)',
              'error')
        return redirect(url_for('settings'))
    updates = result if isinstance(result, list) else []
    chat_id = None
    first_name = ''
    for upd in reversed(updates):
        msg = upd.get('message') or upd.get('edited_message') or {}
        chat = msg.get('chat') or {}
        if chat.get('id'):
            chat_id = str(chat['id'])
            first_name = ((msg.get('from') or {}).get('first_name')) or ''
            break
    if not chat_id:
        flash('لم يُعثر على أي رسالة في تحديثات البوت. '
              'أرسل أي رسالة (مثل "مرحبا") لبوت العيادة من حسابك في تيليجرام, '
              'ثم اضغط هذا الزر مرة أخرى. '
              '(إذا كان webhook مفعّلاً، عطّله أولاً.)', 'error')
        return redirect(url_for('settings'))
    set_setting('doctor_telegram_chat_id', chat_id)
    db.session.commit()
    flash(f'✅ تم ضبط chat_id الطبيب تلقائياً: <code>{chat_id}</code>'
          + (f' (الاسم: {first_name})' if first_name else '')
          + ' — أرسلنا لك رسالة اختبار للتأكد.', 'success')
    notify_doctor(
        f'👋 <b>تم ربط chat_id الطبيب بنجاح</b>\n\n'
        f'من الآن ستصلك الإشعارات والتقارير هنا.\n'
        f'<i>— بوت {clinic_name_safe()}</i>',
        log_subject='تأكيد ربط chat_id الطبيب')
    return redirect(url_for('settings'))


@app.route('/settings/test_doctor_notification', methods=['POST'])
@doctor_required
def test_doctor_notification():
    """يرسل رسالة اختبار للطبيب على chat_id المضبوط في الإعدادات."""
    if not doctor_telegram_chat_id():
        flash('لم يُضبط chat_id الطبيب بعد. اضبطه أولاً أو استخدم زر '
              '«كشف chat_id تلقائياً».', 'error')
        return redirect(url_for('settings'))
    ok = notify_doctor(
        f' <b>رسالة اختبار من بوت العيادة</b>\n\n'
        f'إذا وصلتك هذه الرسالة، فإشعارات الطبيب مُفعّلة بنجاح ✅\n'
        f'<i>— {clinic_name_safe()}</i>',
        log_subject='رسالة اختبار للطبيب')
    if ok:
        flash('✅ تم إرسال رسالة اختبار للطبيب — تحقق من تيليجرام.', 'success')
    else:
        flash('تعذّر الإرسال — راجع chat_id الطبيب وسجلات الإشعارات.', 'error')
    return redirect(url_for('settings'))


@app.route('/telegram/set_webhook', methods=['GET'])
@doctor_required
def telegram_set_webhook():
    """ضبط webhook تلقائياً على الرابط العام الحالي للتطبيق"""
    ok, result = telegram_api_call('setWebhook', {
        'url': request.url_root.rstrip('/') + '/telegram/webhook',
        # يجب تضمين callback_query صراحة، وإلا تجاهل تيليجرام ضغطات الأزرار
        # التفاعلية (Inline Buttons) تماماً ولا يُرسلها للـ webhook أبداً —
        # كان هذا سبب عدم عمل أزرار المريض رغم وصول الرسائل النصية بنجاح.
        'allowed_updates': ['message', 'callback_query'],
    })
    if ok:
        flash('تم ضبط webhook تيليجرام بنجاح على الرابط الحالي', 'success')
    else:
        flash(f'فشل ضبط webhook: {result}', 'error')
    return redirect(url_for('settings'))


def get_tomorrow_confirmation_stats():
    """إحصائيات تأكيدات حضور مواعيد الغد (تظهر في بطاقة لوحة التحكم)"""
    tomorrow = date.today() + timedelta(days=1)
    appointments = Appointment.query.filter(
        Appointment.date == tomorrow,
        Appointment.status == 'scheduled').order_by(Appointment.time).all()
    return {
        'total': len(appointments),
        'confirmed': sum(1 for a in appointments if a.attendance_confirmed is True),
        'declined': sum(1 for a in appointments if a.attendance_confirmed is False),
        'pending': sum(1 for a in appointments if a.attendance_confirmed is None),
        'appointments': appointments,
    }


@app.route('/alerts/poll')
@doctor_required
def alerts_poll():
    """تنبيهات الطبيب غير المقروءة (اعتذارات الحضور) — تُعتبر مقروءة بعد جلبها"""
    alerts = DoctorAlert.query.filter_by(acknowledged=False).order_by(
        DoctorAlert.created_at).all()
    for a in alerts:
        a.acknowledged = True
    db.session.commit()
    return {'alerts': [{'id': a.id, 'message': a.message,
                        'time': a.created_at.strftime('%H:%M')} for a in alerts]}


@app.route('/notifications')
@doctor_required
def notifications_page():
    page = request.args.get('page', 1, type=int)
    logs = NotificationLog.query.order_by(NotificationLog.created_at.desc()).paginate(
        page=page, per_page=25, error_out=False)
    return render_template('notifications.html', logs=logs)


@app.route('/settings/password', methods=['POST'])
@doctor_required
def change_password():
    user = User.query.get(session['doctor_id'])
    current = request.form.get('current_password', '')
    new = request.form.get('new_password', '')
    confirm = request.form.get('confirm_password', '')
    if not user.check_password(current):
        flash('كلمة المرور الحالية غير صحيحة', 'error')
    elif len(new) < 6:
        flash('كلمة المرور الجديدة يجب أن تكون 6 أحرف على الأقل', 'error')
    elif new != confirm:
        flash('كلمتا المرور غير متطابقتين', 'error')
    else:
        user.set_password(new)
        log_action('update', 'user', user.id, 'تغيير كلمة المرور')
        db.session.commit()
        flash('تم تغيير كلمة المرور بنجاح', 'success')
        return redirect(url_for('settings'))


@app.route('/audit')
@doctor_required
def audit_log_page():
    page = request.args.get('page', 1, type=int)
    logs = AuditLog.query.order_by(AuditLog.created_at.desc()).paginate(
        page=page, per_page=30, error_out=False)
    return render_template('audit.html', logs=logs)


# ===================== رسائل المرضى المباشرة =====================

def _notify_patient_doctor_reply(patient, body):
    """إرسال رد الطبيب للمريض عبر القنوات المفعلة (تيليجرام/بريد) مع تسجيل كل محاولة"""
    clinic = get_setting('clinic_name', 'العيادة')
    subject = f'رد جديد من {clinic}'
    sent_any = False
    # القناة الأولى: تيليجرام
    if get_setting('notify_telegram_enabled') == '1':
        chat_id = (patient.telegram_chat_id or '').strip()
        if chat_id:
            text = (f'💬 <b>رد الطبيب على رسالتك</b>\n\n'
                    f'{body}\n\n— {clinic}')
            log = NotificationLog(recipient=chat_id, channel='telegram',
                                  subject=subject, status='queued')
            db.session.add(log)
            db.session.flush()
            db.session.commit()
            threading.Thread(target=_send_telegram_worker,
                             args=(log.id, chat_id, text), daemon=True).start()
            sent_any = True
        else:
            db.session.add(NotificationLog(recipient=None, channel='telegram',
                                           subject=subject, status='skipped',
                                           error='المريض غير مرتبط بتيليجرام'))
            db.session.commit()
    # القناة الثانية: البريد الإلكتروني
    if get_setting('notify_email_enabled') == '1':
        email = (patient.email or '').strip()
        api_key = get_setting('brevo_api_key', '')
        sender_email = get_setting('brevo_sender_email', '')
        if email and '@' in email and api_key and '@' in sender_email:
            html = ('<div dir="rtl" style="font-family:Arial;padding:20px">'
                    '<h3>💬 رد الطبيب على رسالتك</h3>'
                    f'<p style="line-height:1.8">{body}</p>'
                    f'<hr><small>{clinic}</small></div>')
            log = NotificationLog(recipient=email, subject=subject, status='queued')
            db.session.add(log)
            db.session.flush()
            db.session.commit()
            threading.Thread(target=_send_email_worker,
                             args=(log.id, email, subject, html), daemon=True).start()
            sent_any = True
        else:
            db.session.add(NotificationLog(recipient=email or None, subject=subject,
                                           status='skipped',
                                           error='بيانات البريد غير مكتملة'))
            db.session.commit()
    return sent_any


@app.route('/messages')
@doctor_required
def messages_list():
    """قائمة محادثات المرضى مع عداد غير المقروء لكل مريض"""
    rows = PatientMessage.query.order_by(
        PatientMessage.created_at.desc()).all()
    threads_map = {}
    for m in rows:
        t = threads_map.setdefault(m.patient_id, {
            'patient': m.patient, 'unread': 0, 'total': 0, 'last': m.created_at})
        t['total'] += 1
        if m.sender == 'patient' and not m.is_read:
            t['unread'] += 1
    threads = sorted(threads_map.values(),
                     key=lambda x: x['last'] or utcnow(), reverse=True)
    return render_template('messages/list.html', threads=threads)


@app.route('/messages/<int:patient_id>')
@doctor_required
def message_thread(patient_id):
    """محادثة مريض معين - وتعليم رسائله كمقروءة عند الفتح"""
    patient = db.session.get(Patient, patient_id)
    if not patient:
        abort(404)
    msgs = PatientMessage.query.filter_by(patient_id=patient_id).order_by(
        PatientMessage.created_at).all()
    PatientMessage.query.filter_by(patient_id=patient_id, sender='patient',
                                   is_read=False).update({'is_read': True})
    db.session.commit()
    return render_template('messages/thread.html', patient=patient, msgs=msgs)


@app.route('/messages/<int:patient_id>/reply', methods=['POST'])
@doctor_required
def message_reply(patient_id):
    """حفظ رد الطبيب في المحادثة وإشعار المريض عبر القنوات المفعلة"""
    patient = db.session.get(Patient, patient_id)
    if not patient:
        abort(404)
    body = (request.form.get('body') or '').strip()
    if not body:
        flash('لا يمكن إرسال رد فارغ', 'error')
        return redirect(url_for('message_thread', patient_id=patient_id))
    if len(body) > 4000:
        body = body[:4000]
    new_msg = PatientMessage(patient_id=patient_id, sender='doctor',
                             body=body, channel='dashboard')
    db.session.add(new_msg)
    log_action('create', 'message', patient_id, 'رد الطبيب على رسالة مريض')
    db.session.commit()
    db.session.refresh(new_msg)
    _notify_patient_doctor_reply(patient, body)
    _emit_message_event('doctor', new_msg)  # إشعار لحظي للنوافذ الأخرى
    # دعم طلب AJAX من صفحة المحادثة لإبقاء المستخدم في نفس الصفحة
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
        return jsonify({
            'ok': True,
            'message': {
                'id': new_msg.id,
                'patient_id': new_msg.patient_id,
                'sender': new_msg.sender,
                'body': new_msg.body,
                'channel': new_msg.channel,
                'time': new_msg.created_at.strftime('%H:%M') if new_msg.created_at else '',
            },
        })
    flash('تم حفظ الرد في المحادثة وإشعار المريض حسب القنوات المفعلة', 'success')
    return redirect(url_for('message_thread', patient_id=patient_id))


@app.route('/messages/poll')
@doctor_required
def messages_poll():
    """تنبيه Toast فوري: استطلاع رسائل المرضى الجديدة لجلسة الطبيب
    كل رسالة جديدة تظهر مرة واحدة فقط وفق علامة استطلاع محفوظة في الجلسة،
    والنداء الأول يهيئ العلامة عند آخر رسالة قائمة. عداد غير المقروء يحدث
    في كل نداء بينما التصفير يتم فقط بفتح المحادثة.
    """
    if 'msg_toast_watermark' not in session:
        last_id = db.session.query(db.func.max(PatientMessage.id)).filter(
            PatientMessage.sender == 'patient').scalar()
        session['msg_toast_watermark'] = last_id or 0
    watermark = session.get('msg_toast_watermark') or 0
    fresh = PatientMessage.query.filter(
        PatientMessage.sender == 'patient',
        PatientMessage.id > watermark).order_by(PatientMessage.id).all()
    items = []
    for m in fresh:
        body = m.body or ''
        if len(body) > 140:
            body = body[:140] + '…'
        items.append({
            'id': m.id,
            'patient_id': m.patient_id,
            'patient_name': m.patient.full_name if m.patient else 'مريض',
            'body': body,
            'time': m.created_at.strftime('%H:%M') if m.created_at else '',
            'channel': m.channel or 'portal',
            'url': url_for('message_thread', patient_id=m.patient_id),
        })
    if fresh:
        session['msg_toast_watermark'] = fresh[-1].id
    unread_count = PatientMessage.query.filter_by(
        sender='patient', is_read=False).count()
    return {'unread_count': unread_count, 'messages': items}


@app.route('/portal/contact', methods=['GET', 'POST'])
def portal_contact():
    """مراسلة الطبيب مباشرة من البوابة العامة دون حساب"""
    if request.method == 'POST':
        full_name = (request.form.get('full_name') or '').strip()
        phone = (request.form.get('phone') or '').strip()
        body = (request.form.get('body') or '').strip()
        if not full_name or not phone or not body:
            flash('يرجى تعبئة الاسم ورقم الهاتف والرسالة', 'error')
            return redirect(url_for('portal_contact'))
        if len(body) > 4000:
            body = body[:4000]
        patient = find_or_create_patient(full_name, phone)
        new_msg = PatientMessage(patient_id=patient.id, sender='patient',
                                 body=body, channel='portal')
        db.session.add(new_msg)
        log_action('create', 'message', patient.id, 'رسالة من البوابة')
        db.session.commit()
        db.session.refresh(new_msg)
        _emit_message_event('patient', new_msg)  # إشعار لحظي للطبيب
        # إشعار تيليجرام للطبيب برسالة المريض الجديدة من البوابة
        preview = body if len(body) <= 200 else body[:200] + '…'
        notify_doctor(
            f'📩 <b>رسالة جديدة من البوابة</b>\n\n'
            f'<b>{patient.full_name}</b> ({patient.code})\n'
            f'📞 {patient.phone or "—"}\n\n'
            f'{preview}\n\n'
            f'<i>— من بوابة المريض</i>',
            log_subject='رسالة مريض جديدة')
        conversation = PatientMessage.query.filter_by(patient_id=patient.id).order_by(
            PatientMessage.created_at.desc()).limit(20).all()
        return render_template('portal/contact_success.html',
                               patient=patient,
                               conversation=list(reversed(conversation)))
    return render_template('portal/contact.html')


# ===================== القوالب الجاهزة =====================

@app.context_processor
def inject_globals():
    pending_count = 0
    unread_msgs_count = 0
    if session.get('doctor_id'):
        pending_count = Appointment.query.filter_by(status='pending').count()
        unread_msgs_count = PatientMessage.query.filter_by(
            sender='patient', is_read=False).count()
        if 'msg_toast_watermark' not in session:
            last_id = db.session.query(db.func.max(PatientMessage.id)).filter(
                PatientMessage.sender == 'patient').scalar()
            session['msg_toast_watermark'] = last_id or 0
    return dict(
        clinic_name=get_setting('clinic_name', 'عيادة الأخصائي النفساني'),
        doctor_name=get_setting('doctor_name', ''),
        today=date.today(),
        timedelta=timedelta,
        is_doctor=bool(session.get('doctor_id')),
        telegram_bot_link=f"https://t.me/{telegram_bot_username()}",
        statuses=APPOINTMENT_STATUSES,
        weekday_names=WEEKDAY_NAMES,
        pending_requests_count=pending_count,
        unread_messages_count=unread_msgs_count,
    )


# ===================== تهيئة قاعدة البيانات =====================

# ترحيل أعمدة لقواعد بيانات قديمة أنشئت قبل التحديث
SCHEMA_MIGRATIONS = {
    'patients': [
        ('is_active', "ALTER TABLE patients ADD COLUMN is_active BOOLEAN DEFAULT 1"),
        ('telegram_chat_id', "ALTER TABLE patients ADD COLUMN telegram_chat_id VARCHAR(50)"),
    ],
    'patient_messages': [
        ('ai_apologized', "ALTER TABLE patient_messages ADD COLUMN ai_apologized BOOLEAN DEFAULT 0"),
    ],
    'appointments': [
        ('ticket_code', "ALTER TABLE appointments ADD COLUMN ticket_code VARCHAR(20)"),
        ('source', "ALTER TABLE appointments ADD COLUMN source VARCHAR(20) DEFAULT 'doctor'"),
        ('is_special', "ALTER TABLE appointments ADD COLUMN is_special BOOLEAN DEFAULT 0"),
        ('transport_fee', "ALTER TABLE appointments ADD COLUMN transport_fee NUMERIC(10,2) DEFAULT 0"),
        ('contact_name', "ALTER TABLE appointments ADD COLUMN contact_name VARCHAR(150)"),
        ('contact_phone', "ALTER TABLE appointments ADD COLUMN contact_phone VARCHAR(20)"),
        ('attendance_confirmed', "ALTER TABLE appointments ADD COLUMN attendance_confirmed BOOLEAN"),
        ('resched_prev_date', "ALTER TABLE appointments ADD COLUMN resched_prev_date DATE"),
        ('resched_prev_time', "ALTER TABLE appointments ADD COLUMN resched_prev_time TIME"),
        ('resched_prev_status', "ALTER TABLE appointments ADD COLUMN resched_prev_status VARCHAR(20)"),
    ],
}


def ensure_schema():
    with db.engine.connect() as conn:
        for table, columns in SCHEMA_MIGRATIONS.items():
            existing = {row[1] for row in conn.execute(
                text(f"PRAGMA table_info({table})"))}
            if not existing:
                continue
            for col, ddl in columns:
                if col not in existing:
                    conn.execute(text(ddl))
            conn.commit()


def seed_defaults():
    if not User.query.first():
        user = User(username='doctor', name='الطبيب')
        user.set_password('doctor2026')
        db.session.add(user)
        db.session.commit()


with app.app_context():
    db.create_all()
    ensure_schema()
    seed_defaults()

# سجّل دالة قراءة الإعدادات من DB للمساعد الذكي (ليقرأ المفاتيح من DB)
try:
    import ai_client
    ai_client.set_db_getter(get_setting)
except ImportError:
    pass


# ===================== مجدول التذكيرات التلقائية =====================

# يفحص كل 30 دقيقة ويرسل تذكيرات مواعيد الغد عبر تيليجرام بعد ساعة التذكير
# المضبوطة (telegram_reminder_hour). العلم CLINIC_DISABLE_SCHEDULER=1 يوقفه.

from apscheduler.schedulers.background import BackgroundScheduler
import atexit

scheduler = BackgroundScheduler()
scheduler.add_job(check_and_send_reminders, 'interval', minutes=30,
                  id='telegram_reminders', max_instances=1)

# تقرير صباحي يومي للطبيب بين 7 و 10 صباحاً (يفحص كل 30 دقيقة لكنه يُرسل مرة/يوم)
scheduler.add_job(check_and_send_doctor_report, 'interval', minutes=30,
                  id='doctor_daily_report', max_instances=1)

# تذكير استئناف العمل قبل يوم من نهاية العطلة (يفحص كل ساعة)
scheduler.add_job(check_and_send_holiday_resume_reminders, 'interval', hours=1,
                  id='holiday_resume_reminders', max_instances=1)

# الاعتذار التلقائي للمرضى عن تأخر رد الطبيب (يفحص كل 5 دقائق)
import auto_apology


def _run_apology_check():
    with app.app_context():
        auto_apology.check_and_send_apologies()


scheduler.add_job(_run_apology_check, 'interval', minutes=5,
                  id='ai_auto_apology', max_instances=1)


def _start_scheduler():
    """بدء المجدول الدوري والاستماع لتحديثات تيليجرام"""
    if os.environ.get('CLINIC_DISABLE_SCHEDULER') == '1':
        print("ℹ️  تم تعطيل المجدول الدوري (CLINIC_DISABLE_SCHEDULER=1)")
        return
    # مع وضع Debug يعيد werkzeug تشغيل العملية مرتين؛ نشغّل المجدول في العملية الفعلية فقط
    if app.config['DEBUG'] and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        return
    try:
        scheduler.start()
        print("✅ تم تشغيل مجدول التذكيرات التلقائية بنجاح")
    except Exception as e:
        print(f'❌ تعذر تشغيل مجدول التذكيرات: {e}')
    # استقبال رسائل البوت محلياً (getUpdates) ما لم يكن هناك webhook مضبوط
    print("🤖 بدء تشغيل بوت تيليجرام (Polling Mode)...")
    threading.Thread(target=_telegram_polling_loop, daemon=True).start()
    print("✅ تم تشغيل جميع الخدمات الخلفية بنجاح\n")


_start_scheduler()

atexit.register(lambda: scheduler.shutdown(wait=False) if scheduler.running else None)


# ===================== المساعد الذكي (GLM-5.3) =====================

@app.route('/api/assistant/chat', methods=['POST'])
@doctor_required
def assistant_chat():
    """نقطة المحادثة الرئيسية للمساعد الذكي.
    تستقبل: {message: str} من الطبيب.
    تعيد: {type: 'text'|'tool_call'|'error', message, ...}.
    """
    import assistant
    data = request.get_json(silent=True) or {}
    message = (data.get('message') or '').strip()
    if not message:
        return jsonify({'type': 'error', 'message': 'رسالة فارغة'}), 400
    # استخدم session_id من جلسة Flask (كل تبويب = جلسة منفصلة عبر client_id)
    session_id = data.get('client_id') or f"doc_{session.get('doctor_id', 0)}"
    result = assistant.chat(session_id, message)
    return jsonify(result)


@app.route('/api/assistant/execute', methods=['POST'])
@doctor_required
def assistant_execute():
    """تنفيذ أداة بعد تأكيد الطبيب.
    تستقبل: {tool: str, args: dict, client_id: str}.
    تعيد: {type: 'text'|'error', message, success}.
    """
    import assistant
    data = request.get_json(silent=True) or {}
    tool_name = data.get('tool')
    args = data.get('args', {})
    if not tool_name:
        return jsonify({'type': 'error', 'message': 'اسم الأداة مطلوب'}), 400
    session_id = data.get('client_id') or f"doc_{session.get('doctor_id', 0)}"
    log_action('create', 'appointment', details=f'مساعد ذكي: {tool_name}')
    result = assistant.execute_confirmed_tool(session_id, tool_name, args)
    return jsonify(result)


@app.route('/api/assistant/clear', methods=['POST'])
@doctor_required
def assistant_clear():
    """مسح سجل المحادثة"""
    import assistant
    data = request.get_json(silent=True) or {}
    session_id = data.get('client_id') or f"doc_{session.get('doctor_id', 0)}"
    assistant.clear_history(session_id)
    return jsonify({'ok': True})


@app.route('/api/assistant/voice/stt', methods=['POST'])
@doctor_required
def assistant_voice_stt():
    """تحويل صوت (base64) إلى نص عبر Groq Whisper.
    تستقبل: {audio: str (base64)}.
    تعيد: {text: str} أو {error: str}.
    """
    import ai_client
    if not ai_client.is_configured():
        return jsonify({'error': 'لم يُضبط أي مفتاح AI (GROQ_API_KEY أو GEMINI_API_KEY)'}), 500
    data = request.get_json(silent=True) or {}
    audio_b64 = (data.get('audio') or '').strip()
    if not audio_b64:
        return jsonify({'error': 'لا يوجد صوت'}), 400
    # إزالة بادئة data URL إن وُجدت
    if ',' in audio_b64 and audio_b64.startswith('data:'):
        audio_b64 = audio_b64.split(',', 1)[1]
    try:
        text = ai_client.speech_to_text(audio_b64)
        return jsonify({'text': text})
    except ai_client.AIError as e:
        return jsonify({'error': str(e), 'use_browser_fallback': True}), 500


@app.route('/api/assistant/voice/tts', methods=['POST'])
@doctor_required
def assistant_voice_tts():
    """تحويل نص إلى صوت عربي طبيعي (mp3) — يجرّب Edge TTS أولاً (أصوات عصبية
    شبيهة بصوت بشري حقيقي، مجانية بلا مفتاح API)، ويتراجع تلقائياً إلى gTTS
    إذا فشل (مثلاً مشكلة شبكة). مستقل تماماً عن أصوات النظام (لا علاقة بـ
    Narrator أو أي محرك نطق محلي).
    لتغيير الصوت: بدّل قيمة EDGE_TTS_VOICE أدناه. أمثلة أصوات عربية طبيعية:
       ar-SA-HamedNeural     رجل، سعودي، واضح (الافتراضي)
       ar-SA-ZariyahNeural   امرأة، سعودية
       ar-EG-ShakirNeural    رجل، مصري
       ar-EG-SalmaNeural     امرأة، مصرية
       ar-DZ-IsmaelNeural    رجل، جزائري
       ar-DZ-AminaNeural     امرأة، جزائرية
    تستقبل: {text: str}.
    تعيد: ملف صوتي audio/mpeg مباشرة، أو {error} عند فشل كل الطرق.
    """
    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'لا يوجد نص'}), 400
    text = text[:800]  # حد أقصى معقول لطول النص المنطوق
    EDGE_TTS_VOICE = 'ar-SA-HamedNeural'
    # المحاولة 1: Edge TTS (صوت طبيعي عصبي)
    try:
        import asyncio
        import edge_tts

        async def _gen():
            communicate = edge_tts.Communicate(text, voice=EDGE_TTS_VOICE)
            audio = b''
            async for chunk in communicate.stream():
                if chunk.get('type') == 'audio':
                    audio += chunk.get('data', b'')
            return audio

        audio_bytes = asyncio.run(_gen())
        if audio_bytes:
            return Response(audio_bytes, mimetype='audio/mpeg', headers={'Cache-Control': 'no-store'})
    except Exception as e:
        print(f'[TTS] فشل Edge TTS، التراجع إلى gTTS: {e}')
    # المحاولة 2 (احتياطية): gTTS
    try:
        from gtts import gTTS
        import io
        buf = io.BytesIO()
        gTTS(text=text, lang='ar').write_to_fp(buf)
        buf.seek(0)
        return Response(buf.read(), mimetype='audio/mpeg', headers={
            'Cache-Control': 'no-store',
        })
    except ImportError:
        return jsonify({'error': 'لا توجد مكتبة TTS مثبَّتة على الخادم. نفّذ: pip install -r requirements.txt'}), 500
    except Exception as e:
        import traceback
        traceback.print_exc()  # تفاصيل كاملة في سجل الخادم (console/logs) للتشخيص
        return jsonify({'error': f'فشل توليد الصوت: {e}'}), 500


@app.route('/api/assistant/status')
@doctor_required
def assistant_status():
    """تشخيص حالة المزودين — أيهم مضبوط وأيهم فاشل."""
    import ai_client
    return jsonify({
        'providers': ai_client.get_status(),
        'any_configured': ai_client.is_configured(),
    })


# ملاحظة: TTS (نص→صوت) يُنفّذ الآن عبر /api/assistant/voice/tts (gTTS، صوت
# عربي حقيقي مولَّد على الخادم) بدلاً من Web Speech API الذي يعتمد على أصوات
# النظام المحلية (وقد يستدعي Narrator على ويندوز). المتصفح فقط يُشغّل ملف mp3.

if __name__ == '__main__':
    # host='0.0.0.0' للوصول من أجهزة أخرى (كالهاتف) على نفس الشبكة المحلية.
    app.run(debug=app.config['DEBUG'], port=5000, host='0.0.0.0')