const params = new URLSearchParams(window.location.search);
const projectId = params.get('id');
let project = null;
let isOwner = false;

async function init() {
  if (!projectId) { window.location.href = '/projects.html'; return; }
  let user;
  try { user = await API.get('/api/auth/me'); } catch { window.location.href = '/'; return; }
  document.getElementById('userName').textContent = user.name;
  document.getElementById('logoutBtn').onclick = async () => {
    await API.post('/api/auth/logout');
    window.location.href = '/';
  };

  try { project = await API.get(`/api/projects/${projectId}`); } catch { window.location.href = '/projects.html'; return; }
  isOwner = project.role === 'owner';
  document.getElementById('backLink').href = `/project.html?id=${projectId}`;
  document.getElementById('projName').textContent = project.name;
  document.getElementById('projMode').textContent = `Mode: ${project.mode}`;
  if (!isOwner) {
    document.getElementById('readonlyNotice').style.display = 'block';
    document.querySelectorAll('.owner-only').forEach(el => el.style.display = 'none');
    document.querySelectorAll('input, textarea').forEach(el => el.disabled = true);
  }

  document.getElementById('editName').value = project.name;
  document.getElementById('editGuidelines').value = project.guidelines || '';
  renderPreview();

  if (project.mode === 'pose') {
    document.getElementById('posePanel').classList.remove('hidden');
    document.getElementById('editKeypoints').value = project.keypoints.join(', ');
    document.getElementById('editSkeleton').value = project.skeleton.map(e => e.join('-')).join(', ');
  }

  renderClasses();
  bindEvents();
}

function bindEvents() {
  const okMsg = document.getElementById('saveOk');
  const flash = () => { okMsg.textContent = 'Saved.'; okMsg.classList.remove('hidden'); setTimeout(() => okMsg.classList.add('hidden'), 2000); };

  document.getElementById('saveNameBtn').onclick = async () => {
    try {
      await API.request('PATCH', `/api/projects/${projectId}`, { name: document.getElementById('editName').value.trim() });
      project.name = document.getElementById('editName').value.trim();
      document.getElementById('projName').textContent = project.name;
      flash();
    } catch (err) { alert(err.detail || 'Failed'); }
  };

  document.getElementById('saveGuidelinesBtn').onclick = async () => {
    const errEl = document.getElementById('guidelinesErr');
    hideErr(errEl);
    try {
      await API.request('PATCH', `/api/projects/${projectId}`, { guidelines: document.getElementById('editGuidelines').value });
      flash();
    } catch (err) { showErr(errEl, err.detail || 'Failed'); }
  };

  document.getElementById('editGuidelines').addEventListener('input', renderPreview);

  document.getElementById('addClassForm').onsubmit = async (e) => {
    e.preventDefault();
    const errEl = document.getElementById('classErr');
    hideErr(errEl);
    try {
      await API.post(`/api/projects/${projectId}/classes`, {
        name: document.getElementById('newClassName').value.trim(),
        description: document.getElementById('newClassDesc').value.trim(),
      });
      document.getElementById('newClassName').value = '';
      document.getElementById('newClassDesc').value = '';
      await reloadProject();
    } catch (err) { showErr(errEl, err.detail || 'Failed'); }
  };

  document.getElementById('savePoseBtn').onclick = async () => {
    const errEl = document.getElementById('poseErr');
    hideErr(errEl);
    const keypoints = document.getElementById('editKeypoints').value.split(',').map(s => s.trim()).filter(Boolean);
    let skeleton = [];
    const skRaw = document.getElementById('editSkeleton').value.trim();
    if (skRaw) {
      try {
        skeleton = skRaw.split(',').map(pair => {
          const [a, b] = pair.trim().split('-').map(Number);
          if (isNaN(a) || isNaN(b)) throw new Error(`bad edge: ${pair}`);
          return [a, b];
        });
      } catch (err) { showErr(errEl, err.message); return; }
    }
    try {
      await API.request('PATCH', `/api/projects/${projectId}`, { keypoints, skeleton });
      flash();
    } catch (err) { showErr(errEl, err.detail || 'Failed'); }
  };
}

function renderPreview() {
  const md = document.getElementById('editGuidelines').value;
  document.getElementById('guidelinesPreview').innerHTML = marked.parse(md || '');
}

function renderClasses() {
  const wrap = document.getElementById('classRows');
  wrap.innerHTML = project.classes.map(c => `
    <div class="row" data-cid="${c.id}">
      <span class="badge">${c.ord}</span>
      <input type="text" class="cls-name" value="${esc(c.name)}" style="width:180px" ${isOwner ? '' : 'disabled'}>
      <input type="text" class="cls-desc" value="${esc(c.description || '')}" placeholder="Description" style="flex:1" ${isOwner ? '' : 'disabled'}>
      ${isOwner ? `<button class="btn btn-ghost-dark btn-sm cls-save">Save</button>
      <button class="btn btn-ghost-dark btn-sm cls-del">Delete</button>` : ''}
    </div>
  `).join('');
  if (!isOwner) return;
  wrap.querySelectorAll('[data-cid]').forEach(row => {
    const cid = parseInt(row.dataset.cid);
    const errEl = document.getElementById('classErr');
    row.querySelector('.cls-save').onclick = async () => {
      hideErr(errEl);
      try {
        await API.request('PATCH', `/api/projects/${projectId}/classes/${cid}`, {
          name: row.querySelector('.cls-name').value.trim(),
          description: row.querySelector('.cls-desc').value.trim(),
        });
        await reloadProject();
      } catch (err) { showErr(errEl, err.detail || 'Failed'); }
    };
    row.querySelector('.cls-del').onclick = async () => {
      hideErr(errEl);
      if (!confirm('Delete this class?')) return;
      try {
        await API.del(`/api/projects/${projectId}/classes/${cid}`);
        await reloadProject();
      } catch (err) { showErr(errEl, err.detail || 'Failed'); }
    };
  });
}

async function reloadProject() {
  project = await API.get(`/api/projects/${projectId}`);
  renderClasses();
}

init();
