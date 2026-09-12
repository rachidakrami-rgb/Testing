# -*- coding: utf-8 -*-
"""text_utils — أدوات تنظيف النص المشتركة بين المساعدين (الطبيب/المريض)
والنطق الصوتي، في مكان واحد بدل تكرارها في كل ملف (كما كان يحدث سابقاً
وتسبب بأخطاء عند تعديل نسخة دون الأخرى).

الدوال المُصدَّرة:
- sanitize_repetition(text)   — يزيل تكرار الرموز/الكلمات المفرط (خلل
  توليد معروف)، وآمن تماماً على التواريخ (11-11-2026) لأنه يتطلب 3
  فواصل متتالية مباشرة بلا أرقام بينها لتفعيل الاستبدال.
- clean_text_for_display(text) — للنص المعروض: بلا جداول أو رموز تنسيق،
  ويُبقي الوقت/التاريخ كأرقام كما هي (تُقرأ بالعين لا بالصوت).
- clean_text_for_telegram(text) — التنظيف النهائي لرسائل المساعد المكتوبة:
  كل ما سبق + حذف المصطلحات الإنجليزية (الاستثناء الوحيد: رقم التذكرة).
- clean_text_for_speech(text)  — للنص المرسَل لمحرك النطق الصوتي: كل ما
  سبق + تحويل الوقت الرقمي لنطق عربي طبيعي + توسيع الاختصارات (د./م./ع.).
"""
import re

from time_speech import humanize_times

# اختصارات تُستبدل فقط عند وجودها ككلمة مستقلة تماماً (وليس كجزء من كلمة
# أطول) — لتفادي إتلاف كلمات عادية تنتهي بنفس الحرف (واحد، بعد، عند...).
ABBREVIATIONS_AR = {
    'د.': 'دكتور',
    'م.': 'مريض',
    'ع.': 'عيادة',
}


def sanitize_repetition(text):
    """يزيل تكرار الأحرف/الرموز/العبارات المفرط الناتج عن خلل توليد نموذج
    اللغة (Repetition Degeneration). آمن على التواريخ: "11-11-2026" لا
    يحتوي 3 شرطات متتالية مباشرة (تتوسطها أرقام)، فلا يُطابق النمط أدناه."""
    if not text:
        return text
    # أي حرف/رمز مفرد يتكرر 4 مرات متتالية أو أكثر → يُختصر لتكرارين فقط
    text = re.sub(r'(.)\1{3,}', r'\1\1', text)
    # أنماط فاصل متكررة مع مسافات بينها مباشرة (— — — — أو - - - -)
    text = re.sub(r'(?:[-—]\s*){3,}', ' — ', text)
    # كلمة أو عبارة قصيرة (حتى 4 كلمات) تتكرر 3 مرات متتالية أو أكثر
    text = re.sub(r'\b((?:\S+\s+){0,3}\S+)\b(?:\s+\1\b){2,}', r'\1', text)
    # تقليص الأسطر الفارغة/المسافات المتكررة الناتجة عن الحذف أعلاه
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()


# ---------- حماية أكواد التذاكر (TK-XXXXXX) أثناء التنظيف ----------
_TICKET_OPEN = '\uE000'  # علامات استبدال مؤقتة من منطقة الاستخدام الخاص
_TICKET_CLOSE = '\uE001'


def _wrap_ticket_codes(text):
    """تغليف أكواد التذاكر (TK-XXXXXX) بعلامات مؤقتة حتى لا تُمس أثناء التنظيف.
    التذكرة هي الاستثناء الوحيد المسموح به من المصطلحات اللاتينية (طلب صريح)."""
    if not text:
        return text
    return re.sub(r'\bTK[-\s]?([A-Za-z0-9]{3,})\b',
                  _TICKET_OPEN + r'\1' + _TICKET_CLOSE, text)


def _unwrap_ticket_codes(text):
    return text.replace(_TICKET_OPEN, 'TK-').replace(_TICKET_CLOSE, '')


def strip_markdown_symbols(text):
    """إزالة بقايا الجداول وعلامات التنسيق التي يُخرجها نموذج اللغة أحياناً:
    أعمدة الجداول (|)، الفواصل الطويلة (---- ==== ****)، بلوكات الكود،
    رموز الرؤوس (#)، والاقتباسات (>).
    آمنة على التواريخ والأوقات (11-11-2026 و 08:30 لا تُمس لأن الشرطة/النقطتان
    بين أرقام لا تشكل تكراراً متتالياً)."""
    if not text:
        return text
    # بلوكات كود كاملة تُحذف بمحتواها
    text = re.sub(r'```[\s\S]*?```', ' ', text)
    text = re.sub(r'`([^`]*)`', r'\1', text)
    # أعمدة الجداول — أشهر مصدر إزعاج في ردود المساعد
    text = re.sub(r'\|+', ' ', text)
    # الفواصل الطويلة المتتالية ---- / ==== / **** / ~~~ / ••••
    text = re.sub(r'[-_=*~•·]{2,}', ' ', text)
    # رموز متفرقة
    text = text.replace('**', '').replace('*', '').replace('#', '')
    text = re.sub(r'(?m)^\s*>\s?', '', text)
    return text


def remove_latin_terms(text):
    """حذف المصطلحات الإنجليزية/اللاتينية من ردود المساعد مع الاستثناء الوحيد:
    أكواد التذاكر (TK-XXXXXX) تبقى كما هي (طلب صريح من الطبيبة).
    الأرقام والتواريخ والرموز العربية لا تُمس."""
    if not text:
        return text
    text = _wrap_ticket_codes(text)
    # كلمات لاتينية كاملة (scheduled, pending, OK, markdown ...)
    text = re.sub(r'[A-Za-z]{2,}', ' ', text)
    # حرف لاتيني مفرد متروك (يقرؤه المحرك الصوتي حرفاً حرفاً)
    text = re.sub(r'\b[A-Za-z]\b', ' ', text)
    return _unwrap_ticket_codes(text)


def clean_text_for_telegram(text):
    """التنظيف النهائي لرسائل المساعد المكتوبة (تيليجرام والواجهة):
    بلا جداول أو رموز تنسيق أو مصطلحات إنجليزية (عدا رقم التذكرة)،
    مع إبقاء التواريخ والأوقات بصيغتها الرقمية كما هي (تُقرأ بالعين)."""
    if not text:
        return text
    text = clean_text_for_display(text)   # يشمل إزالة الجداول والرموز
    text = remove_latin_terms(text)
    text = sanitize_repetition(text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def clean_text_for_display(text):
    """تنظيف النص المعروض (رسالة نصية للطبيب أو المريض) من رموز التنسيق
    وأي أكواد/بلوكات وجداول، مع الحفاظ الكامل على الأرقام والتواريخ كما هي."""
    if not text:
        return text
    # إزالة بلوكات الكود الكاملة (``` ... ```) مع محتواها بالكامل
    text = re.sub(r'```[\s\S]*?```', '', text)
    # إزالة أكواد سطر واحد بين backticks مفردة (`مثل_هذا`)
    text = re.sub(r'`([^`]*)`', r'\1', text)
    # إزالة أعمدة الجداول | والفواصل الطويلة ---- (شكوى أساسية من قراءة الصوت)
    text = strip_markdown_symbols(text)
    # إزالة رموز Markdown الشائعة دون التأثير على الشرطات داخل الأرقام
    text = text.replace('**', '').replace('*', '').replace('#', '')
    text = sanitize_repetition(text)
    return text.strip()


def clean_text_for_speech(text):
    """تنظيف النص قبل تحويله لصوت: كل تنظيف العرض + حذف المصطلحات الإنجليزية
    (عدا رقم التذكرة) + تحويل الوقت الرقمي لنطق عربي طبيعي + توسيع الاختصارات."""
    if not text:
        return text
    text = clean_text_for_display(text)
    # المصطلحات الإنجليزية تُقرأ حرفاً حرفاً بغير مفهوم — تُحذف (عدا التذكرة)
    text = remove_latin_terms(text)
    text = humanize_times(text)
    for abbr, full in ABBREVIATIONS_AR.items():
        pattern = r'(?<!\S)' + re.escape(abbr) + r'(?=\s|$)'
        text = re.sub(pattern, full, text)
    # إزالة الروابط (URLs) — لا تُقرأ حرفياً بالصوت
    text = re.sub(r'https?://\S+', ' رابط ', text)
    text = re.sub(r'\(\s*\)', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:800]
