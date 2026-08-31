async function init() {
  let user;
  try { user = await API.get('/api/auth/me'); } catch { window.location.href = '/'; return; }
  document.getElementById('userName').textContent = user.name;

  document.getElementById('logoutBtn').onclick = async () => {
    await API.post('/api/auth/logout');
    window.location.href = '/';
  };

  const createPanel = document.getElementById('createPanel');
  const createErr = document.getElementById('createErr');
  document.getElementById('newProjectBtn').onclick = () => createPanel.classList.toggle('hidden');
  document.getElementById('createForm').onsubmit = async (e) => {
    e.preventDefault();
    hideErr(createErr);
    const name = document.getElementById('projName').value.trim();
    const classes = document.getElementById('projClasses').value.split(',').map(s => s.trim()).filter(Boolean);
    try {
      await API.post('/api/projects', { name, classes });
      document.getElementById('projName').value = '';
      document.getElementById('projClasses').value = '';
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
        <p class="text-mute mt-2">${p.classes.map(c => esc(c.name)).join(' · ') || 'No classes'}</p>
        <p class="text-mute mt-2" style="font-size:13px;">${p.labeled_count}/${p.image_count} labeled</p>
      </div>
    `).join('');
  } catch { list.innerHTML = '<p class="error">Failed to load projects.</p>'; }
}

init();
