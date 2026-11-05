const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
let connected = false;
let summary = null;
const titles = {
  overview: ['集群总览', '查看集群健康状态和资源数量。'],
  pods: ['Pods', '按 Namespace 浏览 Pod 运行状态。'],
  nodes: ['Nodes', '查看集群节点状态、版本和内部 IP。'],
  namespaces: ['Namespaces', '查看各 Namespace 中的 Pod 数量。'],
  resources: ['资源管理', '浏览、查看与编辑集群资源。'], publish: ['资源发布', '提交 YAML 多文档资源发布任务。'],
  metrics: ['监控指标', '查看集群 CPU 与内存使用量。'],
};

async function api(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const response = await fetch(url, { credentials: 'same-origin', ...options, headers });
  const result = response.status === 204 ? {} : await response.json();
  if (response.status === 401) { showLogin('登录状态已过期，请重新登录。'); throw new Error('请重新登录'); }
  if (!response.ok || result.error) throw new Error(result.error || `请求失败 (${response.status})`);
  return result;
}
function showLogin(message = '') {
  $('#app').classList.add('hidden'); $('#login-screen').classList.remove('hidden');
  $('#login-error').textContent = message; $('#password-input').focus();
}
function showApp() { $('#login-screen').classList.add('hidden'); $('#app').classList.remove('hidden'); }
function setConnection(isConnected, context = '') {
  connected = isConnected;
  $('.cluster-status').classList.toggle('connected', isConnected);
  $('#sidebar-dot').classList.toggle('online', isConnected);
  $('#sidebar-status').textContent = isConnected ? '已连接' : '未连接';
  $('#sidebar-context').textContent = context || 'kubeconfig.yaml';
  $('#connection-alert').classList.toggle('hidden', isConnected);
  $('#refresh-button').disabled = !isConnected;
}
function selectView(name) {
  $$('.nav-item').forEach((button) => button.classList.toggle('active', button.dataset.view === name));
  $$('.view-panel').forEach((panel) => panel.classList.toggle('hidden', panel.id !== `view-${name}`));
  $('#page-title').textContent = titles[name][0]; $('#page-subtitle').textContent = titles[name][1];
}
function emptyRow(body, cols, message) {
  const row = document.createElement('tr'); const cell = document.createElement('td');
  cell.colSpan = cols; cell.className = 'empty-cell'; cell.textContent = message; row.append(cell); body.replaceChildren(row);
}
function renderNamespaces(items) {
  const nav = $('#namespace-nav'); nav.replaceChildren();
  if (!items.length) { nav.innerHTML = '<div class="side-empty">没有 Namespace</div>'; return; }
  items.forEach((item) => {
    const button = document.createElement('button'); button.className = 'namespace-nav-item'; button.type = 'button';
    button.textContent = item.name; const count = document.createElement('b'); count.textContent = item.pod_count; button.append(count);
    button.addEventListener('click', () => { selectView('pods'); $('#namespace-select').value = item.name; loadPods(item.name); }); nav.append(button);
  });
  const select = $('#namespace-select'); const previous = select.value;
  select.replaceChildren();
  items.forEach((item) => { const option = document.createElement('option'); option.value = item.name; option.textContent = `${item.name} (${item.pod_count})`; select.append(option); });
  if (items.some((item) => item.name === previous)) select.value = previous;
}
function renderNodes(nodes) {
  const body = $('#node-rows'); body.replaceChildren();
  if (!nodes.length) return emptyRow(body, 5, '没有 Node');
  nodes.forEach((node) => {
    const row = document.createElement('tr');
    const values = [node.name, node.ready ? 'Ready' : 'NotReady', node.roles.join(', ') || 'worker', node.version || '—', node.ip || '—'];
    values.forEach((value, index) => { const cell = document.createElement('td'); cell.textContent = value; if (index === 1) { const pill = document.createElement('span'); pill.className = `status-pill ${node.ready ? 'status-running' : 'status-failed'}`; pill.textContent = value; cell.replaceChildren(pill); } if (index === 0) cell.className = 'pod-name'; row.append(cell); }); body.append(row);
  });
  const overview = $('#node-overview'); overview.replaceChildren();
  nodes.slice(0, 6).forEach((node) => { const item = document.createElement('div'); item.className = 'node-mini'; const dot = document.createElement('i'); dot.className = node.ready ? 'online' : 'offline'; const text = document.createElement('strong'); text.textContent = node.name; const state = document.createElement('small'); state.className = node.ready ? '' : 'not-ready'; state.textContent = node.ready ? 'Ready' : 'NotReady'; item.append(dot, text, state); overview.append(item); });
  if (!nodes.length) overview.textContent = '没有 Node';
}
function renderNamespaceTable(items) {
  const box = $('#namespace-table'); box.replaceChildren();
  if (!items.length) { box.textContent = '没有 Namespace'; return; }
  const table = document.createElement('table'); table.innerHTML = '<thead><tr><th>Namespace</th><th>Pod 数量</th><th>操作</th></tr></thead>';
  const body = document.createElement('tbody');
  items.filter(i => !$('#namespace-search')?.value || i.name.includes($('#namespace-search').value)).forEach((item) => { const row = document.createElement('tr'); const name = document.createElement('td'); name.className = 'pod-name'; name.textContent = item.name; const count = document.createElement('td'); count.textContent = item.pod_count; const action = document.createElement('td'); const button = document.createElement('button'); button.className = 'text-button'; button.type = 'button'; button.textContent = '查看 Pod →'; button.addEventListener('click', () => { selectView('pods'); $('#namespace-select').value = item.name; loadPods(item.name); }); action.append(button); row.append(name, count, action); body.append(row); });
  table.append(body); box.append(table);
}
function renderShortcuts(items) {
  const box = $('#namespace-shortcuts'); box.replaceChildren();
  items.slice(0, 8).forEach((item) => { const button = document.createElement('button'); button.className = 'namespace-shortcut'; button.type = 'button'; const name = document.createElement('strong'); name.textContent = item.name; const count = document.createElement('small'); count.textContent = `${item.pod_count} Pods`; button.append(name, count); button.addEventListener('click', () => { selectView('pods'); $('#namespace-select').value = item.name; loadPods(item.name); }); box.append(button); });
  if (!items.length) box.textContent = '没有 Namespace';
}
async function loadSummary() {
  if (!connected) return;
  $('#refresh-button').disabled = true; $('#last-updated').textContent = '正在刷新…';
  try {
    summary = await api('/api/summary');
    $('#pod-total').textContent = `${summary.pod_count.toLocaleString()}${summary.pods_truncated ? '+' : ''}`;
    $('#pod-note').textContent = summary.pods_truncated ? '已达统计上限，下限值' : '集群范围';
    $('#node-total').textContent = summary.node_count.toLocaleString(); $('#node-ready').textContent = `${summary.ready_node_count} 个 Ready`;
    $('#namespace-total').textContent = summary.namespace_count.toLocaleString();
    $('#nav-pod-count').textContent = summary.pod_count.toLocaleString(); $('#nav-node-count').textContent = summary.node_count; $('#nav-ns-count').textContent = summary.namespace_count;
    renderNamespaces(summary.namespaces); renderNodes(summary.nodes); renderNamespaceTable(summary.namespaces); renderShortcuts(summary.namespaces);
    $('#last-updated').textContent = `更新于 ${new Date().toLocaleTimeString()}`;
    if ($('#view-pods').classList.contains('hidden') === false && $('#namespace-select').value) await loadPods($('#namespace-select').value);
  } catch (error) { $('#last-updated').textContent = '加载失败'; $('#connection-error').textContent = error.message; }
  finally { $('#refresh-button').disabled = !connected; }
}
async function loadPods(namespace) {
  if (!namespace || !connected) return;
  const body = $('#pod-rows'); emptyRow(body, 6, '正在加载 Pod…'); $('#pod-footer').textContent = '';
  try {
    const result = await api(`/api/pods?namespace=${encodeURIComponent(namespace)}`); body.replaceChildren();
    if (!result.pods.length) return emptyRow(body, 6, '此 Namespace 中没有 Pod');
    result.pods.forEach((pod) => {
      const row = document.createElement('tr'); const name = document.createElement('td'); name.className = 'pod-name'; name.textContent = pod.name;
      const status = document.createElement('td'); const pill = document.createElement('span'); pill.className = `status-pill status-${pod.phase.toLowerCase().replace(/[^a-z]/g, '')}`; pill.textContent = pod.phase; status.append(pill);
      const ready = document.createElement('td'); ready.textContent = pod.ready; const node = document.createElement('td'); node.textContent = pod.node || '—';
      const restarts = document.createElement('td'); restarts.textContent = pod.restarts; const created = document.createElement('td'); created.textContent = pod.created || '—'; row.append(name, status, ready, node, restarts, created); body.append(row);
    });
    $('#pod-caption').textContent = `Namespace：${namespace}`; $('#pod-footer').textContent = result.truncated ? `超过 ${result.limit} 个，仅显示前 ${result.pods.length} 个。` : `共 ${result.pods.length.toLocaleString()} 个 Pod`;
  } catch (error) { emptyRow(body, 6, error.message); }
}

$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const button = $('button[type="submit"]', event.currentTarget); button.disabled = true; $('#login-error').textContent = '';
  try { await api('/api/login', { method: 'POST', body: JSON.stringify({ password: $('#password-input').value }) }); $('#password-input').value = ''; showApp(); const state = await api('/api/status'); setConnection(state.connected, state.context); if (state.error) $('#connection-error').textContent = state.error; if (state.connected) await loadSummary(); }
  catch (error) { $('#login-error').textContent = error.message; }
  finally { button.disabled = false; }
});
$$('.nav-item').forEach((button) => button.addEventListener('click', () => selectView(button.dataset.view)));
$$('[data-go]').forEach((button) => button.addEventListener('click', () => selectView(button.dataset.go)));
$('#namespace-select').addEventListener('change', (event) => loadPods(event.target.value));
$('#refresh-button').addEventListener('click', loadSummary);
$('#resource-load').addEventListener('click', async () => {
  const type = $('#resource-type').value, ns = $('#resource-namespace').value.trim();
  const body = $('#resource-rows'); body.innerHTML = '<tr><td colspan="4" class="empty-cell">正在读取…</td></tr>';
  try { const data = await api(`/api/resource?type=${encodeURIComponent(type)}&namespace=${encodeURIComponent(ns)}`); body.replaceChildren();
    for (const item of data.items || []) { const meta=item.metadata||{}, row=document.createElement('tr');
      const cells=[meta.name||'—',meta.namespace||'集群级',item.status?.readyReplicas !== undefined ? `${item.status.readyReplicas}/${item.spec?.replicas||0} Ready` : (item.status?.phase||item.spec?.type||item.kind||'')];
      cells.forEach(v=>{const td=document.createElement('td');td.textContent=v;row.append(td)});
      const action=document.createElement('td'), btn=document.createElement('button');btn.className='text-button';btn.textContent='查看 YAML';btn.onclick=async()=>{const d=await api(`/api/resource?type=${type}&namespace=${encodeURIComponent(meta.namespace||ns)}&name=${encodeURIComponent(meta.name)}`);$('#resource-yaml').value=d.yaml;$('#resource-yaml').dataset.name=meta.name;$('#resource-yaml').dataset.ns=meta.namespace||ns;$('#resource-yaml').dataset.type=type};action.append(btn);row.append(action);body.append(row);
    } if (!body.children.length) body.innerHTML='<tr><td colspan="4" class="empty-cell">没有资源</td></tr>';
  } catch(e) {body.innerHTML='';emptyRow(body,4,e.message)}
});
async function submitTask(url, payload) { const result=await api(url,{method:'POST',body:JSON.stringify(payload)}); const out=$('#task-status');out.textContent=`任务 ${result.task_id} 已提交`;
  const poll=async()=>{try{const state=await api(`/api/task?id=${encodeURIComponent(result.task_id)}`);out.textContent=`任务 ${state.id}: ${state.status}${state.error?` — ${state.error}`:''}`;if(state.status==='running')setTimeout(poll,1200)}catch(e){out.textContent=e.message}};setTimeout(poll,500);
}
$('#resource-patch').addEventListener('click',async()=>{try{const y=$('#resource-yaml'), obj=JSON.parse('{}'); await submitTask('/api/resource/patch',{type:y.dataset.type,name:y.dataset.name,namespace:y.dataset.ns,yaml:y.value})}catch(e){$('#task-status').textContent=e.message}});
$('#apply-yaml').addEventListener('click',async()=>{try{await submitTask('/api/resource/apply',{yaml:$('#publish-yaml').value})}catch(e){ $('#task-status').textContent=e.message }});
$('#metrics-load').addEventListener('click',async()=>{
  const box=$('#metrics-output');box.textContent='正在读取 metrics.k8s.io…';
  try { const kind=$('#metrics-kind').value, result=await api(`/api/metrics?kind=${kind}`); const table=document.createElement('table');
    table.innerHTML=kind==='nodes'?'<thead><tr><th>节点</th><th>CPU</th><th>内存</th><th>采样时间</th></tr></thead>':'<thead><tr><th>Namespace</th><th>Pod</th><th>容器 CPU</th><th>容器内存</th></tr></thead>';
    const body=document.createElement('tbody');
    for(const item of result.items||[]){const row=document.createElement('tr');const cells=kind==='nodes'?[item.metadata?.name,item.usage?.cpu,item.usage?.memory,item.timestamp]:[item.metadata?.namespace,item.metadata?.name,(item.containers||[]).map(c=>`${c.name}: ${c.usage?.cpu}`).join(' · '),(item.containers||[]).map(c=>`${c.name}: ${c.usage?.memory}`).join(' · ')];
      for(const value of cells){const cell=document.createElement('td');cell.textContent=value||'—';row.append(cell)}body.append(row)}
    if(!body.children.length){const row=document.createElement('tr'),cell=document.createElement('td');cell.colSpan=4;cell.className='empty-cell';cell.textContent='Metrics API 暂无数据';row.append(cell);body.append(row)}
    table.append(body);box.replaceChildren(table);
  } catch(e){box.textContent=`读取 Metrics API 失败：${e.message}`}
});
$('#connect-button').addEventListener('click', async (event) => {
  event.currentTarget.disabled = true; $('#connection-error').textContent = '正在读取 kubeconfig.yaml 并连接…';
  try { const state = await api('/api/connect', { method: 'POST', body: '{}' }); setConnection(true, state.context); await loadSummary(); }
  catch (error) { $('#connection-error').textContent = error.message; }
  finally { event.currentTarget.disabled = false; }
});
$('#logout-button').addEventListener('click', async () => { try { await api('/api/logout', { method: 'POST', body: '{}' }); } catch (_) {} setConnection(false); showLogin(''); });
async function initialize() {
  try { const status = await api('/api/status'); showApp(); setConnection(status.connected, status.context); if (status.error) $('#connection-error').textContent = status.error; if (status.connected) await loadSummary(); }
  catch (error) { if (!error.message.includes('重新登录')) showLogin(); }
}
initialize();
