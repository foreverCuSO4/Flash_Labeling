async function init() {
  let user;
  try { user = await API.get('/api/auth/me'); } catch { window.location.href = appPath('/'); return; }
  document.getElementById('userName').textContent = user.name;

  const avatarImg = document.getElementById('userAvatar');
  const avatarInput = document.getElementById('avatarInput');
  avatarImg.src = appPath(`/api/users/${user.id}/avatar`);
  document.getElementById('avatarBtn').onclick = () => avatarInput.click();
  avatarInput.onchange = async () => {
    if (!avatarInput.files.length) return;
    const fd = new FormData();
    fd.append('file', avatarInput.files[0]);
    try {
      await API.post('/api/users/me/avatar', fd, true);
      avatarImg.src = appPath(`/api/users/${user.id}/avatar?v=${Date.now()}`);
    } catch (err) { alert(err.detail || t('projects.avatarUploadFailed')); }
    avatarInput.value = '';
  };

  document.getElementById('logoutBtn').onclick = async () => {
    await API.post('/api/auth/logout');
    window.location.href = appPath('/');
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
    } catch (err) { showErr(createErr, err.detail || t('common.failed')); }
  };

  // Create from an uploaded dataset.yaml file
  const yamlErr = document.getElementById('yamlErr');
  document.getElementById('yamlForm').onsubmit = async (e) => {
    e.preventDefault();
    hideErr(yamlErr);
    const file = document.getElementById('yamlFile').files[0];
    if (!file) {
      showErr(yamlErr, t('projects.chooseYaml'));
      return;
    }
    if (!file.size) {
      showErr(yamlErr, t('projects.yamlEmpty'));
      return;
    }
    const name = document.getElementById('yamlName').value.trim();
    let proj;
    try {
      let content;
      try {
        content = await file.text();
      } catch (readErr) {
        showErr(yamlErr, t('projects.yamlRead', { name: file.name, detail: readErr.message || 'Browser could not read the file' }));
        return;
      }
      if (!content.trim()) {
        showErr(yamlErr, t('projects.yamlNoContent'));
        return;
      }
      proj = await API.post('/api/projects/from-yaml-text', {
        filename: file.name || 'dataset.yaml', content, name,
      });
    } catch (err) {
      showErr(yamlErr, t('projects.yamlCreateDetail', { detail: err.detail || t('common.createProjectFailed'), name: file.name || 'unnamed', size: file.size }));
      return;
    }
    document.getElementById('yamlFile').value = '';
    document.getElementById('yamlName').value = '';
    createPanel.classList.add('hidden');
    loadProjects();
    window.location.href = appPath(`/project.html?id=${proj.id}`);
  };

  loadProjects();
}

async function loadProjects() {
  const list = document.getElementById('projectList');
  const emptyMsg = document.getElementById('emptyMsg');
  try {
    const projects = await API.get('/api/projects');
    window.__projectsData = projects;
    emptyMsg.classList.toggle('hidden', projects.length > 0);
    renderProjects(projects);
  } catch { list.innerHTML = `<p class="error">${t('common.loadProjectsFailed')}</p>`; }
}

function renderProjects(projects = window.__projectsData || []) {
  const list = document.getElementById('projectList');
  list.innerHTML = projects.map(p => `
      <div class="panel" style="cursor:pointer" onclick="window.location.href=appPath('/project.html?id=${p.id}')">
        <p class="micro-cap">${esc(p.role ? t(`role.${p.role}`) : t('projects.viewRole'))}</p>
        <h3 style="font-family:var(--font-display);font-size:24px;text-transform:uppercase;letter-spacing:0.96px;">${esc(p.name)}</h3>
        <p class="text-mute mt-2"><span class="badge">${esc(t(`mode.${p.mode}`))}</span> ${p.classes.map(c => esc(c.name)).join(' · ') || t('projects.noClasses')}</p>
        <p class="text-mute mt-2" style="font-size:13px;">${t('projects.labeled', { count: `${p.labeled_count}/${p.image_count}` })}</p>
      </div>
    `).join('');
}

window.addEventListener('languagechange', () => renderProjects());

init();
