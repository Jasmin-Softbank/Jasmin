/* The server owns work/control/approval state. Only panel layout is kept in this tab. */
'use strict';
(() => {
  const root = document.getElementById('railshot-ui-flow');
  const find = selector => root.querySelector(selector);
  const all = selector => root.querySelectorAll(selector);
  const layout = {open: false, view: 'terminal', menu: false};
  let snapshot = null, connected = false, pending = false, events = null;
  let refreshInFlight = null, refreshAgain = false, cursor = 0;
  let chatPending = false, renderedMessages = '', historyBefore = null, historyMore = false, historyLoading = false;
  const messageHistory = new Map();
  let ciRun = null, ciSelection = null, ciFetching = null, ciAgain = false;
  const logs = new Map();
  const labels = {QUEUED: '대기', RUNNING: '응답 중', DISPATCHED: '실행 중', PASS: '완료', FAIL: '실패', BLOCKED: '확인 필요', UNKNOWN: '결과 확인 필요', CANCELLED: '취소', NOT_RUN:'미실행'};
  const kinds = {prepare: '빌드 구성', ci: 'CI 검사', terminal: 'VM 명령', deploy: '배포'};
  const stamp = value => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleTimeString('ko-KR', {hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false}) : '시각 미확인';
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  function showError(error) {
    const box = find('[data-error]');
    box.textContent = error ? `${error.message || error.summary || String(error)}` : '';
    box.hidden = !error;
  }
  async function api(path, body) {
    const response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: body === undefined ? {} : {'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(path === '/api/upload' ? 60000 : 15000),
    });
    let value;
    try { value = await response.json(); }
    catch { throw new Error(`서버 응답을 읽을 수 없습니다 (HTTP ${response.status}).`); }
    if (!response.ok) {
      const detail = value.error || value;
      throw new Error(`${detail.code || `HTTP ${response.status}`} · ${detail.summary || '요청을 처리하지 못했습니다.'}`);
    }
    return value;
  }
  function renderLayout() {
    const clip = find('.nv-computer-clip');
    root.classList.toggle('nv-open', layout.open);
    clip.inert = !layout.open;
    clip.setAttribute('aria-hidden', String(!layout.open));
    find('#nv-pet-menu').hidden = !layout.menu;
    find('[data-action="pet"]').setAttribute('aria-expanded', String(layout.menu));
    all('[data-view]').forEach(element => { element.hidden = element.dataset.view !== layout.view; });
    all('[data-tab]').forEach(element => element.setAttribute('aria-pressed', String(element.dataset.tab === layout.view)));
    find('[data-address]').textContent = {ci: 'CI 실행', terminal: 'VM 실행 로그', files: '프로젝트 파일', browser: '배포된 서비스'}[layout.view];
  }
  function renderCI() {
    const ci = ciRun?.job_id === ciSelection ? ciRun : snapshot?.ci?.job_id === ciSelection ? snapshot.ci : null;
    const runs = (snapshot?.jobs || []).filter(job => job.kind === 'ci');
    const select = find('#ci-run');
    const signature = JSON.stringify(runs.map(job => [job.id,job.status]));
    if (select.dataset.runs !== signature) {
      select.replaceChildren();
      runs.forEach(job => {const option=node('option',`${stamp(job.created_at)} · #${job.id.slice(0,8)} · ${labels[job.status] || job.status}`);option.value=job.id;select.append(option);});
      select.dataset.runs=signature;
    }
    if (ciSelection) select.value=ciSelection;
    find('[data-ci-summary]').textContent=ci ? `${ci.completed}/${ci.total}단계 종료 · ${ci.passed}단계 통과 · ${labels[ci.status] || ci.status}` : ciSelection ? '선택한 실행의 상태를 확인하고 있어요.' : '아직 CI 실행 기록이 없어요.';
    find('[data-ci-progress]').value=ci?.percent || 0;
    find('[data-ci-note]').textContent=ci ? `단계 수 기준 진행률 · #${ci.job_id.slice(0,8)}${ci.logs_truncated ? ' · 최근 로그만 표시' : ''}` : '실제 시작·종료 이벤트가 도착하면 갱신됩니다.';
    const list=find('[data-ci-steps]');
    const opened=new Set([...list.querySelectorAll('details[open]')].map(item=>item.dataset.step));
    list.replaceChildren();
    (ci?.steps || []).forEach(step => {
      const li=node('li',''); const detail=node('details',''); detail.dataset.step=step.id; detail.open=opened.has(step.id) || step.status==='RUNNING';
      const summary=node('summary','');
      const marker=node('span',step.status==='PASS'?'✓':step.status==='RUNNING'?'◷':['FAIL','BLOCKED','UNKNOWN'].includes(step.status)?'!':'○','nv-ci-marker'); marker.dataset.status=step.status;
      summary.append(marker,node('span',`${step.id} · ${step.title}`));
      const state=node('small',step.status==='RUNNING'?'실행 중':labels[step.status] || step.status);state.dataset.status=step.status; summary.append(state);
      const time=node('time',step.duration_s == null ? '' : `${Number(step.duration_s).toFixed(1)}s`);
      if (step.status==='RUNNING' && step.started_at) {time.dataset.started=step.started_at;time.textContent='실행 중';}
      summary.append(time); detail.append(summary);
      const logs=(ci.logs || []).filter(row=>row.phase===step.id || row.phase?.startsWith(step.id+'.')).map(row=>row.text).join('');
      detail.append(node('pre',logs || (step.evidence==='NOT_OBSERVED' ? '이 단계의 실행이 아직 관측되지 않았어요.' : '이 단계에 기록된 출력이 없어요.'),'nv-step-log'));
      li.append(detail);list.append(li);
    });
  }
  async function refreshCIRun() {
    if (!ciSelection || layout.view!=='ci' || !connected) return;
    if (ciFetching) {ciAgain=true;return ciFetching;}
    ciFetching=(async()=>{do {ciAgain=false;const id=ciSelection;const result=await api(`/api/ci?job_id=${encodeURIComponent(id)}`);if(id===ciSelection){ciRun=result;renderCI();}}while(ciAgain);})();
    try {await ciFetching;} catch(error){showError(error);} finally {ciFetching=null;}
  }
  find('#ci-run').addEventListener('change',event=>{ciSelection=event.target.value;ciRun=null;renderCI();refreshCIRun();});
  setInterval(()=>all('[data-started]').forEach(element=>{const value=element.dataset.started;const time=Number.isNaN(Number(value))?Date.parse(value):Number(value)*1000;const elapsed=(Date.now()-time)/1000;element.textContent=Number.isFinite(elapsed)&&elapsed>=0?`${elapsed.toFixed(0)}s 경과`:'시각 확인 중';}),1000);
  function appendLog(sequence, text) {
    if (!Number.isSafeInteger(sequence) || typeof text !== 'string' || logs.has(sequence)) return;
    logs.set(sequence, text);
    // ponytail: keep the latest 256 KiB in the DOM; full evidence stays on the server.
    let size = [...logs.values()].reduce((sum, item) => sum + item.length, 0);
    while (size > 262144 && logs.size > 1) {
      const key = logs.keys().next().value;
      size -= logs.get(key).length; logs.delete(key);
    }
    const terminal = find('[data-view="terminal"]');
    const atBottom = terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight < 50;
    terminal.textContent = [...logs.entries()].sort((a, b) => a[0] - b[0]).map(item => item[1]).join('');
    if (atBottom) terminal.scrollTop = terminal.scrollHeight;
  }
  function render() {
    const conversation = find('[data-chat-scroll]');
    const followConversation = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 60;
    const state = snapshot || {};
    if (!ciSelection) ciSelection = state.ci?.job_id || null;
    const ws = state.workspace || {};
    const jobs = state.jobs || [];
    const active = jobs.some(job => ['QUEUED', 'DISPATCHED'].includes(job.status));
    const uncertain = jobs.some(job => job.status === 'UNKNOWN');
    const human = state.control?.owner === 'human';
    const caps = state.capabilities || {};
    const connection = state.connection || {};
    const ready = connected && connection.status === 'CONNECTED';
    const latest = jobs[0];
    const chat = state.chat || {};
    (chat.messages || []).forEach(message => messageHistory.set(message.id, message));
    if (historyBefore === null && chat.before != null) { historyBefore = chat.before; historyMore = Boolean(chat.has_more); }
    const messages = [...messageHistory.values()].sort((a,b) => (a.sequence - b.sequence) || String(a.created_at).localeCompare(String(b.created_at)) || (a.role === b.role ? 0 : a.role === 'user' ? -1 : 1));
    const historyButton = find('[data-action=history]'); historyButton.hidden = !historyMore; historyButton.disabled = historyLoading;
    historyButton.textContent = historyLoading ? '이전 대화를 불러오는 중…' : '이전 대화 불러오기';
    const messagesSignature = JSON.stringify(messages);
    if (messagesSignature !== renderedMessages) {
      const list = find('[data-messages]'); list.replaceChildren();
      messages.forEach(message => {
        const entry = node('article', '', 'nv-chat-message'); entry.dataset.role = message.role;
        entry.dataset.messageId = message.id; entry.setAttribute('aria-label', message.role === 'user' ? '내 메시지' : '봇 메시지');
        entry.append(node('p', message.text || '', 'nv-message-text'));
        if (message.role==='assistant' && message.proposal && message.proposal_review) {
          entry.append(window.renderTopology(message.proposal,message.proposal_review,text=>{
            const input=find('#chat-message'); input.value=text; input.focus({preventScroll:true});
          }));
        }
        if (message.status && message.status !== 'PASS') entry.append(node('span', labels[message.status] || message.status, 'nv-message-meta'));
        list.append(entry);
      });
      renderedMessages = messagesSignature;
    }
    find('[data-chat-welcome]').hidden = messages.length > 0 || Boolean(state.files?.length);
    find('#chat-message').disabled = !connected || !chat.configured || Boolean(chat.error) || chatPending;
    find('#chat-form button').disabled = !connected || !chat.configured || Boolean(chat.error) || chatPending;
    find('[data-chat-status]').textContent = chat.error ? `${chat.error.code} · ${chat.error.summary || '응답 기록을 확인해야 해요.'}` : !chat.configured ? '에이전트 연결을 준비하고 있어요.' : chat.pending ? '누블렛이 답변을 준비하고 있어요. CI 작업은 계속 진행됩니다.' : `${chat.model || 'Agent SDK'} · Enter로 보내기, Shift+Enter로 줄바꿈`;
    find('[data-workspace]').textContent = ws.name || ws.id || '작업 공간';
    find('[data-resource]').textContent = connection.hostname || ws.resource_id || 'VM 연결 확인 중';
    find('[data-machine]').textContent = connection.hostname ? `${connection.hostname} · ${ready ? '연결됨' : '관측 대기'}` : 'VM을 아직 확인하지 못했어요';
    const connectionLabel = !connected ? '서버 연결 확인 중' : ready ? 'VM 연결됨 · 실시간 수신 중' : '서버 연결됨 · VM 준비 확인 중';
    find('[data-connection] span:last-child').textContent = connectionLabel;
    find('[data-connection]').dataset.status = ready ? 'online' : 'offline';
    find('[data-live-status]').textContent = connected ? 'LIVE' : '재연결 중';
    find('[data-transport]').textContent = connected ? '서버 상태 · 실시간 이벤트 연결' : '이벤트 연결이 끊겼어요. 다시 연결하고 있어요.';
    find('[data-upload-count]').textContent = state.files?.length ? `${state.files.length}개 파일` : '';
    find('[data-request]').hidden = !state.files?.length;
    find('[data-request]').textContent = `${ws.name || '프로젝트'} · ${state.files?.length || 0}개 파일을 올렸어요.`;
    find('[data-summary]').textContent = uncertain ? '결과 확인이 필요한 실행이 있어요.' : active ? `${kinds[latest.kind] || latest.kind} ${labels[latest.status] || latest.status}` : state.files?.length ? `${ws.name || '프로젝트'}의 진행 상황` : '프로젝트 폴더를 올리면 빌드 구성을 확인할게요.';
    const jobList = find('[data-jobs]'); jobList.replaceChildren();
    for (const kind of ['prepare','ci','deploy']) {
      const job = jobs.find(item => item.kind === kind && item.same_upload);
      const li = node('li', kinds[kind]);
      const status = node('span', job ? labels[job.status] || job.status : '시작 전', 'nv-step-state');
      status.dataset.status = job?.status || 'NOT_RUN'; li.append(status); jobList.append(li);
    }
    const historyList = find('[data-job-history]'); historyList.replaceChildren();
    find('[data-history-count]').textContent = `실행 이력 ${jobs.length}건`;
    jobs.forEach(job => {
      const li = node('li', `${stamp(job.created_at)} · ${kinds[job.kind] || job.kind} · ${labels[job.status] || job.status}`);
      li.append(node('small', `#${job.id.slice(0,8)}`)); historyList.append(li);
    });
    for (const kind of ['prepare', 'ci']) find(`[data-action="${kind}"]`).disabled = pending || !connected || !caps[kind] || !state.files?.length || active || uncertain || human;
    find('[data-action="upload"]').disabled = pending || !connected || active || uncertain || human;
    find('[data-action-note]').textContent = human ? '수동 제어를 반환하면 검사를 실행할 수 있어요.' : active ? '실제 러너에서 처리 중이에요. 화면을 열어 로그를 확인해 주세요.' : ready && !caps.ci ? `VM은 연결됐지만 CI 환경 검증을 통과하지 못했어요.${connection.readiness_error?.code ? ' '+connection.readiness_error.code : ''}` : '빌드 구성 확인은 준비 검사이며, CI 통과와 구분해 표시합니다.';
    const control = find('[data-action="control"]');
    control.disabled = pending || !connected || !caps.terminal || active || uncertain;
    control.textContent = human ? '봇에게 반환' : '제어권 가져오기';
    find('[data-control-status]').textContent = !connected ? '제어 상태 확인 중' : human ? '수동 제어 · 서버 임대 활성' : active ? '봇 작업 실행 중' : '봇 작업 대기';
    find('#terminal-form').hidden = !human;
    find('#terminal-command').disabled = pending || !connected || !human;
    find('#terminal-form button').disabled = pending || !connected || !human;
    const fileList = find('[data-files]'); fileList.replaceChildren();
    (state.files || []).forEach(file => {
      const li = node('li', file.path);
      li.append(node('small', `${file.bytes ?? '?'} bytes · ${file.sha256 || '해시 미확인'}`)); fileList.append(li);
    });
    const approvals = find('[data-approval-list]'); approvals.replaceChildren();
    (state.allows || []).filter(allow => allow.state === 'PENDING').forEach(allow => {
      const card = node('div', '', 'nv-approval');
      card.append(node('div', `${allow.operation} 승인`, 'nv-approval-title'), node('p', `계획 ${allow.plan_hash}`, 'nv-caption'));
      const actions = node('div', '', 'nv-actions');
      for (const [text, approve] of [['Allow', true], ['거절', false]]) {
        const button = node('button', text, approve ? 'nv-primary' : '');
        button.type = 'button'; button.disabled = pending || !connected;
        button.addEventListener('click', () => mutate(() => api(`/api/allows/${encodeURIComponent(allow.id)}`, {approve})));
        actions.append(button);
      }
      card.append(actions); approvals.append(card);
    });
    // A URL is displayed only after the backend has observed the deployment.
    const link = find('[data-service-link]');
    let serviceURL = null;
    try { const url = new URL(state.deployment_url); if (['https:', 'http:'].includes(url.protocol)) serviceURL = url; } catch { /* absent URL is not a deployment */ }
    link.hidden = !caps.browser || !serviceURL;
    find('[data-tab="browser"]').disabled = link.hidden;
    if (!link.hidden) link.href = serviceURL.href;
    const roots = state.build_roots || [];
    const select = find('#build-root');
    if (JSON.stringify(roots) !== select.dataset.roots) {
      const selected = select.value; select.replaceChildren(node('option', '자동 선택')); select.firstChild.value = '';
      roots.forEach(path => { const option = node('option', path); option.value = path; select.append(option); });
      select.value = selected; select.dataset.roots = JSON.stringify(roots);
    }
    select.hidden = roots.length < 2; find('[data-root-label]').hidden = select.hidden;
    (state.logs || []).forEach(record => appendLog(record.sequence, record.text));
    cursor = Math.max(cursor, state.cursor || 0);
    renderCI();
    if (followConversation) conversation.scrollTop = conversation.scrollHeight;
  }
  async function loadHistory() {
    if (historyLoading || !historyMore || historyBefore === null) return;
    const pane = find('[data-chat-scroll]');
    const anchor = [...all('[data-message-id]')].find(element => element.getBoundingClientRect().bottom > pane.getBoundingClientRect().top);
    const anchorId = anchor?.dataset.messageId, anchorTop = anchor?.getBoundingClientRect().top;
    historyLoading = true; render();
    try {
      const page = await api(`/api/chat?before=${encodeURIComponent(historyBefore)}&limit=20`);
      if (!Number.isSafeInteger(page.before) || page.before >= historyBefore) {
        if (page.has_more) throw new Error('이전 대화의 순서를 확인하지 못했습니다.');
      } else historyBefore = page.before;
      (page.messages || []).forEach(message => messageHistory.set(message.id,message));
      historyMore = Boolean(page.has_more); render();
      const restored = [...all('[data-message-id]')].find(element => element.dataset.messageId === anchorId);
      if (restored) pane.scrollTop += restored.getBoundingClientRect().top - anchorTop;
    } catch (error) { showError(error); }
    finally { historyLoading = false; render(); }
  }
  find('[data-chat-scroll]').addEventListener('scroll', () => {
    if (find('[data-chat-scroll]').scrollTop < 60) loadHistory();
  }, {passive:true});
  async function refresh() {
    if (refreshInFlight) { refreshAgain = true; return refreshInFlight; }
    refreshInFlight = (async () => {
      do {
        refreshAgain = false;
        snapshot = await api('/api/state');
        render(); refreshCIRun();
      } while (refreshAgain);
    })();
    try { await refreshInFlight; } finally { refreshInFlight = null; }
  }
  async function mutate(action) {
    if (pending) return;
    pending = true; showError(null); render();
    try { await action(); await refresh(); }
    catch (error) { showError(error); }
    finally { pending = false; render(); }
  }
  function receive(event) {
    try {
      const record = JSON.parse(event.data);
      const sequence = Number(record.sequence || event.lastEventId);
      cursor = Math.max(cursor, sequence || 0);
      if (record.event === 'log') { appendLog(sequence, record.attributes?.text); refreshCIRun(); return; }
      if (record.type === 'log') appendLog(sequence, record.text);
      else if (record.attributes?.type === 'log') appendLog(sequence, record.attributes.text);
      else if (record.attributes?.record?.type === 'log') appendLog(sequence, record.attributes.record.text);
      refresh().catch(showError);
    } catch (error) { showError(new Error(`실시간 이벤트를 읽지 못했습니다: ${error.message}`)); }
  }
  function connectEvents() {
    events = new EventSource(`/api/events?after=${cursor}`);
    events.onopen = () => { connected = true; render(); refresh().catch(showError); };
    events.onmessage = receive;
    for (const name of ['state', 'log', 'result', 'control']) events.addEventListener(name, receive);
    events.onerror = () => { connected = false; render(); };
  }
  async function encodeFile(file, path) {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = '';
    for (let offset = 0; offset < bytes.length; offset += 32768) binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
    return {path, content_base64: btoa(binary)};
  }
  find('#upload').addEventListener('change', event => mutate(async () => {
    const files = [...event.target.files];
    if (!files.length) return;
    if (files.length > 5000 || files.reduce((sum, file) => sum + file.size, 0) > 50 * 1024 * 1024) throw new Error('업로드는 5,000개 파일, 50 MiB 이하여야 합니다. 빌드 산출물과 의존성 폴더를 제외해 주세요.');
    const name = files[0].webkitRelativePath.split('/')[0];
    const encoded = [];
    for (const file of files) encoded.push(await encodeFile(file, file.webkitRelativePath.split('/').slice(1).join('/') || file.name));
    await api('/api/upload', {name, files: encoded});
    event.target.value = '';
  }));
  find('#terminal-form').addEventListener('submit', event => {
    event.preventDefault(); const input = find('#terminal-command'); const command = input.value.trim();
    if (command) mutate(async () => { await api('/api/terminal', {command, idempotency_key: crypto.randomUUID()}); input.value = ''; });
  });
  find('#chat-form').addEventListener('submit', async event => {
    event.preventDefault();
    const input = find('#chat-message'); const message = input.value.trim();
    if (!message || chatPending) return;
    chatPending = true; showError(null); render();
    try {
      await api('/api/chat', {message, idempotency_key: crypto.randomUUID()});
      input.value = ''; await refresh();
    } catch (error) { showError(error); }
    finally { chatPending = false; render(); input.focus({preventScroll:true}); }
  });
  find('#chat-message').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault(); find('#chat-form').requestSubmit();
    }
  });
  root.addEventListener('click', event => {
    const button = event.target.closest('button'); if (!button || button.disabled) return;
    if (button.dataset.tab) { layout.view = button.dataset.tab; renderLayout(); refreshCIRun(); return; }
    const action = button.dataset.action;
    if (action === 'pet') { layout.menu = !layout.menu; renderLayout(); if (layout.menu) find('[data-action="open"]').focus(); }
    if (action === 'open' || action === 'close') {
      layout.open = action === 'open'; layout.menu = false; renderLayout();
      find(action === 'open' ? '[data-action="close"]' : '[data-action="pet"]').focus();
    }
    if (action === 'history') loadHistory();
    if (action === 'upload') find('#upload').click();
    if (['prepare', 'ci'].includes(action)) mutate(async () => {
      const job=await api('/api/jobs', {kind: action, idempotency_key: crypto.randomUUID(), selected_root: find('#build-root').value || null});
      if (action==='ci') {ciSelection=job.id;ciRun=null;layout.open=true;layout.view='ci';layout.menu=false;renderLayout();}
    });
    if (action === 'control') mutate(() => api('/api/control', {action: snapshot?.control?.owner === 'human' ? 'release' : 'acquire'}));
  });
  root.addEventListener('keydown', event => {
    if (event.key === 'Escape' && layout.menu) { layout.menu = false; renderLayout(); find('[data-action="pet"]').focus(); }
  });
  document.addEventListener('click', event => { if (layout.menu && !find('.nv-pet-wrap').contains(event.target)) { layout.menu = false; renderLayout(); } });
  new ResizeObserver(() => root.style.setProperty('--nv-vm-width', `${find('.nv-layout').getBoundingClientRect().width * .61}px`)).observe(root);
  renderLayout();
  (async () => {
    const token = new URLSearchParams(location.hash.slice(1)).get('token');
    if (token) {
      history.replaceState(null, '', location.pathname);
      await api('/api/session', {token});
    }
    await refresh(); connectEvents();
  })().catch(error => { connected = false; render(); showError(error); });
  window.addEventListener('pagehide', () => events?.close());
})();
