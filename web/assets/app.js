const $ = (id) => document.getElementById(id);
let token = sessionStorage.getItem('zhixu_token') || '';
let role = 'engineer';
let models = [];
let documents = [];
let noticeTimer;

function notify(message, error = false) {
  const box = $('notice');
  box.textContent = message;
  box.classList.toggle('error', error);
  box.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { box.hidden = true; }, 6500);
}

async function api(path, options = {}) {
  if (!token) { location.replace('/login'); throw new Error('请先登录'); }
  const response = await fetch(path, {
    ...options,
    headers: { Authorization: `Bearer ${token}`, ...options.headers },
    cache: 'no-store',
  });
  let body = null;
  try { body = await response.json(); } catch (_) { /* empty or non-JSON response */ }
  if (!response.ok) {
    if (response.status === 401) {
      sessionStorage.removeItem('zhixu_token');
      location.replace('/login');
    }
    const detail = body?.detail;
    const message = typeof detail === 'string' ? detail : Array.isArray(detail) ? '请检查填写内容' : `请求失败（${response.status}）`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return body;
}

async function busy(button, operation) {
  button.disabled = true;
  try { await operation(); } catch (error) { notify(error.message || '操作失败', true); }
  finally { button.disabled = false; }
}

function option(value, label) {
  const el = document.createElement('option');
  el.value = value;
  el.textContent = label;
  return el;
}

function renderModels() {
  for (const id of ['askModel', 'uploadModels']) {
    const select = $(id);
    const previous = id === 'askModel' ? select.value : '';
    select.replaceChildren();
    if (id === 'askModel') select.append(option('', models.length ? '请选择设备型号' : '暂无设备型号'));
    for (const model of models) select.append(option(model.id, `${model.model_code} · ${model.name}`));
    if (previous) select.value = previous;
  }
  $('modelCount').textContent = String(models.length);
}

function statusLabel(status) {
  return ({ processing: '入库中', review: '待审核', published: '已发布', superseded: '已替换', failed: '失败' })[status] || status;
}

function renderDocuments() {
  $('docCount').textContent = String(documents.length);
  $('reviewCount').textContent = String(documents.filter((item) => item.status === 'review').length);
  const list = $('documentList');
  list.replaceChildren();
  if (!documents.length) {
    const p = document.createElement('p'); p.className = 'empty'; p.textContent = '还没有文档。管理员可以在右侧上传第一份 PDF。'; list.append(p); return;
  }
  for (const doc of documents) {
    const row = document.createElement('div'); row.className = 'doc-row';
    const info = document.createElement('div');
    const title = document.createElement('strong'); title.textContent = doc.title;
    const meta = document.createElement('small'); meta.textContent = `#${doc.id} · ${doc.family_key} · v${doc.version} · ${doc.chunks} 个片段`;
    info.append(title, meta);
    const actions = document.createElement('div');
    const badge = document.createElement('span'); badge.className = `badge ${doc.status === 'review' ? 'warn' : doc.status === 'published' ? '' : 'muted'}`; badge.textContent = statusLabel(doc.status);
    actions.append(badge);
    if (doc.status === 'review' && role === 'admin') {
      const publish = document.createElement('button'); publish.type = 'button'; publish.textContent = '审核后发布 →';
      publish.addEventListener('click', () => busy(publish, async () => {
        await api(`/v1/documents/${doc.id}/publish`, { method: 'POST' });
        notify(`《${doc.title}》已发布`); await loadDocuments();
      }));
      actions.append(publish);
    }
    row.append(info, actions); list.append(row);
  }
}

async function loadDocuments() { documents = await api('/v1/documents'); renderDocuments(); }
async function loadWorkspace() {
  const identity = await api('/v1/auth/me');
  role = identity.role;
  $('orgName').textContent = identity.organization;
  $('currentOrg').textContent = identity.organization;
  $('currentUser').textContent = identity.username || '管理员密钥';
  $('currentRole').textContent = role === 'admin' ? '管理员' : '工程师';
  $('connectionBadge').textContent = `已连接 · ${role === 'admin' ? '管理员' : '工程师'}`;
  $('connectionBadge').classList.remove('muted');
  $('userManager').hidden = role !== 'admin';
  $('adminActions').hidden = role !== 'admin';
  [models, documents] = await Promise.all([api('/v1/device-models'), api('/v1/documents')]);
  renderModels(); renderDocuments();
  if (role === 'admin') await loadUsers();
}

async function loadUsers() {
  const users = await api('/v1/auth/users');
  const list = $('userList'); list.replaceChildren();
  for (const user of users) {
    const row = document.createElement('div'); row.className = 'member-row';
    const name = document.createElement('strong'); name.textContent = user.username;
    const userRole = document.createElement('span'); userRole.textContent = user.role === 'admin' ? '管理员' : '工程师';
    row.append(name, userRole); list.append(row);
  }
}
$('userForm').addEventListener('submit', (event) => { event.preventDefault(); busy(event.submitter, async () => {
  await api('/v1/auth/users', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: $('newUsername').value.trim(), password: $('newPassword').value, role: $('newRole').value }) });
  event.target.reset(); await loadUsers(); notify('成员账号已创建');
}); });
$('disconnect').addEventListener('click', async () => {
  try { await api('/v1/auth/logout', { method: 'POST' }); } catch (_) { /* Clear browser session even if offline. */ }
  sessionStorage.removeItem('zhixu_token');
  sessionStorage.removeItem('zhixu_role');
  sessionStorage.removeItem('zhixu_org');
  location.replace('/login');
});
$('modelForm').addEventListener('submit', (event) => { event.preventDefault(); busy(event.submitter, async () => {
  await api('/v1/device-models', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model_code: $('modelCode').value.trim(), name: $('modelName').value.trim() }) });
  models = await api('/v1/device-models'); renderModels(); event.target.reset(); notify('设备型号已添加');
}); });
$('refreshDocs').addEventListener('click', (event) => busy(event.currentTarget, async () => { await loadDocuments(); notify('文档列表已更新'); }));
$('uploadForm').addEventListener('submit', (event) => { event.preventDefault(); busy(event.submitter, async () => {
  const file = $('pdfFile').files[0];
  if (!file || file.size > 50 * 1024 * 1024) throw new Error('请选择不超过 50 MB 的 PDF');
  const ids = [...$('uploadModels').selectedOptions].map((item) => item.value);
  if (!ids.length) throw new Error('请至少选择一个设备型号');
  const params = new URLSearchParams({ family_key: $('familyKey').value.trim(), version: $('version').value.trim(), title: $('uploadTitle').value.trim() });
  ids.forEach((id) => params.append('device_model_ids', id));
  const result = await api(`/v1/documents?${params}`, { method: 'POST', headers: { 'Content-Type': 'application/pdf' }, body: file });
  $('jobStatus').textContent = `文档 #${result.document_id} 已上传，任务 #${result.job_id} 排队中。`;
  event.target.reset(); await loadDocuments(); notify('上传成功，正在等待入库');
  if (role === 'admin') pollJob(result.job_id);
}); });
async function pollJob(id) {
  for (let attempt = 0; attempt < 40; attempt++) {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    if (!token) return;
    try {
      const job = await api(`/v1/ingestion-jobs/${id}`);
      $('jobStatus').textContent = `任务 #${id}：${({ queued: '排队中', running: '处理中', succeeded: '已完成，等待审核', failed: '失败' })[job.status] || job.status}${job.error_message ? ` · ${job.error_message}` : ''}`;
      if (job.status === 'succeeded' || job.status === 'failed') { await loadDocuments(); return; }
    } catch (error) { $('jobStatus').textContent = `任务状态查询失败：${error.message}`; return; }
  }
  $('jobStatus').textContent = `任务 #${id} 仍在处理，请稍后刷新文档列表。`;
}
$('askForm').addEventListener('submit', (event) => { event.preventDefault(); busy(event.submitter, async () => {
  const result = await api('/v1/answers', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ device_model_id: Number($('askModel').value), fault_code: $('faultCode').value.trim() || null, question: $('question').value.trim() }) });
  $('answerBox').hidden = false;
  $('answerStatus').textContent = ({ answered: '已回答', needs_review: '需要复核', insufficient_evidence: '资料不足' })[result.status] || result.status;
  $('answerStatus').className = `badge ${result.status === 'answered' ? '' : 'warn'}`;
  $('answerText').textContent = result.answer;
  const references = $('references'); references.replaceChildren();
  for (const ref of result.references || []) {
    const card = document.createElement('div'); card.className = 'reference';
    const heading = document.createElement('strong'); heading.textContent = `${ref.label} · ${ref.title} · v${ref.version} · 第 ${ref.page} 页`;
    const content = document.createElement('p'); content.textContent = ref.content;
    card.append(heading, content); references.append(card);
  }
  $('answerBox').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}); });

sessionStorage.removeItem('zhixu_api_key');
if (!token) location.replace('/login');
else loadWorkspace().catch((error) => { notify(error.message, true); });
