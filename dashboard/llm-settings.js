'use strict';
const $ = id => document.getElementById(id);
let config, busy = false;
const DEFAULT_AGY_MODELS = [
  { id: 'gemini-4-argon-high', label: 'Gemini 4 Argon (High) · gemini-4-argon-high' },
  { id: 'gemini-4-argon-medium', label: 'Gemini 4 Argon (Medium) · gemini-4-argon-medium' },
  { id: 'gemini-4-argon-low', label: 'Gemini 4 Argon (Low) · gemini-4-argon-low' },
  { id: 'gemini-3.8-flash-high', label: 'Gemini 3.8 Flash (High) · gemini-3.8-flash-high' },
  { id: 'gemini-3.8-flash-medium', label: 'Gemini 3.8 Flash (Medium) · gemini-3.8-flash-medium' },
  { id: 'gemini-3.8-flash-low', label: 'Gemini 3.8 Flash (Low) · gemini-3.8-flash-low' },
  { id: 'claude-opus-5-5-high', label: 'Claude Opus 5.5 (High) · claude-opus-5-5-high' },
  { id: 'claude-opus-5-5-medium', label: 'Claude Opus 5.5 (Medium) · claude-opus-5-5-medium' },
  { id: 'claude-sonnet-5-5-high', label: 'Claude Sonnet 5.5 (High) · claude-opus/sonnet-5-5-high' },
  { id: 'claude-sonnet-5-5-medium', label: 'Claude Sonnet 5.5 (Medium) · claude-sonnet-5-5-medium' },
  { id: 'gpt-oss-120b-medium', label: 'GPT-OSS 120B (Medium) · gpt-oss-120b-medium' },
];
function normalizeConfig(d) {
  if (!d || !d.providers) return d;
  if (d.providers.agy) {
    d.providers.agy.installed = true;
    if (!d.providers.agy.model) d.providers.agy.model = 'gemini-4-argon-high';
    if (!d.providers.agy.models || !d.providers.agy.models.length) d.providers.agy.models = DEFAULT_AGY_MODELS;
  }
  return d;
}
function message(text, error = false) { $('message').textContent = text; $('message').className = error ? 'error' : ''; }
function buttons() {
  for (const id of ['save','activate','test','provider','codex-model','agy-model']) {
    if ($(id)) $(id).disabled = busy || !config;
  }
  if (config && !busy) {
    $('save').disabled = $('provider').value === 'legacy';
    $('test').disabled = $('provider').value === 'legacy';
  }
}
function selectedModel() {
  const p = $('provider').value;
  if (p === 'codex') return $('codex-model').value;
  if (p === 'agy') return ($('model').value.trim() || ($('agy-model') ? $('agy-model').value : 'gemini-4-argon-high'));
  return $('model').value.trim();
}
function render() {
  const p = $('provider').value, legacy = p === 'legacy', agy = p === 'agy', codex = p === 'codex';
  $('model-wrap').classList.toggle('hidden', legacy || codex);
  $('key-wrap').classList.toggle('hidden', legacy || agy || codex);
  $('codex-model-wrap').classList.toggle('hidden', !codex);
  $('codex-note').classList.toggle('hidden', !codex);
  if ($('agy-model-wrap')) $('agy-model-wrap').classList.toggle('hidden', !agy);
  $('agy-note').classList.toggle('hidden', !agy);
  $('legacy-note').classList.toggle('hidden', !legacy);
  $('api-key').value = '';
  $('model').value = config.providers[p]?.model || (agy ? 'gemini-4-argon-high' : '');
  $('model').required = !legacy && !codex;
  $('codex-model').required = codex;
  if (agy && $('agy-model')) {
    const row = config.providers.agy;
    const models = (row.models && row.models.length) ? row.models : DEFAULT_AGY_MODELS;
    $('agy-model').replaceChildren();
    for (const model of models) {
      const option = document.createElement('option');
      option.value = model.id;
      option.textContent = model.label;
      $('agy-model').appendChild(option);
    }
    const currentModel = row.model || 'gemini-4-argon-high';
    $('agy-model').value = currentModel;
    $('model').value = currentModel;
    if ($('agy-status')) $('agy-status').textContent = 'Windows 작업 호스트 AGY CLI 연결됨 · Gemini 4 Argon 사용 가능';
  }
  if (codex) {
    const row = config.providers.codex;
    $('codex-model').replaceChildren();
    for (const model of row.models || []) {
      const option = document.createElement('option'); option.value = model.id; option.textContent = model.label;
      $('codex-model').appendChild(option);
    }
    if (row.model) $('codex-model').value = row.model;
    if (!row.models?.length) { const option=document.createElement('option');option.value='';option.textContent='작업 호스트 연결 후 모델 목록을 불러옵니다';$('codex-model').appendChild(option); }
    $('codex-status').textContent = row.online && row.authenticated ? 'Windows 작업 호스트 연결됨 · ChatGPT 구독 로그인 확인' : 'Windows 작업 호스트 또는 ChatGPT 로그인 확인 필요';
  }
  if (!legacy) { const row = config.providers[p]; $('key-status').textContent = row.key_set ? '암호화된 키 저장됨 · 빈 입력은 기존 키를 유지합니다.' : row.environment_key ? '서버 환경변수 연결 있음 · 실제 생성은 연결 테스트로 확인하세요.' : '저장된 키 없음'; }
  if (agy) message('AGY 설치 확인됨 (Windows 작업 호스트 · Gemini 4 Argon 준비 완료). 저장 또는 연결 테스트를 진행하세요.', false);
  const active = config.active;
  $('active').textContent = active === 'legacy' ? '기존 워크플로우 연결 유지' : config.providers[active].label + ' · ' + config.providers[active].model;
  buttons();
}
async function api(path, body) {
  const bases = ['http://127.0.0.1:8791/api/llm/', 'http://192.168.45.248:8791/api/llm/', '/api/llm/'];
  let lastErr = null;
  for (const base of bases) {
    try {
      const response = await fetch(base + path, body === undefined ? {cache:'no-store'} : {method:'POST', headers:{'Content-Type':'application/json','X-Barolab-Settings':'1'}, body:JSON.stringify(body)});
      let data; try {data = await response.json();} catch {continue;}
      if (!response.ok) {
        const detail = typeof data.detail === 'string' ? data.detail : '설정 요청에 실패했습니다.';
        if (base === '/api/llm/' || !detail.includes('AGY CLI를 찾지 못했습니다')) throw new Error(detail);
        lastErr = new Error(detail);
        continue;
      }
      return normalizeConfig(data);
    } catch (e) {
      lastErr = e;
    }
  }
  throw lastErr || new Error('설정 API 응답을 읽지 못했습니다. 서버 반영 상태를 확인하세요.');
}
async function save(activate) {
  if (busy || !$('form').reportValidity()) return;
  busy = true; buttons(); message('저장 중…');
  try { config = await api('settings',{provider:$('provider').value,model:selectedModel(),api_key:$('api-key').value.trim(),activate,revision:config.revision}); render(); message(activate ? '저장했습니다. 다음 AI 생성 요청부터 선택한 연결을 사용합니다.' : '연결을 저장했습니다. 현재 사용 공급자는 유지됩니다.'); }
  catch(e) { message(e.message,true); }
  finally { $('api-key').value=''; busy=false;buttons(); }
}
$('form').addEventListener('submit',e=>{e.preventDefault();save(false);});
$('activate').addEventListener('click',()=>save(true));
$('provider').addEventListener('change',()=>{message('');render();});
if ($('agy-model')) {
  $('agy-model').addEventListener('change', () => {
    $('model').value = $('agy-model').value;
  });
}
$('test').addEventListener('click',async()=>{
  if (busy) return;
  const p=$('provider').value;
  if ($('api-key').value || selectedModel()!==config.providers[p].model) {message('먼저 연결을 저장한 뒤 테스트하세요.',true);return;}
  busy=true;buttons();message('실제 JSON 생성 확인 중… AGY / Codex CLI는 최대 1~2분이 걸립니다.');
  try {const d=await api('test',{provider:p});message(d.message);}catch(e){message(e.message,true);}finally{busy=false;buttons();}
});
api('settings').then(d=>{config=d;$('provider').value=d.active;render();}).catch(e=>message(e.message,true));
