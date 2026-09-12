"""نماذج قاعدة البيانات لعيادة أخصائي نفساني"""
from datetime import datetime, date, timezone

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


def utcnow():
    """الوقت الحالي بتوقيت UTC (بديل عن datetime.utcnow المهجورة)"""
    return datetime.now(timezone.utc)


def today():
    return date.today()


class User(db.Model):
    """حساب الطبيب / الإدارة"""
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Patient(db.Model):
    """نموذج المريض"""
    __tablename__ = 'patients'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)  # كود المريض
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    gender = db.Column(db.String(10))  # ذكر / أنثى
    birth_date = db.Column(db.Date)
    phone = db.Column(db.String(20))
    email = db.Column(db.String(120))
    telegram_chat_id = db.Column(db.String(50))  # معرف محادثة تيليجرام بعد ربط البوت
    address = db.Column(db.Text)
    job = db.Column(db.String(100))
    marital_status = db.Column(db.String(20))
    emergency_contact = db.Column(db.String(100))
    emergency_phone = db.Column(db.String(20))
    medical_history = db.Column(db.Text)  # تاريخ مرضي
    chronic_diseases = db.Column(db.Text)
    current_medications = db.Column(db.Text)
    notes = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)  # أرشفة بدل الحذف النهائي
    created_at = db.Column(db.DateTime, default=utcnow)

    appointments = db.relationship('Appointment', backref='patient', lazy=True,
                                   cascade='all, delete-orphan')
    visits = db.relationship('Visit', backref='patient', lazy=True,
                             cascade='all, delete-orphan')
    images = db.relationship('MedicalImage', backref='patient', lazy=True,
                             cascade='all, delete-orphan')
    invoices = db.relationship('Invoice', backref='patient', lazy=True,
                               cascade='all, delete-orphan')

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def age(self):
        if self.birth_date:
            t = date.today()
            return t.year - self.birth_date.year - (
                (t.month, t.day) < (self.birth_date.month, self.birth_date.day))
        return None


class Appointment(db.Model):
    """نموذج الموعد

    الحالات: pending (طلب بانتظار تأكيد الطبيب)، scheduled، completed،
    cancelled، no_show.
    المصدر: doctor (حجز من داخل العيادة) أو patient (طلب إلكتروني من بوابة المريض).
    """
    __tablename__ = 'appointments'

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('patients.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    time = db.Column(db.Time, nullable=False)
    duration = db.Column(db.Integer, default=60)  # بالدقائق
    status = db.Column(db.String(20), default='scheduled')
    reason = db.Column(db.String(200))  # سبب الزيارة
    notes = db.Column(db.Text)
    reminder_sent = db.Column(db.Boolean, default=False)
    attendance_confirmed = db.Column(db.Boolean, nullable=True)  # None = لم يُرد، True = سيحضر، False = معتذر
    # حقول بوابة المريض
    ticket_code = db.Column(db.String(20), unique=True)  # رقم التذكرة لمتابعة الحجز
    source = db.Column(db.String(20), default='doctor')  # doctor / patient
    is_special = db.Column(db.Boolean, default=False)  # جلسة خاصة خارج أوقات العمل
    transport_fee = db.Column(db.Numeric(10, 2), default=0)  # مصاريف النقل المكفولة
    contact_name = db.Column(db.String(150))  # بيانات التواصل كما أدخلها المريض
    contact_phone = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=utcnow)
    # عند طلب تأجيل من المريض: تُحفظ هنا حالة/تاريخ/وقت الموعد قبل التأجيل،
    # حتى إذا رفض الطبيب طلب التأجيل يعود الموعد لوضعه الأصلي بدل إلغائه
    # بالكامل (تُمسح هذه الحقول عند تأكيد الطبيب للتأجيل أو رفضه).
    resched_prev_date = db.Column(db.Date, nullable=True)
    resched_prev_time = db.Column(db.Time, nullable=True)
    resched_prev_status = db.Column(db.String(20), nullable=True)


class Visit(db.Model):
    """نموذج الزيارة - تسجيل كل زيارة والعلاج"""
    __tablename__ = 'visits'

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('patients.id'), nullable=False)
    appointment_id = db.Column(db.Integer, db.ForeignKey('appointments.id'), nullable=True)
    visit_date = db.Column(db.Date, nullable=False, default=today)
    chief_complaint = db.Column(db.Text)  # الشكوى الرئيسية
    diagnosis = db.Column(db.Text)  # التشخيص
    treatment = db.Column(db.Text)  # العلاج المقدم
    prescription = db.Column(db.Text)  # الوصفة
    session_notes = db.Column(db.Text)  # ملاحظات الجلسة
    mood_assessment = db.Column(db.String(50))  # تقييم الحالة المزاجية
    next_appointment = db.Column(db.Date)  # الموعد القادم المقترح
    fee = db.Column(db.Numeric(10, 2), default=0)  # رسوم الزيارة
    created_at = db.Column(db.DateTime, default=utcnow)

    appointment = db.relationship('Appointment', backref='visit')


class MedicalImage(db.Model):
    """نموذج الأشعات والصور الطبية"""
    __tablename__ = 'medical_images'

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('patients.id'), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    original_name = db.Column(db.String(255))
    file_path = db.Column(db.String(500), nullable=False)
    file_type = db.Column(db.String(50))  # x-ray, mri, ct, photo, document
    description = db.Column(db.Text)
    upload_date = db.Column(db.DateTime, default=utcnow)


class Invoice(db.Model):
    """نموذج الفاتورة"""
    __tablename__ = 'invoices'

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('patients.id'), nullable=False)
    invoice_number = db.Column(db.String(30), unique=True, nullable=False)
    invoice_date = db.Column(db.Date, nullable=False, default=today)
    description = db.Column(db.Text)
    amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    paid_amount = db.Column(db.Numeric(10, 2), default=0)
    status = db.Column(db.String(20), default='unpaid')  # unpaid, partial, paid
    payment_method = db.Column(db.String(50))  # cash, card, transfer
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)

    @property
    def balance(self):
        return (self.amount or 0) - (self.paid_amount or 0)


class Setting(db.Model):
    """إعدادات العيادة"""
    __tablename__ = 'settings'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.Text)


class AuditLog(db.Model):
    """سجل تدقيق: من فعل ماذا ومتى (مهم للسجلات الطبية)"""
    __tablename__ = 'audit_logs'

    id = db.Column(db.Integer, primary_key=True)
    user = db.Column(db.String(80))  # اسم المستخدم المنفذ للعملية
    action = db.Column(db.String(50))  # create / update / delete / pay / login
    entity = db.Column(db.String(50))  # patient / appointment / invoice ...
    entity_id = db.Column(db.Integer)
    details = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)


class NotificationLog(db.Model):
    """سجل إشعارات البريد المرسلة للمريض (عبر Brevo أو خدمة مماثلة)"""
    __tablename__ = 'notification_logs'

    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer)  # مرجع حر دون قيد حتى يبقى السجل بعد أي حذف
    recipient = db.Column(db.String(120))
    subject = db.Column(db.String(255))
    channel = db.Column(db.String(20), default='email')  # email (Brevo) / telegram
    status = db.Column(db.String(20), default='queued')  # queued / sent / failed / skipped
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)


class DoctorAlert(db.Model):
    """تنبيه فوري للطبيب (مثال: اعتذار مريض عن الحضور عبر زر تيليجرام)

    يُعرض كـ Toast في لوحة التحكم عبر استطلاع دوري، ويُعتبر مقروءاً بعد الإقرار.
    """
    __tablename__ = 'doctor_alerts'

    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer)  # مرجع حر دون قيد
    message = db.Column(db.Text, nullable=False)
    kind = db.Column(db.String(30), default='attendance_declined')
    acknowledged = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)

class PatientMessage(db.Model):
    """رسالة مباشرة بين المريض والطبيب (من البوابة أو تيليجرام) مع ردود الطبيب

    sender: patient (رسالة من المريض) / doctor (رد الطبيب).
    channel: portal / telegram / dashboard — من أين جاءت الرسالة.
    is_read: هل قرأ الطبيب رسائل المريض (لشرايط عدد الغير مقروء).
    """
    __tablename__ = 'patient_messages'

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('patients.id'), nullable=False)
    sender = db.Column(db.String(10), default='patient')
    body = db.Column(db.Text, nullable=False)
    channel = db.Column(db.String(20), default='portal')
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    # هل أرسل الوكيل الذكي اعتذاراً تلقائياً عن تأخر الرد على هذه الرسالة
    ai_apologized = db.Column(db.Boolean, default=False)

    patient = db.relationship('Patient', backref='messages')


class Holiday(db.Model):
    """فترة عطلة للعيادة — تُنقل خلالها كل المواعيد (scheduled + pending) تلقائياً
    إلى ما بعد تاريخ استئناف العمل، مع الحفاظ على ترتيبها وتوقيتها.

    - start_date / end_date: نطاق العطلة (شامل الطرفين).
    - resume_date: أول يوم عمل بعد العطلة (يُحسب تلقائياً = أول يوم غير إجازة أسبوعية بعد end_date).
    - reason: سبب العطلة (اختياري).
    - moved_count: عدد المواعيد التي نُقلت (للسجل).
    - notification_sent: هل أُرسل تذكير «استئناف الغد» للمرضى.
    - status: 'active' (العطلة جارية أو مقبلة) / 'past' (انتهت).
    """
    __tablename__ = 'holidays'

    id = db.Column(db.Integer, primary_key=True)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    resume_date = db.Column(db.Date, nullable=False)
    reason = db.Column(db.String(200), default='')
    moved_count = db.Column(db.Integer, default=0)
    notification_sent = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(10), default='active')  # active / past
    created_at = db.Column(db.DateTime, default=utcnow)


class AppointmentMoveLog(db.Model):
    """سجل نقل موعد بسبب عطلة — يربط الموعد بالعطلة ويحفظ التاريخ القديم/الجديد.

    يُستخدم لعرض سجل النقل ولتجنب النقل المزدوج لنفس الموعد إذا أُنشئت عطلات متداخلة.
    """
    __tablename__ = 'appointment_move_logs'

    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey('appointments.id'), nullable=False)
    holiday_id = db.Column(db.Integer, db.ForeignKey('holidays.id'), nullable=False)
    old_date = db.Column(db.Date, nullable=False)
    old_time = db.Column(db.Time, nullable=False)
    new_date = db.Column(db.Date, nullable=False)
    new_time = db.Column(db.Time, nullable=False)
    conflict_resolved = db.Column(db.Boolean, default=False)  # هل نُقل بسبب تعارض
    created_at = db.Column(db.DateTime, default=utcnow)

    appointment = db.relationship('Appointment', backref='move_logs')
    holiday = db.relationship('Holiday', backref='move_logs')
