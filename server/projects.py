"""Read-only app inventory. Product manifests never contribute to blog metrics."""
import ipaddress
import json
import math
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter

ROOT = Path(__file__).resolve().parent.parent
router = APIRouter()
PROJECT_REGISTRY = (
    ('bible_app', 'apps/bible/project.json'),
    ('greeting', 'apps/greeting/project.json'),
    ('magnifier', 'apps/magnifier/project.json'),
    ('shopping', 'apps/shopping/project.json'),
)


def public_url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        if parsed.scheme != 'https' or not host or parsed.username or parsed.password:
            return None
        if host == 'localhost' or host.endswith(('.localhost', '.local')) or '.' not in host:
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        return value if len(value) <= 2048 else None
    except ValueError:
        return None


def text(value, limit=240):
    return value[:limit] if isinstance(value, str) else ''


def measurement(raw, metric):
    raw = raw if isinstance(raw, dict) else {}
    value = raw.get(metric)
    known = (raw.get('status') == 'collected' and bool(text(raw.get('checkedAt')))
             and bool(text(raw.get('period'))) and isinstance(value, (int, float))
             and not isinstance(value, bool) and math.isfinite(value) and value >= 0)
    return {'status': 'collected' if known else 'not_connected', metric: value if known else None,
            'currency': text(raw.get('currency'), 8), 'period': text(raw.get('period'), 80),
            'checkedAt': text(raw.get('checkedAt'), 50)}


def load_manifest(manifest, expected_id):
    if not manifest.is_file():
        return {'status': 'missing', 'projects': []}
    try:
        if manifest.stat().st_size > 65536:
            raise ValueError('oversized manifest')
        raw = json.loads(manifest.read_text(encoding='utf-8-sig'))
        if not isinstance(raw, dict) or raw.get('id') != expected_id or not text(raw.get('name')):
            raise ValueError('invalid identity')
        milestones = raw.get('milestones', [])
        if not isinstance(milestones, list):
            raise ValueError('invalid milestones')
        steps = []
        for item in milestones[:20]:
            if not isinstance(item, dict):
                raise ValueError('invalid milestone')
            status = item.get('status')
            status = status if status in {'not_started', 'in_progress', 'verified', 'blocked'} else 'not_started'
            # A checkmark needs a verification date and an evidence reference.
            if status == 'verified' and not (text(item.get('verifiedAt')) and text(item.get('evidence'))):
                status = 'in_progress'
            steps.append({key: text(item.get(key), 500 if key == 'evidence' else 240)
                          for key in ('id', 'label', 'scope', 'verifiedAt', 'evidence')})
            steps[-1]['status'] = status
        metrics = raw.get('measurement') if isinstance(raw.get('measurement'), dict) else {}
        project = {key: text(raw.get(key), 500 if key == 'description' else 240)
                   for key in ('id', 'name', 'description', 'stage', 'updatedAt')}
        project['status'] = raw.get('status') if raw.get('status') in {'development', 'preview', 'released'} else 'development'
        project['publicUrl'] = public_url(raw.get('publicUrl'))
        project['platforms'] = [text(v, 40) for v in raw.get('platforms', [])[:8] if isinstance(v, str)] if isinstance(raw.get('platforms'), list) else []
        project['milestones'] = steps
        project['measurement'] = {'admob': measurement(metrics.get('admob'), 'revenue'),
                                  'analytics': measurement(metrics.get('analytics'), 'activeUsers')}
        return {'status': 'collected', 'projects': [project]}
    except (OSError, ValueError, TypeError):
        return {'status': 'invalid', 'projects': []}


def inventory(root=ROOT):
    projects, sources = [], []
    base = Path(root).resolve()
    for project_id, relative_path in PROJECT_REGISTRY:
        try:
            manifest = (base / relative_path).resolve()
            if not manifest.is_relative_to(base):
                raise ValueError('manifest outside inventory')
            result = load_manifest(manifest, project_id)
        except (OSError, ValueError, TypeError):
            result = {'status': 'invalid', 'projects': []}
        sources.append({'id': project_id, 'status': result['status']})
        projects.extend(result['projects'])
    if len(projects) == len(PROJECT_REGISTRY):
        status = 'collected'
    elif projects:
        status = 'partial'
    else:
        status = 'missing' if all(s['status'] == 'missing' for s in sources) else 'invalid'
    return {'status': status, 'expectedProjects': len(PROJECT_REGISTRY),
            'projects': projects, 'sources': sources}


@router.get('/api/projects')
def get_projects():
    return inventory()
