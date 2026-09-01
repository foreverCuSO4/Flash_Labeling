const params = new URLSearchParams(window.location.search);
const projectId = params.get('id');
let currentUser = null;
let project = null;
let allImages = [];
let currentFilter = 'all';

async function init() {
  if (!projectId) { window.location.href = '/projects.html'; return; }
  try { currentUser = await API.get('/api/auth/me'); } catch { window.location.href = '/'; return; }
  document.getElementById('userName').textContent = currentUser.name;
  document.getElementById('logoutBtn').onclick = async () => {
    await API.post('/api/auth/logout');
    window.location.href = '/';
  };

  try { project = await API.get(`/api/projects/${projectId}`); } catch { window.location.href = '/projects.html'; return; }
  document.getElementById('projName').textContent = project.name;
  document.getElementById('projRole').textContent = `${project.role} · ${project.mode}`;
  document.getElementById('projMeta').textContent =
    `${project.classes.map(c => c.name).join(' · ') || 'No classes'} — ${project.labeled_count}/${project.image_count} labeled`;
  document.getElementById('exportBtn').href = `/api/projects/${projectId}/export`;
  document.getElementById('settingsBtn').href = `/project_settings.html?id=${projectId}`;
  if (project.guidelines) {
    document.getElementById('guidelinesPanel').classList.remove('hidden');
    document.getElementById('guidelinesView').innerHTML = marked.parse(project.guidelines);
  }

  // Upload
  const uploadErr = document.getElementById('uploadErr');
  const uploadOk = document.getElementById('uploadOk');
  document.getElementById('uploadForm').onsubmit = async (e) => {
    e.preventDefault();
    hideErr(uploadErr); uploadOk.classList.add('hidden');
    const files = document.getElementById('fileInput').files;
    if (!files.length) return;
    const fd = new FormData();
    for (const f of files) fd.append('files', f);
    try {
      const uploaded = await API.post(`/api/projects/${projectId}/images/upload`, fd, true);
      uploadOk.textContent = `${uploaded.length} image(s) uploaded.`;
      uploadOk.classList.remove('hidden');
      document.getElementById('fileInput').value = '';
      loadImages();
    } catch (err) { showErr(uploadErr, err.detail || 'Upload failed'); }
  };

  // Members
  if (project.role === 'owner') {
    document.getElementById('addMemberForm').classList.remove('hidden');
    document.getElementById('addMemberForm').onsubmit = async (e) => {
      e.preventDefault();
      const errEl = document.getElementById('memberErr');
      hideErr(errEl);
      try {
        await API.post(`/api/projects/${projectId}/members`, { email: document.getElementById('memberEmail').value.trim() });
        document.getElementById('memberEmail').value = '';
        loadMembers();
      } catch (err) { showErr(errEl, err.detail || 'Failed'); }
    };
  }
  loadMembers();

  // Filters
  document.getElementById('filterAll').onclick = () => { currentFilter = 'all'; renderImages(); };
  document.getElementById('filterUnlabeled').onclick = () => { currentFilter = 'unlabeled'; renderImages(); };
  document.getElementById('filterMine').onclick = () => { currentFilter = 'mine'; renderImages(); };

  loadImages();
}

async function loadMembers() {
  try {
    const members = await API.get(`/api/projects/${projectId}/members`);
    document.getElementById('memberList').innerHTML = members.map(m =>
      `<div class="row-between"><span>${esc(m.name)} <span class="text-mute" style="font-size:12px">${esc(m.email)}</span></span><span class="badge">${m.role}</span></div>`
    ).join('');
  } catch {}
}

async function loadImages() {
  try {
    allImages = await API.get(`/api/projects/${projectId}/images`);
    renderImages();
    // update header count
    const labeled = allImages.filter(i => i.status === 'labeled').length;
    document.getElementById('projMeta').textContent =
      `${project.classes.map(c => c.name).join(' · ') || 'No classes'} — ${labeled}/${allImages.length} labeled`;
  } catch {}
}

function renderImages() {
  const grid = document.getElementById('imageGrid');
  const empty = document.getElementById('emptyImages');
  let filtered = allImages;
  if (currentFilter === 'unlabeled') filtered = allImages.filter(i => i.status === 'unlabeled');
  if (currentFilter === 'mine') filtered = allImages.filter(i => i.claimed_by === currentUser.id);
  empty.classList.toggle('hidden', filtered.length > 0);
  grid.innerHTML = filtered.map(img => {
    const claimed = img.claimed_by && !img.claim_expired;
    const mine = img.claimed_by === currentUser.id;
    const badgeClass = img.status === 'labeled' ? 'badge-labeled' : (claimed ? 'badge-claimed' : 'badge-unlabeled');
    const badgeText = img.status === 'labeled' ? 'Labeled' : (claimed ? (mine ? 'Mine' : `Claimed`) : 'Unlabeled');
    const canOpen = !claimed || mine;
    return `
      <div class="thumb-card" ${canOpen ? `onclick="openImage(${img.id})"` : ''} style="${canOpen ? '' : 'opacity:0.5;cursor:not-allowed'}">
        <img src="${img.url}" alt="${esc(img.filename)}" loading="lazy">
        <div class="thumb-info">
          <div class="row-between">
            <span style="font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(img.filename)}</span>
            <span class="badge ${badgeClass}">${badgeText}</span>
          </div>
          <p class="text-mute" style="font-size:12px;margin-top:4px;">${img.annotation_count} box(es)${img.claimed_by_name ? ' · ' + esc(img.claimed_by_name) : ''}</p>
        </div>
      </div>`;
  }).join('');
}

async function openImage(imageId) {
  // Try to claim, then open annotate page regardless (annotate page handles read-only)
  try { await API.post(`/api/projects/${projectId}/images/${imageId}/claim`); } catch {}
  window.location.href = `/annotate.html?project=${projectId}&image=${imageId}`;
}

init();
