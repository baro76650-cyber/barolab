/* App projects are separate from blog publication and revenue totals. */
(function () {
  'use strict';
  const list = document.getElementById('app-project-list');
  const state = document.getElementById('app-project-status');
  const filterBar = document.getElementById('app-filter-bar');
  const refreshBtn = document.getElementById('apps-refresh');
  if (!list || !state) return;

  const statuses = {development:'개발 중', preview:'내부 테스트 · 미리보기', released:'정식 출시'};
  const steps = {not_started:'대기', in_progress:'진행 중', verified:'검증 완료', blocked:'확인 필요'};
  let currentFilter = 'all';
  let latestData = null;

  function node(tag, value, className) {
    const el = document.createElement(tag);
    if (value !== undefined && value !== null) el.textContent = value;
    if (className) el.className = className;
    return el;
  }

  function formatKST(isoStr) {
    if (!isoStr) return '';
    const d = new Date(isoStr);
    if (Number.isNaN(d.getTime())) return isoStr;
    return d.toLocaleString('ko-KR', {
      timeZone: 'Asia/Seoul',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false
    }) + ' KST';
  }

  function metric(raw, key, unit) {
    if (raw?.status !== 'collected' || !Number.isFinite(raw[key]) || !raw.period || !raw.checkedAt) return '미연동 · 미수집';
    return `${raw[key].toLocaleString('ko-KR')}${unit} · ${raw.period}`;
  }

  function updateKPIs(data) {
    const projects = Array.isArray(data?.projects) ? data.projects : [];
    const expected = data?.expectedProjects || projects.length || 4;
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    const totalVal = document.getElementById('apps-kpi-total-val');
    if (totalVal) totalVal.innerHTML = `${projects.length} <small style="font-size:14px; color:#64748b;">/ ${expected}개</small>`;
    set('apps-kpi-total-badge', projects.length === expected ? '전체 등록 완료' : `${projects.length}/${expected} 등록`);
    if (projects.length) {
      set('apps-kpi-total-desc', projects.map(p => p.name).join(' · '));
    }

    const previewCount = projects.filter(p => p.status === 'preview' || p.status === 'released').length;
    const releasedCount = projects.filter(p => p.status === 'released').length;
    const relVal = document.getElementById('apps-kpi-release-val');
    if (relVal) relVal.innerHTML = `${previewCount} <small style="font-size:14px; color:#64748b;">개 내부 배포</small>`;
    set('apps-kpi-release-badge', releasedCount > 0 ? `정식 ${releasedCount}개` : '내부 테스트 단계');
    set('apps-kpi-release-desc', `정식 출시 ${releasedCount}개 · 내부 테스트 ${previewCount}개 · 실기기 검증 진행 중`);

    let verifiedMs = 0, inProgressMs = 0, waitingMs = 0;
    for (const p of projects) {
      for (const m of (p.milestones || [])) {
        if (m.status === 'verified') verifiedMs++;
        else if (m.status === 'in_progress') inProgressMs++;
        else waitingMs++;
      }
    }
    const totalMs = verifiedMs + inProgressMs + waitingMs;
    const msVal = document.getElementById('apps-kpi-milestone-val');
    if (msVal) msVal.innerHTML = `${verifiedMs} <small style="font-size:14px; color:#64748b;">/ ${totalMs}건 완료</small>`;
    set('apps-kpi-milestone-badge', totalMs > 0 ? `${Math.round((verifiedMs / totalMs) * 100)}% 검증` : '집계 중');
    set('apps-kpi-milestone-desc', `진행 중 ${inProgressMs}건 · 대기 ${waitingMs}건`);
  }

  function renderFilterBar(projects) {
    if (!filterBar) return;
    filterBar.replaceChildren();
    const items = [{id: 'all', label: `전체 (${projects.length})`}].concat(
      projects.map(p => ({id: p.id, label: p.name}))
    );
    for (const item of items) {
      const btn = node('button', item.label, 'app-filter-btn' + (currentFilter === item.id ? ' active' : ''));
      btn.type = 'button';
      btn.onclick = () => {
        currentFilter = item.id;
        if (latestData) render(latestData);
      };
      filterBar.append(btn);
    }
  }

  function render(data) {
    latestData = data;
    list.replaceChildren();
    if (!['collected','partial'].includes(data.status) || !Array.isArray(data.projects) || !data.projects.length) {
      state.textContent = data.status === 'invalid' ? '프로젝트 등록 정보 확인 필요' : '등록 정보 준비 중 · 수익과 이용자 수는 미수집';
      return;
    }
    updateKPIs(data);
    renderFilterBar(data.projects);

    const unavailable = (data.sources || []).filter(source => source.status !== 'collected');
    const names = {bible_app:'성경말씀',greeting:'인사말',magnifier:'돋보기',shopping:'장보기'};
    const labels = {missing:'등록 대기',invalid:'등록 정보 확인 필요'};
    state.textContent = `${data.projects.length}/${data.expectedProjects || data.projects.length}개 앱 프로젝트 · 블로그 실적과 별도 관리`
      + (unavailable.length ? ' · ' + unavailable.map(source => `${names[source.id] || source.id}: ${labels[source.status] || '미확인'}`).join(' / ') : '');

    const visibleProjects = currentFilter === 'all'
      ? data.projects
      : data.projects.filter(p => p.id === currentFilter);

    for (const project of visibleProjects) {
      const card = node('article', '', 'app-project-card');
      const heading = node('div', '', 'traffic-head');
      const vCount = (project.milestones || []).filter(m => m.status === 'verified').length;
      const tCount = (project.milestones || []).length;
      const badges = node('div', '', 'app-project-head-badges');
      badges.append(
        node('span', `검증 ${vCount}/${tCount}건`, 'pillar-badge badge-green'),
        node('span', statuses[project.status] || '상태 미확인', 'pillar-badge badge-blue')
      );
      heading.append(node('h3', project.name), badges);
      card.append(heading, node('p', project.description, 'section-caption'));
      card.append(node('p', `${(project.platforms || []).join(' · ')} · ${project.stage || '단계 확인 중'}`, 'revenue-meta'));

      const metrics = node('div', '', 'app-project-metrics');
      metrics.append(node('p', `AdMob 수익: ${metric(project.measurement?.admob, 'revenue', ' ' + (project.measurement?.admob?.currency || 'USD'))}`));
      metrics.append(node('p', `활성 이용자: ${metric(project.measurement?.analytics, 'activeUsers', '명')}`));
      card.append(metrics);

      const milestones = node('ul', '', 'app-project-milestones');
      for (const step of project.milestones || []) {
        const item = node('li', '', `st-${step.status || 'not_started'}`);
        const prefix = step.status === 'verified' ? '✓ ' : (step.status === 'in_progress' ? '⚡ ' : '');
        item.append(node('strong', `${prefix}${step.label || step.id} · ${steps[step.status] || '미확인'}`));
        if (step.scope) item.append(node('span', step.scope));
        if (step.status === 'verified' && step.verifiedAt) item.append(node('small', `검증 ${formatKST(step.verifiedAt)}`));
        milestones.append(item);
      }
      card.append(milestones);

      const footer = node('div', '', 'app-project-footer');
      if (project.publicUrl) {
        try {
          const url = new URL(project.publicUrl);
          if (url.protocol === 'https:' && !url.username && !url.password) {
            const link = node('a', '앱 안내 웹페이지 열기 ↗', 'overview-button');
            link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
            footer.append(link);
          }
        } catch (_) { /* Invalid URL remains unlinked. */ }
      } else {
        footer.append(node('span', '공개 링크 준비 중', 'revenue-note'));
      }
      if (project.updatedAt) {
        footer.append(node('span', `등록 정보 갱신 ${formatKST(project.updatedAt)}`, 'revenue-note'));
      }
      card.append(footer);
      list.append(card);
    }
  }

  async function refresh() {
    try {
      const res = await fetch('/api/projects', {cache:'no-store'});
      if (!res.ok) throw new Error('projects unavailable');
      render(await res.json());
    } catch (_) {
      list.replaceChildren(); state.textContent = '프로젝트 정보 조회 불가 · 수익과 이용자 수 미확인';
    }
  }

  if (refreshBtn) refreshBtn.addEventListener('click', refresh);
  refresh(); setInterval(refresh, 60000);
})();
