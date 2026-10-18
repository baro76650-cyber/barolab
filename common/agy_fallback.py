"""Bounded text draft fallback with rate-limit cooldown for agy CLI.
Prevents Google account lockouts (Further action is required) by strictly enforcing
a minimum 20-second cooldown between calls.
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

MODELS = {
    'simple': 'gemini-3.8-flash-low',
    'standard': 'gemini-3.8-flash-medium',
    'complex': 'gemini-3.1-pro-low',
}

COOLDOWN_SECONDS = 20.0
COOLDOWN_FILE = Path.home() / '.barolab' / 'scratch' / 'agy_cooldown.json'


def enforce_agy_cooldown(min_interval=COOLDOWN_SECONDS):
    """Enforce minimum cooldown interval between agy CLI calls across processes."""
    COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    if COOLDOWN_FILE.is_file():
        try:
            data = json.loads(COOLDOWN_FILE.read_text(encoding='utf-8'))
            last_call = float(data.get('last_call_epoch', 0.0))
            elapsed = now - last_call
            if elapsed < min_interval:
                sleep_sec = min_interval - elapsed
                time.sleep(sleep_sec)
        except Exception:
            pass
    # Update timestamp
    try:
        COOLDOWN_FILE.write_text(json.dumps({'last_call_epoch': time.time()}), encoding='utf-8')
    except Exception:
        pass


def compact_policy(text: str, max_chars: int = 900) -> str:
    """Diet bloated policy strings to core rules while preserving essential constraints."""
    if not isinstance(text, str) or not text.strip():
        return ''
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Skip verbose examples ("예를 들어 ...") to keep only core rules
        if line.startswith(('예를 들어', '이 예문을')):
            continue
        lines.append(line)
    compacted = '\n'.join(lines)
    if len(compacted) <= max_chars:
        return compacted
    return compacted[:max_chars].rsplit('\n', 1)[0].strip()


def generate(router, task, level='standard', run=subprocess.run):
    model = MODELS.get(level, level)
    executable = shutil.which('agy')
    if not executable:
        raise RuntimeError('agy_unavailable')

    # Rate-limit cooldown to avoid Google account lockouts (15~20s)
    enforce_agy_cooldown(COOLDOWN_SECONDS)

    start = time.monotonic()
    entry = dict(provider='agy', requested_model=model, returned_model='unknown', attempt=1, status='failed')
    try:
        prompt = (
            'Return only a bounded draft, without tools, filesystem access or shell commands. '
            'Do not execute or apply changes. All output needs supervisor review.\n' + router.prepare(task)
        )
        # Timeout 130s (--print-timeout 120s) to prevent premature cutoff
        result = run(
            [executable, '-p', prompt, '--model', model, '--sandbox',
             '--disable-slash-commands', '--print-timeout', '120s', '--output-format', 'json'],
            cwd=router.project, capture_output=True, encoding='utf-8', errors='replace',
            timeout=130, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        )
        if result.returncode or len(result.stdout) > 131072:
            raise ValueError('agy_failed')
        raw = json.loads(result.stdout)
        draft = raw.get('response')
        entry['usage'] = raw.get('usage', {})
        entry['returned_model'] = router.model_name(raw.get('model'))
        if raw.get('status') != 'SUCCESS' or not isinstance(draft, str) or not draft.strip() or len(draft) > 24000:
            raise ValueError('agy_invalid')
        entry['status'] = 'ok'
        return router.redact(draft)
    finally:
        # Re-stamp cooldown after execution
        try:
            COOLDOWN_FILE.write_text(json.dumps({'last_call_epoch': time.time()}), encoding='utf-8')
        except Exception:
            pass
        entry['latency_ms'] = round((time.monotonic() - start) * 1000)
        router.ledger(entry)
