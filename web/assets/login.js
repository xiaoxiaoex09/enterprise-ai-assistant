const $ = (id) => document.getElementById(id);
const errorBox = $('loginError');

function showError(message) { errorBox.textContent = message; errorBox.hidden = false; }
async function request(path, body, key) {
  const response = await fetch(path, {
    method: 'POST', cache: 'no-store',
    headers: { 'Content-Type': 'application/json', ...(key ? { 'X-API-Key': key } : {}) },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `请求失败（${response.status}）`);
  return data;
}
async function login(organization, username, password) {
  const result = await request('/v1/auth/login', { organization, username, password });
  sessionStorage.setItem('zhixu_token', result.token);
  sessionStorage.setItem('zhixu_org', result.organization);
  sessionStorage.setItem('zhixu_role', result.role);
  location.replace('/app');
}
$('loginForm').addEventListener('submit', async (event) => {
  event.preventDefault(); errorBox.hidden = true;
  const button = event.submitter; button.disabled = true;
  try { await login($('organization').value.trim(), $('username').value.trim(), $('password').value); }
  catch (error) { showError(error.message); button.disabled = false; }
});
$('showSetup').addEventListener('click', () => { $('setupPanel').hidden = !$('setupPanel').hidden; });
$('setupForm').addEventListener('submit', async (event) => {
  event.preventDefault(); errorBox.hidden = true;
  const button = event.submitter; button.disabled = true;
  const organization = $('setupOrg').value.trim();
  const username = $('setupUser').value.trim();
  const password = $('setupPassword').value;
  try {
    await request('/v1/auth/bootstrap', { organization, username, password }, $('setupKey').value.trim());
    $('setupKey').value = '';
    await login(organization, username, password);
  } catch (error) { showError(error.message); button.disabled = false; }
});
if (sessionStorage.getItem('zhixu_token')) {
  fetch('/v1/auth/me', { headers: { Authorization: `Bearer ${sessionStorage.getItem('zhixu_token')}` }, cache: 'no-store' })
    .then((response) => { if (response.ok) location.replace('/app'); else sessionStorage.removeItem('zhixu_token'); });
}
