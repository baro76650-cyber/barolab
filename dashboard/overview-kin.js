/* Overview presentation only. The shared adapter owns collection and refreshes. */
(() => {
  'use strict';
  const source = window.BaroLabKinSummary;
  if (!source || !document.getElementById('overview-kin-title')) return;
  const put = (id, value) => { const node = document.getElementById(id); if (node) node.textContent = value; };
  const count = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
  const number = value => count(value) === null ? '미확인' : value.toLocaleString('ko-KR');
  const account = value => ['adinfo0905', 'stone0747'].includes(value) ? value : '계정 미확인';
  const time = value => {
    const date = new Date(value || '');
    return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat('ko-KR', {
      timeZone:'Asia/Seoul', month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit', hour12:false
    }).format(date) + ' KST' : '시각 미확인';
  };
  const reason = value => ({
    answer_history_incomplete:'기존 답변의 중복 여부 확인 미완료',
    own_answer_inventory_incomplete:'기존 답변의 중복 여부 확인 미완료',
    inventory_incomplete:'기존 답변의 중복 여부 확인 미완료',
    review_rejected:'답변 검수 기준 미충족', no_eligible_reviewed_answer:'적합한 검수 완료 답변 없음',
    no_eligible_questions:'현재 답할 수 있는 적합 질문 없음', question_unavailable:'해당 질문에 답변할 수 없음',
    account_protected:'계정 보호 안내 확인 필요', operation_LoginRequiredError:'계정 로그인 확인 필요',
    operation_TimeoutExpired:'작업 시간 초과',
    process_interrupted:'이전 작업 중단으로 결과 재확인 필요'
  })[value] || (value ? '상세 원인 확인 필요' : '별도 원인 기록 없음');

  function render(state) {
    const panel = document.querySelector('.overview-kin-panel');
    panel.dataset.sourceState = state.status;
    if (!state.data) {
      put('overview-kin-state', state.status === 'loading' ? '운영 원장 확인 중' : '운영 원장 조회 실패');
      put('overview-kin-source', '현재 실적·일정 미확인');
      put('overview-kin-task-title', '지식인 운영 원장 확인');
      put('overview-kin-task-detail', '현재 원장을 확인하지 못해 완료 수와 다음 작업을 확정하지 않습니다.');
      put('overview-kin-priority-detail', '현재 운영 원장 조회가 먼저 필요합니다. 블로그 발행 실적에는 영향을 주지 않습니다.');
      return;
    }
    const data = state.data, view = source.summarize(state), auto = view.automation, daily = data.daily || {};
    const today = daily.date === view.today;
    const operation = view.operation;
    const active = operation.status === 'running';
    const ended = auto.cadenceState === 'experiment_completed';
    const last = auto.lastOperation;
    const latest = !state.stale && state.status !== 'error';
    const status = operation.label;
    put('overview-kin-state', status + (latest ? '' : ' · 마지막 확인 기록'));
    const badge = document.getElementById('overview-kin-state');
    badge.className = 'pillar-badge ' + (active ? 'badge-blue' : operation.status === 'waiting' ? 'badge-green' : 'badge-orange');
    put('overview-kin-source', (latest ? '' : '최신 조회·기록 확인 필요 · ') + '운영 원장 ' + time(data.checkedAt) + ' · ' + operation.detail);
    put('overview-kin-verified', view.todayPublished === null ? '미확인' : number(view.todayPublished) + '건');
    put('overview-kin-submissions', today ? '오늘 제출 시도 ' + number(view.todayAttempted) + '회 · 블로그 발행 수와 별도' : '원장 일자 ' + (daily.date || '미확인') + ' · 오늘 실적 미확인');
    const remaining = today ? count(auto.globalRemainingRuns) : null;
    put('overview-kin-remaining', remaining === null ? '미확인' : number(remaining) + '회');
    const budgets = auto.byAccountRemainingRuns || {};
    put('overview-kin-budget', today ? 'adinfo0905 ' + number(count(budgets.adinfo0905)) + '회 · stone0747 ' + number(count(budgets.stone0747)) + '회 · 중단도 회차에 포함' : '오늘 운영 회차 범위 미확인');

    put('overview-kin-next-label', active ? '현재 작업 계정' : '다음 계정 · 최소 가능 시각');
    put('overview-kin-next', active ? account(auto.activeAccount) : ended ? '시험 종료' : account(auto.nextAccount));
    put('overview-kin-next-detail', active ? time(auto.activeStartedAt) + ' 시작 · 종료 후 최소 1시간 대기' :
      auto.nextEligibleAt ? time(auto.nextEligibleAt) + ' 이후 · 5분 주기 조건 확인' : ended ? '측정 보완은 ' + (auto.measurementEndDate || '일정 미확인') + '까지' : '현재 다음 가능 시각 미확인');

    const m = view.measurement;
    put('overview-kin-effect', view.measurementKind === 'baseline' ? '시험 집계 대기' : view.trialSessions === null ? '시험 유입 미확인' : number(view.trialSessions) + ' 세션');
    let measurement = view.measurementKind === 'baseline' ? '비교 기준 ' + m.startDate + ' ~ ' + m.endDate + ' · 시험 유입과 구분' :
      view.measurementKind === 'trial' ? m.startDate + ' ~ ' + m.endDate + ' · 어제까지 UTM 세션' : '시험 기간 전체 수집 여부 확인 필요';
    put('overview-kin-effect-detail', measurement + (view.measurementStale ? ' · 측정 기록 갱신 필요' : '') + ' · 수익 귀속 미확인');

    let followup = '현재 작업과 과거 결과는 구분합니다.';
    if (!last) put('overview-kin-last', '아직 실제 운영 회차 결과를 확인하지 못했습니다.');
    else {
      let result;
      if (last.state === 'started' || (active && last.account === auto.activeAccount)) result = '작업 진행 중';
      else if (last.questionState === 'published_verified' || last.state === 'published_verified') result = '답변 공개 확인';
      else if (last.questionState === 'publishing' || last.questionState === 'submitted_unverified') result = '등록 결과·공개 여부 확인 대기';
      else if (last.submitted === false) result = '등록 전 중단 · ' + reason(last.reason);
      else result = reason(last.reason);
      put('overview-kin-last', account(last.account) + ' · ' + time(last.updatedAt) + ' · ' + result);
      if (last.reason && last.submitted === false) followup = '최근 회차의 결과입니다. 현재 계정 제재나 수정 미완료를 뜻하지 않으며, 조치 결과는 다음 회차에서 확인합니다.';
      else if (last.questionState === 'submitted_unverified' || last.questionState === 'publishing') followup = '완료로 집계하지 않습니다. 같은 질문을 다시 등록하지 않고 공개 상태부터 확인합니다.';
    }
    put('overview-kin-followup', followup);

    let task, detail;
    if (!latest) { task = '운영 원장 갱신 확인'; detail = '마지막 확인 기록입니다. 최신 원장을 확인한 뒤 현재 작업 상태를 판단하세요.'; }
    else if (active) { task = '진행 중 회차 결과 확인'; detail = account(auto.activeAccount) + ' 작업 종료 후 공개 확인 여부와 다음 최소 간격을 확인하세요.'; }
    else if (last && ['publishing','submitted_unverified'].includes(last.questionState)) { task = '답변 공개 여부 먼저 확인'; detail = '제출 결과가 불확실합니다. 재등록 대신 원장과 공개 답변을 대조하세요.'; }
    else if (auto.enabled === false) { task = '자동 운영 설정 확인'; detail = '자동 운영이 꺼져 있습니다. 설정과 실제 작업 등록 상태를 상세 화면에서 확인하세요.'; }
    else if (last && last.submitted === false && last.reason) { task = '최근 중단 후 다음 회차 확인'; detail = reason(last.reason) + ' 기록이 있습니다. 현재 조치 미완료로 단정하지 않고 다음 결과를 확인하세요.'; }
    else { task = '공개 답변과 유입 효과 확인'; detail = '실제 공개 수와 시험 기간 UTM 세션을 비교하고, 방문이나 수익을 게시 수로 추정하지 않습니다.'; }
    put('overview-kin-task-title', task);
    put('overview-kin-task-detail', detail);
    put('overview-kin-priority-detail', task + ' · ' + detail);
    put('overview-kin-operation-link', status + ' · 오늘 공개 ' + number(view.todayPublished) + '건 · 시험 유입 별도 확인');
  }
  source.subscribe(render);
})();
