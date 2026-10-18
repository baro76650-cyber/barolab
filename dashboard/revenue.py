"""Monthly revenue evidence. Run --collect on the browser host; NAS only reads it.

Money is kept in its original currency. Missing report days are never zero-filled.
Credentials and snapshots live outside the public dashboard directory.
"""
import argparse
import hashlib
import hmac
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

from metrics import ROOT, DATA, CONFIG, read_json, atomic_write, collector_lock, stamp, inventory

KST = timezone(timedelta(hours=9))
SNAPSHOT = DATA / 'revenue.json'
CATEGORIES = [('adsense', '구글 애드센스'), ('adpost', '네이버 애드포스트'),
              ('affiliate', '제휴마케팅')]


def adsense_scope(email, domains, states):
    labels = {'READY': 'READY', 'GETTING_READY': '심사 중',
              'REQUIRES_REVIEW': '검토 필요', 'NEEDS_ATTENTION': '조치 필요',
              'STATE_UNSPECIFIED': '상태 미확인', 'not_registered': '미등록'}
    groups = defaultdict(list)
    for domain in domains:
        groups[states.get(domain, 'STATE_UNSPECIFIED')].append(domain)
    parts = [f'{email} · CF 조회 대상 {len(domains)}개']
    for state, members in groups.items():
        label = labels.get(state, '상태 미확인')
        parts.append(f'{label} {len(members)}개' + (f" ({', '.join(members)})" if state != 'READY' else ''))
    return ' · '.join(parts)


def money(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('유효하지 않은 수익 값')
    return result


def report_day(value):
    text = str(value)
    return datetime.strptime(text, '%Y%m%d' if len(text) == 8 else '%Y-%m-%d').date().isoformat()


def source(key, category, label, currency, scope, **extra):
    return dict(id=key, category=category, label=label, currency=currency,
                scope=scope, daily={}, checkedAt=stamp(), valueKind='estimated',
                status='not_collected', reason='수익 보고서 미연결', **extra)


def response_json(response):
    if not response.ok:
        raise ValueError(f'원천 보고서 HTTP {response.status_code}')
    return response.json()


def adsense(cfg, start, end, sites, prefix='SJ_GOOGLE_ADSENSE'):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import AuthorizedSession
    account = cfg[prefix + '_ACCOUNT_ID']
    creds = Credentials(token=None, refresh_token=cfg[prefix + '_REFRESH_TOKEN'],
                        token_uri='https://oauth2.googleapis.com/token',
                        client_id=cfg[prefix + '_CLIENT_ID'],
                        client_secret=cfg[prefix + '_CLIENT_SECRET'])
    with AuthorizedSession(creds) as session:
        base = 'https://adsense.googleapis.com/v2/' + account
        metadata = response_json(session.get(base, timeout=25))
        if metadata.get('timeZone', {}).get('id') != 'Asia/Seoul' or metadata.get('state') != 'READY':
            raise ValueError('애드센스 계정 시간대 또는 활성 상태 불일치')
        params = {'dateRange': 'CUSTOM', 'dimensions': ['DATE', 'DOMAIN_NAME'],
                  'metrics': ['ESTIMATED_EARNINGS'], 'currencyCode': 'USD',
                  'reportingTimeZone': 'ACCOUNT_TIME_ZONE', 'limit': 10000}
        for prefix, day in [('startDate', start), ('endDate', end)]:
            params.update({prefix + '.year': day.year, prefix + '.month': day.month,
                           prefix + '.day': day.day})
        data = response_json(session.get(base + '/reports:generate', params=params, timeout=30))
        registered, next_token = {}, None
        while True:
            page = response_json(session.get(base + '/sites', params={'pageSize': 100, **({'pageToken': next_token} if next_token else {})}, timeout=25))
            for item in page.get('sites', []):
                domain = item.get('domain', '').lower().removeprefix('www.')
                registered[domain] = item.get('state', 'STATE_UNSPECIFIED')
            next_token = page.get('nextPageToken')
            if not next_token:
                break
    headers = data.get('headers', [])
    if [h.get('name') for h in headers] != ['DATE', 'DOMAIN_NAME', 'ESTIMATED_EARNINGS'] or headers[-1].get('currencyCode') != 'USD':
        raise ValueError('애드센스 보고서 열 또는 통화 불일치')
    if int(data.get('totalMatchedRows', 0)) != len(data.get('rows', [])):
        raise ValueError('애드센스 보고서 잘림; 전체 행 재수집 필요')
    values, details, seen = defaultdict(Decimal), [], set()
    for row in data.get('rows', []):
        day, domain, value = [c.get('value') for c in row['cells']]
        domain = domain.lower().removeprefix('www.')
        if domain not in sites:
            continue
        key = (report_day(day), domain)
        if key in seen:
            raise ValueError('애드센스 일별 도메인 행 중복')
        seen.add(key)
        values[key[0]] += money(value)
        details.append({'date': key[0], 'domain': domain, 'amount': str(money(value))})
    return {'daily': {d: str(v) for d, v in values.items()}, 'details': details,
            'status': 'collected',
            'reportSource': 'AdSense Management API v2 · DATE/DOMAIN_NAME',
            'siteStatus': {d: registered.get(d, 'not_registered') for d in sites},
            'registeredDomains': [d for d in sites if d in registered],
            'reason': f'조회 대상 {len(sites)}개 · 계정 등록 {sum(d in registered for d in sites)}개 · 수익 행 있는 날짜만 집계'}


def growth_windows(now=None):
    """Two complete, equal Google reporting periods, excluding three recent days."""
    now = now or datetime.now(timezone.utc)
    end = now.astimezone(ZoneInfo('America/Los_Angeles')).date() - timedelta(days=3)
    recent_start = end - timedelta(days=27)
    return {'recent': {'start': recent_start.isoformat(), 'end': end.isoformat()},
            'previous': {'start': (recent_start - timedelta(days=28)).isoformat(),
                         'end': (recent_start - timedelta(days=1)).isoformat()}}


def parse_growth_report(data, sites, start, end):
    """Validate a complete account report; retain real zero rows and host detail."""
    headers = data.get('headers', [])
    names = ['DATE', 'DOMAIN_CODE', 'ESTIMATED_EARNINGS', 'PAGE_VIEWS']
    if [h.get('name') for h in headers] != names or headers[2].get('currencyCode') != 'USD':
        raise ValueError('growth revenue report columns/currency mismatch')
    rows = data.get('rows', [])
    if int(data.get('totalMatchedRows', 0)) != len(rows):
        raise ValueError('growth revenue report truncated')
    result, seen = [], set()
    for row in rows:
        cells = [c.get('value') for c in row.get('cells', [])]
        if len(cells) != 4:
            raise ValueError('growth revenue invalid row')
        day, host, amount, views = cells
        day = report_day(day)
        if not start <= day <= end:
            raise ValueError('growth revenue date outside requested range')
        host = str(host).lower().rstrip('.')
        domain = host.removeprefix('www.')
        if domain not in sites:
            continue
        key = (day, host)
        if key in seen:
            raise ValueError('growth revenue duplicate date/host')
        seen.add(key)
        views = money(views)
        if views < 0 or views != views.to_integral_value():
            raise ValueError('growth revenue invalid page views')
        result.append({'date': day, 'domain': domain, 'host': host,
                       'revenue': str(money(amount)), 'pageViews': int(views)})
    return result


def adsense_growth(cfg, start, end, sites, prefix='SJ_GOOGLE_ADSENSE'):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import AuthorizedSession
    account = cfg[prefix + '_ACCOUNT_ID']
    creds = Credentials(token=None, refresh_token=cfg[prefix + '_REFRESH_TOKEN'],
                        token_uri='https://oauth2.googleapis.com/token',
                        client_id=cfg[prefix + '_CLIENT_ID'],
                        client_secret=cfg[prefix + '_CLIENT_SECRET'])
    a, b = datetime.fromisoformat(start).date(), datetime.fromisoformat(end).date()
    params = {'dateRange': 'CUSTOM', 'dimensions': ['DATE', 'DOMAIN_CODE'],
              'metrics': ['ESTIMATED_EARNINGS', 'PAGE_VIEWS'], 'currencyCode': 'USD',
              'reportingTimeZone': 'GOOGLE_TIME_ZONE', 'limit': 10000}
    for field, day in [('startDate', a), ('endDate', b)]:
        params.update({field + '.year': day.year, field + '.month': day.month,
                       field + '.day': day.day})
    with AuthorizedSession(creds) as session:
        data = response_json(session.get('https://adsense.googleapis.com/v2/' + account + '/reports:generate',
                                         params=params, timeout=45))
    return parse_growth_report(data, sites, start, end)


def growth_domain_period(rows, window, complete=True):
    observed = [r for r in rows if window['start'] <= r['date'] <= window['end']]
    revenue = sum((money(r['revenue']) for r in observed), Decimal(0))
    views = sum(r['pageViews'] for r in observed)
    observed_days = len({r['date'] for r in observed})
    complete = complete and observed_days == 28
    return dict(window, revenue=float(revenue) if complete else None,
                pageViews=views if complete else None,
                rpm=float(revenue * 1000 / views) if views and complete else None,
                accountRevenue=float(revenue) if observed else None, accountPageViews=views if observed else None,
                accountRpm=float(revenue * 1000 / views) if views else None,
                observedDays=observed_days, expectedDays=28, complete=complete,
                status='collected' if complete else 'partial',
                noRows=not observed)


def collect_growth_revenue(start=None, end=None, force=False, windows=None):
    """Read-only AdSense growth evidence; no missing credentials become zero."""
    from dotenv import dotenv_values
    if isinstance(start, dict):
        windows = start
    elif start and end:
        a, b = (datetime.fromisoformat(str(v)).date() for v in (start, end))
        if (b - a).days != 55:
            raise ValueError('growth revenue needs exactly 56 days')
        windows = {'previous': {'start': a.isoformat(), 'end': (a + timedelta(days=27)).isoformat()},
                   'recent': {'start': (a + timedelta(days=28)).isoformat(), 'end': b.isoformat()}}
    windows = windows or growth_windows()
    if 'current' in windows:
        windows = {'recent': windows['current'], 'previous': windows['previous']}
    for key in ('recent', 'previous'):
        a, b = (datetime.fromisoformat(windows[key][k]).date() for k in ('start', 'end'))
        if (b - a).days != 27:
            raise ValueError('growth revenue windows must each contain 28 days')
    if datetime.fromisoformat(windows['previous']['end']).date() + timedelta(days=1) != datetime.fromisoformat(windows['recent']['start']).date():
        raise ValueError('growth revenue windows must be adjacent')
    with collector_lock(DATA / 'revenue.lock') as acquired:
        if not acquired:
            return read_json(SNAPSHOT).get('growth', {'status': 'busy'})
        document = read_json(SNAPSHOT)
        old = document.get('growth', {})
        if not force and old.get('windows') == windows and old.get('status') in ('collected', 'partial'):
            checked = datetime.fromisoformat(old.get('checkedAt', '2000-01-01T00:00:00+00:00'))
            if datetime.now(timezone.utc) - checked < timedelta(hours=12):
                return old
        cfg = dotenv_values(ROOT / 'cf_posting' / '.env')
        accounts = read_json(CONFIG).get('adsense_accounts', {})
        sources, domains = {}, {}
        for account_key, config in accounts.items():
            prefix = config.get('env_prefix', '')
            sites = config.get('sites', [])
            record = {'account': account_key, 'accountEmail': config.get('email'),
                      'currency': 'USD', 'timezone': 'America/Los_Angeles',
                      'valueKind': 'estimated', 'status': 'not_connected', 'domains': sites}
            connected = prefix and all(cfg.get(prefix + '_' + key) for key in ('ACCOUNT_ID', 'CLIENT_ID', 'CLIENT_SECRET', 'REFRESH_TOKEN'))
            rows = None
            if connected:
                record['attemptedAt'] = stamp()
                try:
                    rows = adsense_growth(cfg, windows['previous']['start'], windows['recent']['end'], sites, prefix)
                    record.update(status='collected', checkedAt=stamp(), rows=rows,
                                  reportSource='AdSense Management API v2 DATE/DOMAIN_CODE ESTIMATED_EARNINGS/PAGE_VIEWS')
                except Exception:
                    record.update(status='error', attemptedAt=stamp(), reason='AdSense growth source failed; missing values remain unknown')
            else:
                record['reason'] = 'AdSense OAuth not connected; GA4/GSC credentials cannot read revenue'
            sources[account_key] = record
            for domain in sites:
                # These domains joined SJ in October 2026. Old account reports are unavailable.
                migrated = domain in ('healthcox.com', 'tripmarvel.com') and account_key == 'sj'
                item = {'domain': domain, 'account': account_key, 'currency': 'USD',
                        'timezone': 'America/Los_Angeles', 'valueKind': 'estimated',
                        'sourceCoverage': 'account_only' if migrated else 'configured_account',
                        'status': record['status'], 'checkedAt': record.get('checkedAt'),
                        'reason': 'SJ 전환 이전 계정 수익 미연결 · 전체 도메인 수익 비교 불가' if migrated else record.get('reason')}
                if rows is not None:
                    own = [r for r in rows if r['domain'] == domain]
                    item.update({k: growth_domain_period(own, w, not migrated or w['start'] >= '2026-10-13') for k, w in windows.items()})
                    item['status'] = 'partial_account_scope' if migrated and any(w['start'] < '2026-10-13' for w in windows.values()) else ('collected' if all(item[k]['complete'] for k in windows) else 'partial')
                    if not all(item[k]['complete'] for k in windows) and not migrated:
                        item['reason'] = '보고서에 없는 날짜는 미확인 · 확인된 날짜의 수익과 광고 페이지뷰만 표시'
                else:
                    item.update({k: dict(w, revenue=None, pageViews=None, rpm=None, status=record['status']) for k, w in windows.items()})
                domains[domain] = item
        result = {'schema': 'dashboard-growth-revenue-v1', 'windows': windows, 'sources': sources,
                  'domains': domains, 'timezone': 'America/Los_Angeles', 'currency': 'USD',
                  'checkedAt': stamp(), 'status': 'partial' if any(r['status'] != 'collected' for r in sources.values()) else 'collected'}
        # On failure preserve the prior successful evidence explicitly as stale, never restamp it.
        for key, record in sources.items():
            if record['status'] == 'error' and old.get('windows') == windows:
                prior = old.get('sources', {}).get(key, {})
                successful = prior if prior.get('status') == 'collected' else prior.get('lastSuccessful', {})
                if successful:
                    record['lastSuccessful'] = successful
                    for domain in record['domains']:
                        previous_domain = old.get('domains', {}).get(domain, {})
                        evidence = previous_domain if previous_domain.get('checkedAt') else previous_domain.get('lastSuccessful')
                        if evidence:
                            domains[domain]['lastSuccessful'] = evidence
        document['growth'] = result
        atomic_write(SNAPSHOT, document)
        return result


def growth_revenue_summary(periods, path=SNAPSHOT):
    """Expose observed account income without treating omitted report days as zero."""
    growth = read_json(path).get('growth', {})
    expected = {'recent': periods.get('current', periods.get('recent')), 'previous': periods['previous']}
    valid = growth.get('windows') == expected
    domains = {}
    for domain, row in growth.get('domains', {}).items():
        item = {k: row.get(k) for k in ('domain', 'account', 'currency', 'sourceCoverage', 'reason')}
        try:
            collected = datetime.fromisoformat(row.get('checkedAt') or '')
            fresh = collected.tzinfo is not None and timedelta(0) <= datetime.now(timezone.utc) - collected <= timedelta(hours=26)
        except (ValueError, TypeError):
            fresh = False
        for target, origin in [('current', 'recent'), ('previous', 'previous')]:
            part = row.get(origin, {}) if valid else {}
            amount = part.get('accountRevenue')
            item[target] = {'amounts': {'USD': amount} if amount is not None else {},
                            'status': 'stale' if part.get('accountRevenue') is not None and not fresh else part.get('status', 'not_collected'),
                            'complete': bool(part.get('complete')) and valid and fresh,
                            'observedDays': part.get('observedDays', 0), 'expectedDays': 28,
                            'pageViews': part.get('accountPageViews'), 'rpm': part.get('accountRpm'),
                            'sourceTimezone': row.get('timezone', 'America/Los_Angeles'),
                            'checkedAt': row.get('checkedAt'),
                            'reason': '수익 원천 수집 후 26시간 경과 · 마지막 확인분이며 증감 비교 제외' if part.get('accountRevenue') is not None and not fresh else row.get('reason'),
                            **expected[origin]}
        domains[domain] = item
        item['decomposition'] = revenue_decomposition(item['current'], item['previous'])
    health = {}
    for key, record in growth.get('sources', {}).items():
        successful = record if record.get('status') == 'collected' else record.get('lastSuccessful', {})
        checked = successful.get('checkedAt')
        retry = None
        if growth.get('checkedAt'):
            try:
                retry = (datetime.fromisoformat(growth['checkedAt']) + timedelta(hours=12)).isoformat()
            except ValueError:
                pass
        health[key] = {'status': record.get('status', 'not_collected'),
                       'lastAttemptAt': record.get('attemptedAt'), 'lastSuccessAt': checked,
                       'nextRetryAt': retry if record.get('status') != 'not_connected' else None,
                       'cacheHours': 12, 'rows': len(successful.get('rows', [])),
                       'domains': len(record.get('domains', [])),
                       'sourceTimezone': record.get('timezone', 'America/Los_Angeles'),
                       'reason': record.get('reason'), 'windowsMatch': valid}
    return {'status': growth.get('status', 'not_collected') if valid else 'period_mismatch',
            'checkedAt': growth.get('checkedAt'), 'domains': domains,
            'sourceTimezone': 'America/Los_Angeles', 'valueKind': 'estimated',
            'collection': {'accounts': health, 'cacheHours': 12, 'periodsMatch': valid}}


def collection_health(collection, now=None):
    """Read-time cadence health. Cached source success is not a worker heartbeat."""
    now = now or datetime.now(timezone.utc)
    def age(value):
        if value is None:
            return None, False
        try:
            moment = datetime.fromisoformat(value)
            seconds = (now - moment).total_seconds() if moment.tzinfo else -1
            return (seconds, True) if seconds >= 0 else (None, False)
        except (ValueError, TypeError):
            return None, False
    runtime = collection.get('runtime') or {}
    heartbeat, heartbeat_valid = age(runtime.get('checkedAt'))
    attempt, attempt_valid = age(collection.get('lastAttemptAt'))
    if not runtime:
        runtime_status, runtime_reason = 'not_started', '수집 작업 런타임 기록 없음'
    elif not heartbeat_valid:
        runtime_status, runtime_reason = 'unknown', '런타임 heartbeat 날짜가 없거나 잘못되었거나 미래임'
    elif runtime.get('status') == 'running' and heartbeat > 120:
        runtime_status, runtime_reason = 'stalled', '실행 중 heartbeat가 120초 넘게 갱신되지 않음 · 중단 여부 확인 필요'
    else:
        runtime_status, runtime_reason = runtime.get('status', 'unknown'), '실행 상태와 원천 수집 성공은 별도'
    if collection.get('lastAttemptAt') is None:
        traffic_status, traffic_reason = 'not_started', '성장 수집 시도 기록 없음'
    elif not attempt_valid:
        traffic_status, traffic_reason = 'unknown', '성장 수집 시도 날짜가 잘못되었거나 미래임'
    elif attempt > 45 * 60:
        traffic_status, traffic_reason = 'collection_delayed', '30분 수집 주기에서 마지막 시도 후 45분 초과 · 스케줄러 확인 필요'
    else:
        traffic_status, traffic_reason = 'on_schedule', '최근 45분 안에 수집 시도 확인 · 캐시 적중이나 원천 대기와 구분'
    status, reason = 'on_schedule', traffic_reason
    for candidate, detail in ((runtime_status, runtime_reason), (traffic_status, traffic_reason)):
        if candidate in ('stalled', 'unknown', 'collection_delayed'):
            status, reason = candidate, detail
            break
    else:
        if runtime_status == 'not_started' and traffic_status == 'not_started':
            status, reason = 'not_started', '아직 연속 수집을 확인할 기록이 없음'
        elif runtime_status == 'partial_failure':
            status, reason = 'partial_failure', '일부 수집 단계 실패 · 개별 원천 상태 확인'
        elif runtime_status == 'running':
            status, reason = 'running', '수집 작업 실행 중 · heartbeat 정상 갱신'
    return {'status': status, 'reason': reason, 'checkedAt': now.isoformat(),
            'runtime': {'status': runtime_status, 'reason': runtime_reason, 'heartbeatAgeSeconds': heartbeat},
            'traffic': {'status': traffic_status, 'reason': traffic_reason, 'lastAttemptAgeSeconds': attempt},
            'heartbeatThresholdSeconds': 120, 'attemptThresholdSeconds': 45 * 60}


def revenue_decomposition(current, previous):
    """Symmetric arithmetic attribution, not a causal estimate of an intervention."""
    result = {'status': 'not_comparable', 'revenueDelta': None, 'pageViewEffect': None,
              'rpmEffect': None, 'residual': None, 'currency': None, 'causalProof': False,
              'reason': '양쪽 28일 전체·같은 통화·신선한 원천과 RPM이 확인되어야 분해 가능'}
    if not current.get('complete') or not previous.get('complete'):
        return result
    currencies = set(current.get('amounts', {})) & set(previous.get('amounts', {}))
    if (len(currencies) != 1 or current.get('sourceTimezone') != previous.get('sourceTimezone')
            or any(p.get('rpm') is None or p.get('pageViews') is None for p in (current, previous))):
        return result
    currency = next(iter(currencies))
    a, b = current['amounts'][currency], previous['amounts'][currency]
    pa, pb, ra, rb = current['pageViews'], previous['pageViews'], current['rpm'], previous['rpm']
    pv_effect = (pa - pb) * (ra + rb) / 2000
    rpm_effect = (ra - rb) * (pa + pb) / 2000
    return {**result, 'status': 'comparable', 'currency': currency, 'revenueDelta': a - b,
            'pageViewEffect': pv_effect, 'rpmEffect': rpm_effect,
            'residual': (a - b) - pv_effect - rpm_effect,
            'reason': '수익=광고 페이지뷰×RPM/1000의 대칭 산술 분해 · 원인이나 개선의 인과효과를 입증하지 않음'}


def browser_json(page, url):
    result = page.evaluate("""async url => {
      const r = await fetch(url, {credentials:'include', signal:AbortSignal.timeout(15000)});
      return {http:r.status, data:r.ok ? await r.json() : null};
    }""", url)
    if result['http'] != 200:
        raise ValueError(f"애드포스트 보고서 HTTP {result['http']}")
    return result['data']


def adpost(start, end):
    from playwright.sync_api import sync_playwright
    publisher = 1224849
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp('http://127.0.0.1:9333', timeout=10000)
        page = next((page for page in browser.contexts[0].pages
                     if page.url.startswith('https://adpost.naver.com/main')), None)
        if page is None:
            raise ValueError('soul5373 애드포스트 로그인 페이지 연결 필요')
        channels = browser_json(page, f'/api/publishers/{publisher}/channels')
        if not any(c.get('alias') == 'nblog.soul5373' and c.get('publisherId') == publisher for c in channels):
            raise ValueError('애드포스트 로그인 계정 불일치')
        query = urlencode({'startDate': start.strftime('%Y%m%d'), 'endDate': end.strftime('%Y%m%d'),
                           'publisherId': publisher, 'clipRequired': 'true'})
        rows = browser_json(page, '/api/report/revenue?' + query)
        # Disconnect only; never close a user's CDP browser or existing tabs.
    if not isinstance(rows, list):
        raise ValueError('애드포스트 수입 보고서 형식 불일치')
    values, details, seen, pending = defaultdict(Decimal), [], set(), set()
    for row in rows:
        day = report_day(row['date'])
        if row.get('mediaTypeName') != 'nblog':
            if not row.get('mediaTypeName'):
                pending.add(day)  # Report placeholders contain zero; these are not observations.
            continue
        key = (day, row['mediaId'], row.get('targetDeviceTypeName'))
        if key in seen:
            raise ValueError('애드포스트 미디어/기기별 행 중복')
        seen.add(key)
        value = money(row['revenueAmount'])
        values[day] += value
        details.append({'date': day, 'media': row['mediaName'], 'mediaId': row['mediaId'],
                        'device': row.get('targetDeviceTypeName'), 'amount': str(value)})
    # A partly generated day must not appear as a complete daily amount.
    for day in pending:
        values.pop(day, None)
    return {'daily': {d: str(v) for d, v in values.items()}, 'details': details,
            'pendingDates': sorted(pending), 'status': 'collected',
            'reason': 'soul5373 계정의 블로그 보고서 · 미집계 날짜는 집계 중',
            'reportSource': '네이버 애드포스트 수입 보고서'}


def coupang(cfg, start, end):
    path = '/v2/providers/affiliate_open_api/apis/openapi/v1/reports/commission'
    query = urlencode({'startDate': start.strftime('%Y%m%d'), 'endDate': end.strftime('%Y%m%d')})
    signed = datetime.now(timezone.utc).strftime('%y%m%dT%H%M%SZ')
    signature = hmac.new(cfg['COUPANG_SECRET_KEY'].encode(),
                         (signed + 'GET' + path + query).encode(), hashlib.sha256).hexdigest()
    auth = f"CEA algorithm=HmacSHA256, access-key={cfg['COUPANG_ACCESS_KEY']}, signed-date={signed}, signature={signature}"
    data = response_json(requests.get('https://api-gateway.coupang.com' + path + '?' + query,
                                     headers={'Authorization': auth}, timeout=25))
    if str(data.get('rCode')) != '0' or not isinstance(data.get('data'), list):
        raise ValueError('쿠팡 수수료 보고서 오류; 인증 또는 조회 기간 확인 필요')
    values, details, seen = defaultdict(Decimal), [], set()
    for row in data['data']:
        day = report_day(row['date'])
        key = (day, row.get('trackingCode'), row.get('subId', ''))
        if key in seen:
            raise ValueError('쿠팡 수수료 보고서 행 중복')
        seen.add(key)
        value = money(row['commission'])  # GMV is sales volume, never revenue.
        values[day] += value
        details.append({'date': day, 'channel': row.get('subId') or '기본 채널', 'amount': str(value)})
    return {'daily': {d: str(v) for d, v in values.items()}, 'details': details,
            'status': 'collected', 'reason': '계정 전체 · YouTube 등 포함 · 보고서 없는 날짜는 미확인',
            'reportSource': '쿠팡 파트너스 Open API · commission'}


def collect(now=None):
    from dotenv import dotenv_values
    now = now or datetime.now(KST)
    today, month = now.date(), now.strftime('%Y-%m')
    start = today.replace(day=1)
    with collector_lock(DATA / 'revenue.lock') as acquired:
        if not acquired:
            return {'status': 'busy'}
        config = read_json(CONFIG)
        accounts = config.get('adsense_accounts', {})
        owned = accounts.get('sj', {}).get('sites', [])
        expected = sum(s['platform'] == 'cf' for s in inventory())
        sources = [source('adsense-sj', 'adsense', 'SJ · CF 애드센스', 'USD',
                          f'SJ 수익 조회 대상 CF {len(owned)}/{expected}개 · 등록/승인 별도 확인', domains=owned),
                   source('adpost-soul5373', 'adpost', 'soul5373 · 애드포스트', 'KRW',
                          'soul5373 계정의 블로그 미디어'),
                   source('coupang', 'affiliate', '쿠팡 파트너스', 'KRW', '쿠팡 계정 전체 · 모든 subId')]
        cfg = dotenv_values(ROOT / 'cf_posting' / '.env')
        previous = read_json(SNAPSHOT)
        old_sources = {s['id']: s for s in previous.get('months', {}).get(month, {}).get('sources', [])}
        jobs = [lambda: adsense(cfg, start, today, owned, accounts.get('sj', {}).get('env_prefix', 'SJ_GOOGLE_ADSENSE')), lambda: adpost(start, today),
                lambda: coupang(cfg, start, today)]
        for record, job in zip(sources, jobs):
            try:
                if record['id'] == 'adsense-sj' and not owned:
                    raise ValueError('CF 소유 도메인 목록 미설정')
                record.update(job())
                for day, value in record['daily'].items():
                    if not start.isoformat() <= day <= today.isoformat():
                        raise ValueError('조회 기간 밖의 수익 행')
                    money(value)
                record.update(checkedAt=stamp(), reportStart=start.isoformat(), reportEnd=today.isoformat())
            except Exception as exc:
                # Exceptions can contain tokens/URLs. Persist only our controlled validation text.
                reason = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else '원천 연결 실패; 이전 수집값 유지'
                old = old_sources.get(record['id'])
                if old and old.get('daily'):
                    record.update(old)
                else:
                    record.update(daily={}, details=[], pendingDates=[])
                record.update(status='error', reason=reason, attemptedAt=stamp())
        family = accounts.get('aaa10291028', {})
        family_record = source('adsense-aaa10291028', 'adsense', '가족 aaa · 애드센스', 'USD', 'nolplace.com · 가족 계정 별도 수집', domains=family.get('sites', []))
        family_prefix = family.get('env_prefix', 'AAA10291028_GOOGLE_ADSENSE')
        if family.get('sites') and all(cfg.get(family_prefix + '_' + key) for key in ('ACCOUNT_ID', 'CLIENT_ID', 'CLIENT_SECRET', 'REFRESH_TOKEN')):
            try:
                family_record.update(adsense(cfg, start, today, family['sites'], family_prefix), reportStart=start.isoformat(), reportEnd=today.isoformat(), checkedAt=stamp())
            except Exception:
                if old_sources.get(family_record['id']):
                    family_record.update(old_sources[family_record['id']])
                family_record.update(status='error', reason='가족 계정 원천 연결 실패; 이전 수집값 유지', attemptedAt=stamp())
        else:
            family_record['reason'] = '가족 계정 AdSense OAuth 미연결 · GA4/Search Console 조회 인증과 별개'
        sources.append(family_record)
        for record in sources:
            if record['id'].startswith('adsense-'):
                account_key = 'sj' if record['id'] == 'adsense-sj' else 'aaa10291028'
                account = accounts.get(account_key, {})
                record.update(accountEmail=account.get('email'), plannedDomains=account.get('sites', []))
                record.setdefault('siteStatus', account.get('site_status', {}))
                if record['id'] == 'adsense-sj':
                    states = record.get('siteStatus', {})
                    absent = [d for d in account.get('sites', []) if states.get(d) == 'not_registered']
                    record['scope'] = adsense_scope(account.get('email', 'sj@must.biz'), account.get('sites', []), states)
                    if absent and account.get('pending_reason'):
                        record['reason'] += ' · ' + account['pending_reason']
        for key, label, reason in [('toss', '토스 쉐어링크', '수익 보고서 미연결'),
                                   ('trip', 'Trip.com', '수익 보고서 미연결'),
                                   ('replyalba', '제휴알바', '수익 보고서 미연결')]:
            category = 'adsense' if key.startswith('adsense') else 'adpost' if key.startswith('adpost') else 'affiliate'
            record = source(key, category, label, 'USD' if category == 'adsense' else 'KRW', label)
            record['reason'] = reason
            sources.append(record)
        previous.setdefault('months', {})[month] = {'sources': sources, 'checkedAt': stamp()}
        previous.update(schema='dashboard-revenue-v1', checkedAt=stamp())
        atomic_write(SNAPSHOT, previous)
    collect_growth_revenue()
    return summary(now=now)


def period(sources, start, end):
    if end < start:
        return {'amounts': {}, 'status': 'not_applicable', 'observedDays': 0}
    dates = [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)]
    totals, observed, pending = defaultdict(Decimal), set(), False
    for record in sources:
        rows = record.get('daily', {})
        for day in dates:
            if day in rows:
                totals[record['currency']] += money(rows[day])
                observed.add(day)
            if day in record.get('pendingDates', []):
                pending = True
    status = 'partial' if totals else 'pending' if pending else 'not_collected'
    return {'amounts': {c: str(v) for c, v in totals.items()}, 'status': status,
            'observedDays': len(observed), 'expectedDays': len(dates),
            'start': start.isoformat(), 'end': end.isoformat(), 'estimated': True}


def summary(path=SNAPSHOT, now=None):
    now = now or datetime.now(KST)
    today, month = now.date(), now.strftime('%Y-%m')
    start, yesterday = today.replace(day=1), today - timedelta(days=1)
    document = read_json(path)
    selected = document.get('months', {}).get(month, {})
    sources = selected.get('sources', [])
    previous_sources = document.get('months', {}).get(yesterday.strftime('%Y-%m'), {}).get('sources', []) if yesterday.month != today.month else []
    if not sources and previous_sources:
        sources = [dict(s, daily={}, pendingDates=[]) for s in previous_sources]
    # Older measurements remain visible with their original timestamp and stale badge.
    for record in sources:
        try:
            record['stale'] = now.astimezone(timezone.utc) - datetime.fromisoformat(record['checkedAt']) > timedelta(hours=2)
        except (ValueError, KeyError, TypeError):
            record['stale'] = True
    windows = [('throughYesterday', start, yesterday), ('yesterday', yesterday, yesterday),
               ('today', today, today), ('month', start, today)]
    def amounts(records):
        ids = {s['id'] for s in records}
        return {key: period([s for s in previous_sources if s['id'] in ids]
                            if key == 'yesterday' and previous_sources else records, a, b)
                for key, a, b in windows}
    categories = [{'id': key, 'label': label, 'sources': [s for s in sources if s['category'] == key],
                   'periods': amounts([s for s in sources if s['category'] == key])}
                  for key, label in CATEGORIES]
    daily = []
    for offset in range(today.day):
        day = start + timedelta(days=offset)
        daily.append({'date': day.isoformat(), 'categories': {
            key: period([s for s in sources if s['category'] == key], day, day)
            for key, _ in CATEGORIES}})
    for record in sources:
        record['periods'] = amounts([record])
        record['lastReportDay'] = max(record.get('daily', {}), default=None)
    return {'status': 'partial' if any(s.get('daily') for s in sources) else 'not_collected',
            'timezone': 'Asia/Seoul', 'month': month, 'today': today.isoformat(),
            'yesterday': yesterday.isoformat(), 'checkedAt': selected.get('checkedAt'),
            'categories': categories, 'totals': amounts(sources), 'daily': daily,
            'note': '확인된 수익만 표시 · 미연결 계정/업체 제외 · 예상 수익이며 정산액과 다를 수 있음'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collect', action='store_true')
    args = parser.parse_args()
    result = collect() if args.collect else summary()
    print(json.dumps({k: v for k, v in result.items() if k not in ('categories', 'daily')}, ensure_ascii=False, indent=2))
