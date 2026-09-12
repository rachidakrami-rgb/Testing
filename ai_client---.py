"""واجهة AI موحدة — تدعم عدة مزودين مع fallback تلقائي.

الترتيب الافتراضي للـ LLM: Groq → Gemini → Z.ai → Cerebras → OpenRouter
الترتيب الافتراضي للـ ASR: Groq Whisper (المتصفح ك fallback عبر Web Speech API)
الـ TTS: Edge TTS (أصوات عصبية طبيعية) مع تراجع تلقائي لـ gTTS عند الفشل

المفاتيح تُقرأ من مصدرين:
1. قاعدة البيانات (جدول settings) — الأولوية الأعلى، تُضبط من صفحة الإعدادات.
2. متغيرات البيئة (.env) — fallback.

كل مزوّد يحتاج مفتاحه في DB setting أو .env:
  GROQ_API_KEY=gsk_...        (مجاني: console.groq.com)
  GEMINI_API_KEY=AIza...      (مجاني: aistudio.google.com)
  ZAI_API_KEY=...             (مدفوع: z.ai)
  CEREBRAS_API_KEY=csk-...    (مجاني/مدفوع: cloud.cerebras.ai)
  OPENROUTER_API_KEY=sk-or-... (مدفوع حسب الاستخدام: openrouter.ai)
"""
import os
import json
import urllib.request
import urllib.error
import uuid
import base64

# ===================== المزودون المتاحون =====================
PROVIDERS = [
    {
        'name': 'Groq',
        'env_var': 'GROQ_API_KEY',
        'setting_key': 'ai_groq_api_key',  # مفتاح في DB
        'base_url': 'https://api.groq.com/openai/v1',
        'llm_model': 'openai/gpt-oss-120b',
        'asr_model': 'whisper-large-v3',
        'has_asr': True,
        'format': 'openai',
    },
    {
        'name': 'Gemini',
        'env_var': 'GEMINI_API_KEY',
        'setting_key': 'ai_gemini_api_key',
        'base_url': 'https://generativelanguage.googleapis.com/v1beta',
        'llm_model': 'gemini-2.0-flash',
        'llm_models_fallback': ['gemini-1.5-flash', 'gemini-flash-latest'],
        'asr_model': None,
        'has_asr': False,
        'format': 'gemini',
    },
    {
        'name': 'Z.ai',
        'env_var': 'ZAI_API_KEY',
        'setting_key': 'ai_zai_api_key',
        'base_url': 'https://api.z.ai/api/paas/v4',
        'llm_model': 'glm-5.3',
        'asr_model': None,
        'has_asr': False,
        'format': 'openai',
    },
    {
        'name': 'Cerebras',
        'env_var': 'CEREBRAS_API_KEY',
        'setting_key': 'ai_cerebras_api_key',
        'base_url': 'https://api.cerebras.ai/v1',
        'llm_model': 'gpt-oss-120b',
        'asr_model': None,
        'has_asr': False,
        'format': 'openai',
    },
    {
        'name': 'OpenRouter',
        'env_var': 'OPENROUTER_API_KEY',
        'setting_key': 'ai_openrouter_api_key',
        'base_url': 'https://openrouter.ai/api/v1',
        'llm_model': 'openai/gpt-oss-120b',
        'asr_model': None,
        'has_asr': False,
        'format': 'openai',
    },
]


class AIError(Exception):
    """خطأ في استدعاء AI API"""
    pass


# ===================== مساعدات =====================
_db_getter = None  # دالة قراءة من DB (تُضبط من app.py لتجنب الاستيراد الدائري)


def set_db_getter(fn):
    """تسجيل دالة قراءة الإعدادات من DB (تُستدعى من app.py)."""
    global _db_getter
    _db_getter = fn


def _get_key(env_var):
    """قراءة مفتاح: من DB أولاً (إن سُجّلت دالة)، ثم من البيئة (.env)."""
    # 1) جرّب DB
    if _db_getter is not None:
        try:
            # ابحث عن setting_key المقابل
            for p in PROVIDERS:
                if p['env_var'] == env_var:
                    val = _db_getter(p['setting_key'], '')
                    if val and val.strip():
                        return val.strip()
                    break
        except Exception:
            pass
    # 2) fallback للبيئة
    return os.environ.get(env_var, '').strip()


def available_providers():
    """قائمة المزودين المتاحين (مفاتيحهم مضبوطة)، مرتبة حسب المزوّد المفضل."""
    avail = [p for p in PROVIDERS if _get_key(p['env_var'])]
    # إذا وُجد مزوّد مفضل في DB، قدّمه
    if _db_getter is not None:
        try:
            preferred = (_db_getter('ai_preferred_provider', '') or '').strip()
            if preferred:
                # رتّب: المفضل أولاً
                avail.sort(key=lambda p: 0 if p['name'].lower() == preferred.lower() else 1)
        except Exception:
            pass
    return avail


def get_status():
    """حالة كل مزوّد للعرض في التشخيص."""
    return [{
        'name': p['name'],
        'configured': bool(_get_key(p['env_var'])),
        'has_asr': p['has_asr'],
        'key_source': 'db' if (_db_getter and _db_getter(p.get('setting_key',''),'').strip()) else ('env' if os.environ.get(p['env_var'],'').strip() else 'none'),
    } for p in PROVIDERS]


def is_configured():
    """هل أي مزوّد متاح؟"""
    return len(available_providers()) > 0


def _primary_provider():
    """أول مزوّد متاح (أو None)."""
    avail = available_providers()
    return avail[0] if avail else None


def test_provider(provider_name, api_key=None):
    """اختبار مفتاح مزوّد معين. يعيد {ok, message}.

    إذا api_key=None، يستخدم المفتاح الحالي (من DB أو .env).
    """
    provider = None
    for p in PROVIDERS:
        if p['name'].lower() == provider_name.lower():
            provider = p
            break
    if not provider:
        return {'ok': False, 'message': f'مزوّد غير معروف: {provider_name}'}
    key = (api_key or _get_key(provider['env_var'])).strip()
    if not key:
        return {'ok': False, 'message': 'لم يُدخل مفتاح'}
    try:
        if provider['format'] == 'openai':
            url = f"{provider['base_url']}/chat/completions"
            payload = {
                'model': provider['llm_model'],
                'messages': [{'role': 'user', 'content': 'قل: ok'}],
                'max_tokens': 10,
            }
            _post_json(url, payload, key, timeout=30)
            return {'ok': True, 'message': f'✅ {provider["name"]} يعمل بشكل صحيح.'}
        elif provider['format'] == 'gemini':
            # جرّب عدة نماذج
            models = [provider['llm_model']] + provider.get('llm_models_fallback', [])
            last_err = ''
            for model in models:
                url = f"{provider['base_url']}/models/{model}:generateContent?key={key}"
                payload = {'contents': [{'parts': [{'text': 'قل: ok'}]}],
                           'generationConfig': {'maxOutputTokens': 10}}
                try:
                    req = urllib.request.Request(
                        url, data=json.dumps(payload).encode('utf-8'),
                        method='POST', headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0 (compatible; ClinicApp/1.0)'})
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        resp.read()
                    return {'ok': True, 'message': f'✅ {provider["name"]} يعمل (نموذج: {model}).'}
                except urllib.error.HTTPError as e:
                    body = ''
                    try:
                        body = e.read().decode('utf-8', errors='replace')[:200]
                    except Exception:
                        pass
                    last_err = f'HTTP {e.code}: {body or e.reason}'
                    continue
            return {'ok': False, 'message': f'❌ {provider["name"]}: {last_err}'}
    except AIError as e:
        return {'ok': False, 'message': f'❌ {provider["name"]}: {e}'}
    except Exception as e:
        return {'ok': False, 'message': f'❌ {provider["name"]}: خطأ غير متوقع: {e}'}


# ===================== HTTP helpers =====================
def _post_json(url, payload, api_key, timeout=90):
    """POST JSON مع Bearer auth."""
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        url, data=data, method='POST',
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
            'User-Agent': 'Mozilla/5.0 (compatible; ClinicApp/1.0; +https://api.groq.com)',
            'Accept': 'application/json',
        })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read().decode('utf-8', errors='replace')[:500]
        except Exception:
            pass
        raise AIError(f'HTTP {e.code}: {body or e.reason}')
    except Exception as e:
        raise AIError(f'خطأ في الاتصال: {e}')


# ===================== LLM مع fallback =====================
def chat_completion(messages, tools=None, tool_choice='auto', temperature=0.7,
                    frequency_penalty=0.4, presence_penalty=0.2):
    """استدعاء LLM مع fallback تلقائي بين المزودين.

    messages: [{role, content}] (system/user/assistant/tool).
    tools: تعريفات أدوات (OpenAI format) أو None.
    frequency_penalty/presence_penalty: تقليل احتمال تكرار النموذج لنفس
        الرمز/الكلمة/العبارة (خلل معروف باسم Repetition Degeneration) —
        القيم الافتراضية معتدلة، تكفي لمنع التكرار دون التأثير على جودة الرد.
    يعيد: {content, tool_calls, provider, raw}.
    يرفع AIError إذا فشل كل المزودين.
    """
    errors = []
    for provider in available_providers():
        try:
            if provider['format'] == 'openai':
                result = _llm_openai(provider, messages, tools, tool_choice,
                                     temperature, frequency_penalty, presence_penalty)
            elif provider['format'] == 'gemini':
                result = _llm_gemini(provider, messages, tools, temperature,
                                     frequency_penalty, presence_penalty)
            else:
                continue
            result['provider'] = provider['name']
            return result
        except AIError as e:
            errors.append(f"{provider['name']}: {e}")
            continue  # جرّب المزوّد التالي
    raise AIError('فشل كل المزودين المتاحين. ' + ' | '.join(errors))


def _llm_openai(provider, messages, tools, tool_choice, temperature,
                frequency_penalty=0.4, presence_penalty=0.2):
    """استدعاء مزوّد بصيغة OpenAI (Groq, Z.ai)."""
    payload = {
        'model': provider['llm_model'],
        'messages': messages,
        'temperature': temperature,
        'max_tokens': 1024,
        'frequency_penalty': frequency_penalty,
        'presence_penalty': presence_penalty,
    }
    if tools:
        payload['tools'] = tools
        payload['tool_choice'] = tool_choice
    url = f"{provider['base_url']}/chat/completions"
    result = _post_json(url, payload, _get_key(provider['env_var']))
    choice = (result.get('choices') or [{}])[0]
    msg = choice.get('message', {})
    return {
        'content': msg.get('content') or '',
        'tool_calls': msg.get('tool_calls') or [],
        'raw': result,
    }


def _llm_gemini(provider, messages, tools, temperature,
                frequency_penalty=0.4, presence_penalty=0.2):
    """استدعاء Gemini بصيغته الأصلية مع تحويل tools.

    Gemini لا يستخدم OpenAI format، لكن يدعم function_declarations
    ومعاملي frequencyPenalty/presencePenalty ضمن generationConfig.
    """
    api_key = _get_key(provider['env_var'])
    # جرّب النماذج بالترتيب (النموذج الأساسي + fallbacks)
    models_to_try = [provider['llm_model']] + provider.get('llm_models_fallback', [])
    last_err = None
    for model in models_to_try:
        url = f"{provider['base_url']}/models/{model}:generateContent?key={api_key}"
        # تحويل messages لصيغة Gemini
        contents = []
        system_text = ''
        for m in messages:
            if m['role'] == 'system':
                system_text += m['content'] + '\n'
            elif m['role'] == 'user':
                contents.append({'role': 'user', 'parts': [{'text': m['content']}]})
            elif m['role'] == 'assistant':
                contents.append({'role': 'model', 'parts': [{'text': m.get('content', '')}]})
            elif m['role'] == 'tool':
                # نتيجة أداة — أضفها كرسالة user
                contents.append({'role': 'user', 'parts': [{'text': f'[نتيجة الأداة] {m.get("content","")}'}]})
        payload = {
            'contents': contents,
            'generationConfig': {
                'temperature': temperature,
                'maxOutputTokens': 1024,
                'frequencyPenalty': frequency_penalty,
                'presencePenalty': presence_penalty,
            },
        }
        if system_text:
            payload['systemInstruction'] = {'parts': [{'text': system_text}]}
        # تحويل tools لصيغة Gemini
        if tools:
            fd = []
            for t in tools:
                fn = t.get('function', {})
                params = fn.get('parameters', {})
                # Gemini يتطلب خطة مختلفة قليلاً
                gemini_params = {
                    'type': params.get('type', 'object'),
                    'properties': params.get('properties', {}),
                }
                if params.get('required'):
                    gemini_params['required'] = params['required']
                fd.append({
                    'name': fn.get('name'),
                    'description': fn.get('description', ''),
                    'parameters': gemini_params,
                })
            payload['tools'] = [{'function_declarations': fd}]
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                method='POST',
                headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0 (compatible; ClinicApp/1.0)'})
            with urllib.request.urlopen(req, timeout=90) as resp:
                result = json.loads(resp.read().decode('utf-8'))
            # استخراج الرد
            candidates = result.get('candidates') or []
            if not candidates:
                last_err = AIError('لا رد من Gemini')
                continue
            content_parts = candidates[0].get('content', {}).get('parts', [])
            content = ''.join(p.get('text', '') for p in content_parts if 'text' in p)
            # استخراج tool calls إن وُجدت
            tool_calls = []
            for part in content_parts:
                fc = part.get('functionCall')
                if fc:
                    tool_calls.append({
                        'id': str(uuid.uuid4())[:8],
                        'type': 'function',
                        'function': {
                            'name': fc.get('name', ''),
                            'arguments': json.dumps(fc.get('args', {}), ensure_ascii=False),
                        }
                    })
            return {
                'content': content,
                'tool_calls': tool_calls,
                'raw': result,
            }
        except urllib.error.HTTPError as e:
            body = ''
            try:
                body = e.read().decode('utf-8', errors='replace')[:300]
            except Exception:
                pass
            last_err = AIError(f'HTTP {e.code}: {body or e.reason}')
            continue  # جرّب النموذج التالي
    raise last_err or AIError('فشل Gemini')


# ===================== ASR (صوت → نص) =====================
def speech_to_text(file_base64, language='ar'):
    """تحويل صوت (base64) إلى نص. يستخدم Groq Whisper إذا متاح، وإلا يرفع خطأ
    (الواجهة تستخدم Web Speech API كـ fallback في المتصفح).

    file_base64: سلسلة base64 للملف الصوتي.
    language: كود اللغة (ar للعربية).
    """
    # ابحث عن مزوّد لديه ASR (Groq فقط حالياً)
    for provider in available_providers():
        if not provider.get('has_asr'):
            continue
        try:
            return _asr_groq(provider, file_base64, language)
        except AIError:
            continue  # جرّب المزوّد التالي (لكن لا يوجد ASR آخر حالياً)
    raise AIError('لا يوجد مزوّد ASR متاح. استخدم Web Speech API في المتصفح.')


def _asr_groq(provider, file_base64, language):
    """Groq Whisper ASR عبر multipart/form-data."""
    try:
        audio_bytes = base64.b64decode(file_base64)
    except Exception as e:
        raise AIError(f'فشل فك base64: {e}')
    url = f"{provider['base_url']}/audio/transcriptions"
    boundary = uuid.uuid4().hex
    body_parts = []
    body_parts.append(f'--{boundary}\r\n'.encode())
    body_parts.append(b'Content-Disposition: form-data; name="model"\r\n\r\n')
    body_parts.append(f"{provider['asr_model']}\r\n".encode())
    if language:
        body_parts.append(f'--{boundary}\r\n'.encode())
        body_parts.append(b'Content-Disposition: form-data; name="language"\r\n\r\n')
        body_parts.append(f'{language}\r\n'.encode())
    body_parts.append(f'--{boundary}\r\n'.encode())
    body_parts.append(
        'Content-Disposition: form-data; name="file"; filename="audio.webm"\r\n'.encode())
    body_parts.append(b'Content-Type: audio/webm\r\n\r\n')
    body_parts.append(audio_bytes)
    body_parts.append(b'\r\n')
    body_parts.append(f'--{boundary}--\r\n'.encode())
    body = b''.join(body_parts)
    req = urllib.request.Request(
        url, data=body, method='POST',
        headers={
            'Content-Type': f'multipart/form-data; boundary={boundary}',
            'Authorization': f'Bearer {_get_key(provider["env_var"])}',
            'User-Agent': 'Mozilla/5.0 (compatible; ClinicApp/1.0)',
        })
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            return result.get('text', '')
    except urllib.error.HTTPError as e:
        body_text = ''
        try:
            body_text = e.read().decode('utf-8', errors='replace')[:500]
        except Exception:
            pass
        raise AIError(f'HTTP {e.code}: {body_text or e.reason}')
    except Exception as e:
        raise AIError(f'خطأ في ASR: {e}')
