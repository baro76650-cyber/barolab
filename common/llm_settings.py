"""Shared LLM configuration. Encrypted keys are never returned by the API."""
import base64
import copy
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / 'server' / 'data' / 'llm-settings.json'
PROVIDERS = {
    'gemini': ('Gemini', 'GEMINI_API_KEY'),
    'deepseek': ('DeepSeek', 'DEEPSEEK_API_KEY'),
    'openai': ('ChatGPT / OpenAI API', 'OPENAI_API_KEY'),
    'claude': ('Claude', 'ANTHROPIC_API_KEY'),
    'agy': ('Antigravity AGY CLI', ''),
    'codex': ('Codex CLI · ChatGPT 구독', ''),
}
LOCK = threading.RLock()


class SettingsError(ValueError):
    pass


def crypt(value, decrypt=False):
    prefix = 'fernet:v1:'
    key_path = Path(os.getenv('BAROLAB_CONFIG_KEY_FILE',
                            str(ROOT / 'server' / 'data-secret' / 'llm-settings.key')))
    portable = decrypt and value.startswith(prefix)
    if portable or (not decrypt and key_path.is_file()):
        from cryptography.fernet import Fernet, InvalidToken
        try:
            cipher = Fernet(key_path.read_bytes().strip())
            if decrypt:
                return cipher.decrypt(value[len(prefix):].encode('ascii')).decode('utf-8')
            return prefix + cipher.encrypt(value.encode('utf-8')).decode('ascii')
        except (OSError, ValueError, InvalidToken):
            raise SettingsError('설정 암호화 키를 읽을 수 없거나 암호문이 유효하지 않습니다.') from None
    if os.name != 'nt':
        raise SettingsError('Windows DPAPI 암호화가 필요합니다.')
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_byte))]
    raw = base64.b64decode(value) if decrypt else value.encode('utf-8')
    buf = ctypes.create_string_buffer(raw)
    src = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    dst = Blob()
    dll = ctypes.WinDLL('crypt32', use_last_error=True)
    fn = dll.CryptUnprotectData if decrypt else dll.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst)):
        raise SettingsError('키 암호화/복호화 실패: 저장한 Windows 사용자로 실행하세요.')
    try:
        result = ctypes.string_at(dst.data, dst.size)
        return result.decode('utf-8') if decrypt else base64.b64encode(result).decode('ascii')
    finally:
        free = ctypes.WinDLL('kernel32').LocalFree
        free.argtypes = [ctypes.c_void_p]
        free(ctypes.cast(dst.data, ctypes.c_void_p))


def load():
    if not CONFIG.exists():
        return {'active': 'legacy', 'providers': {p: {'model': ''} for p in PROVIDERS}}
    try:
        data = json.loads(CONFIG.read_text(encoding='utf-8'))
        if data['active'] not in ('legacy', *PROVIDERS) or not isinstance(data['providers'], dict):
            raise ValueError()
        return data
    except (ValueError, KeyError, TypeError, OSError):
        raise SettingsError('LLM 설정을 읽을 수 없습니다. 기존 파일을 확인하세요.') from None


def revision(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def environment_key(provider):
    env = PROVIDERS[provider][1]
    return os.getenv(env, '') or (os.getenv('GOOGLE_API_KEY', '') if provider == 'gemini' else '')


AGY_MODELS = [
    {'id': 'gemini-4-argon-high', 'label': 'Gemini 4 Argon (High) · 최고 추론'},
    {'id': 'gemini-4-argon-medium', 'label': 'Gemini 4 Argon (Medium) · 표준'},
    {'id': 'gemini-4-argon-low', 'label': 'Gemini 4 Argon (Low) · 고속'},
    {'id': 'gemini-3.8-flash-high', 'label': 'Gemini 3.8 Flash (High)'},
    {'id': 'gemini-3.8-flash-medium', 'label': 'Gemini 3.8 Flash (Medium)'},
    {'id': 'gemini-3.8-flash-low', 'label': 'Gemini 3.8 Flash (Low)'},
    {'id': 'claude-opus-5-5-high', 'label': 'Claude Opus 5.5 (High)'},
    {'id': 'claude-opus-5-5-medium', 'label': 'Claude Opus 5.5 (Medium)'},
    {'id': 'claude-sonnet-5-5-high', 'label': 'Claude Sonnet 5.5 (High)'},
    {'id': 'claude-sonnet-5-5-medium', 'label': 'Claude Sonnet 5.5 (Medium)'},
    {'id': 'gpt-oss-120b-medium', 'label': 'GPT-OSS 120B (Medium)'},
]


def resolve_agy_executable():
    found = shutil.which('agy')
    if found:
        return found
    win_candidate = Path.home() / 'AppData/Local/agy/bin/agy.exe'
    if win_candidate.is_file():
        return str(win_candidate)
    return None


def is_agy_available():
    if resolve_agy_executable():
        return True
    # Check shared codex-runtime.json to see if Windows worker host is online
    rt = ROOT / 'server' / 'data' / 'codex-runtime.json'
    if rt.is_file():
        return True
    return False


def public(data=None):
    data = load() if data is None else data
    providers = {}
    from common.codex_cli import runtime
    codex = runtime()
    for name, (label, _) in PROVIDERS.items():
        row = data['providers'].get(name, {})
        model_val = row.get('model', '') or ('gemini-4-argon-high' if name == 'agy' else '')
        providers[name] = {'label': label, 'model': model_val,
                           'key_set': bool(row.get('encrypted_key')),
                           'environment_key': bool(environment_key(name)),
                           'installed': is_agy_available() if name == 'agy' else None}
        if name == 'agy':
            providers[name]['models'] = AGY_MODELS
        if name == 'codex':
            providers[name].update(installed=codex.get('installed', False), online=codex.get('online', False),
                                   authenticated=codex.get('authenticated', False), models=codex.get('models', []),
                                   modelsFetchedAt=codex.get('modelsFetchedAt'), checkedAt=codex.get('checkedAt'))
    return {'active': data['active'], 'revision': revision(data), 'providers': providers}


def save(body):
    with LOCK:
        current = load()
        if not isinstance(body, dict) or body.get('revision') != revision(current):
            raise SettingsError('설정이 변경되었습니다. 새로고침 후 다시 저장하세요.')
        provider = body.get('provider')
        if provider not in PROVIDERS and provider != 'legacy':
            raise SettingsError('지원하지 않는 공급자입니다.')
        updated = copy.deepcopy(current)
        if provider != 'legacy':
            model = body.get('model', '')
            if provider == 'agy' and model.lower() in ('argon', '아르곤', 'gemini-4-argon'):
                model = 'gemini-4-argon-high'
            if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}', model):
                raise SettingsError('유효한 모델 ID를 입력하세요.')
            key = body.get('api_key', '')
            if not isinstance(key, str) or len(key) > 4096 or (key and (not key.isascii() or any(c.isspace() for c in key))):
                raise SettingsError('API 키 형식을 확인하세요.')
            row = updated['providers'].setdefault(provider, {})
            row['model'] = model
            if key and provider not in ('agy', 'codex'):
                row['encrypted_key'] = crypt(key)
            if body.get('activate') is True:
                if provider == 'agy' and not is_agy_available():
                    raise SettingsError('AGY CLI를 찾을 수 없습니다.')
                if provider == 'codex':
                    from common.codex_cli import runtime
                    info = runtime()
                    if not info.get('online') or not info.get('authenticated'):
                        raise SettingsError('Windows Codex 작업 호스트·ChatGPT 로그인을 확인하세요.')
                    if model not in {entry['id'] for entry in info.get('models', [])}:
                        raise SettingsError('로그인 계정에서 제공하는 Codex 모델을 선택하세요.')
                if provider not in ('agy', 'codex') and not row.get('encrypted_key') and not environment_key(provider):
                    raise SettingsError('API 키를 입력한 뒤 적용하세요.')
        if body.get('activate') is True:
            updated['active'] = provider
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        # One atomic replacement includes both encrypted key and selection.
        fd, name = tempfile.mkstemp(prefix='llm-settings-', suffix='.tmp', dir=CONFIG.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(updated, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, CONFIG)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return public(updated)
