async function init() {
  let user;
  try { user = await API.get('/api/auth/me'); } catch { window.location.href = '/'; return; }
  document.getElementById('userName').textContent = user.name;

  const avatarImg = document.getElementById('userAvatar');
  const avatarInput = document.getElementById('avatarInput');
  avatarImg.src = `/api/users/${user.id}/avatar`;
  document.getElementById('avatarBtn').onclick = () => avatarInput.click();
  avatarInput.onchange = async () => {
    if (!avatarInput.files.length) return;
    const fd = new FormData();
    fd.append('file', avatarInput.files[0]);
    try {
      await API.post('/api/users/me/avatar', fd, true);
      avatarImg.src = `/api/users/${user.id}/avatar?v=${Date.now()}`;
    } catch (err) { alert(err.detail || 'Avatar upload failed'); }
    avatarInput.value = '';
  };

  document.getElementById('logoutBtn').onclick = async () => {
    await API.post('/api/auth/logout');
    window.location.href = '/';
  };

  const createPanel = document.getElementById('createPanel');
  const createErr = document.getElementById('createErr');
  document.getElementById('newProjectBtn').onclick = () => createPanel.classList.toggle('hidden');
  document.querySelectorAll('input[name="mode"]').forEach(r => {
    r.onchange = () => document.getElementById('poseFields').classList.toggle('hidden', r.value !== 'pose' || !r.checked);
  });
  document.getElementById('createForm').onsubmit = async (e) => {
    e.preventDefault();
    hideErr(createErr);
    const name = document.getElementById('projName').value.trim();
    const classes = document.getElementById('projClasses').value.split(',').map(s => s.trim()).filter(Boolean);
    const mode = document.querySelector('input[name="mode"]:checked').value;
    const body = { name, classes, mode };
    if (mode === 'pose') {
      body.keypoints = document.getElementById('projKeypoints').value.split(',').map(s => s.trim()).filter(Boolean);
      const skRaw = document.getElementById('projSkeleton').value.trim();
      body.skeleton = [];
      if (skRaw) {
        try {
          body.skeleton = skRaw.split(',').map(pair => {
            const [a, b] = pair.trim().split('-').map(Number);
            if (isNaN(a) || isNaN(b)) throw new Error(`bad edge: ${pair}`);
            return [a, b];
          });
        } catch (err) { showErr(createErr, err.message); return; }
      }
    }
    try {
      await API.post('/api/projects', body);
      document.getElementById('projName').value = '';
      document.getElementById('projClasses').value = '';
      document.getElementById('projKeypoints').value = '';
      document.getElementById('projSkeleton').value = '';
      createPanel.classList.add('hidden');
      loadProjects();
    } catch (err) { showErr(createErr, err.detail || 'Failed'); }
  };

  loadProjects();
}

async function loadProjects() {
  const list = document.getElementById('projectList');
  const emptyMsg = document.getElementById('emptyMsg');
  try {
    const projects = await API.get('/api/projects');
    emptyMsg.classList.toggle('hidden', projects.length > 0);
    list.innerHTML = projects.map(p => `
      <div class="panel" style="cursor:pointer" onclick="window.location.href='/project.html?id=${p.id}'">
        <p class="micro-cap">${esc(p.role)}</p>
        <h3 style="font-family:var(--font-display);font-size:24px;text-transform:uppercase;letter-spacing:0.96px;">${esc(p.name)}</h3>
        <p class="text-mute mt-2"><span class="badge">${p.mode}</span> ${p.classes.map(c => esc(c.name)).join(' · ') || 'No classes'}</p>
        <p class="text-mute mt-2" style="font-size:13px;">${p.labeled_count}/${p.image_count} labeled</p>
      </div>
    `).join('');
  } catch { list.innerHTML = '<p class="error">Failed to load projects.</p>'; }
}

init();
