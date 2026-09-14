/* Project page: browse/claim/annotate entry, dataset management for owners.
   Image upload and video import live on the separate /upload.html page. */
const params = new URLSearchParams(window.location.search);
const projectId = params.get('id');
let currentUser = null;
let project = null;
let allImages = [];
let currentFilter = 'all';
let currentTab = 'browse';
let isMember = false;
let manageOn = false;
const selected = new Set();

async function init() {
  if (!projectId) { window.location.href = appPath('/projects.html'); return; }
  try { currentUser = await API.get('/api/auth/me'); } catch { window.location.href = appPath('/'); return; }
  document.getElementById('userName').textContent = currentUser.name;
  document.getElementById('logoutBtn').onclick = async () => {
    await API.post('/api/auth/logout');
    window.location.href = appPath('/');
  };

  try { project = await API.get(`/api/projects/${projectId}`); } catch { window.location.href = appPath('/projects.html'); return; }
  isMember = project.role !== null;
  document.getElementById('projName').textContent = project.name;
  updateProjectHeader();
  document.getElementById('uploadBtn').href = appPath(`/upload.html?project=${projectId}`);
  document.getElementById('exportBtn').href = appPath(`/api/projects/${projectId}/export`);
  document.getElementById('settingsBtn').href = appPath(`/project_settings.html?id=${projectId}`);
  if (project.guidelines) {
    document.getElementById('guidelinesPanel').classList.remove('hidden');
    document.getElementById('guidelinesView').innerHTML = marked.parse(project.guidelines);
  }

  if (!isMember) {
    // Guest view: read-only until they join.
    document.getElementById('joinPanel').classList.remove('hidden');
    document.getElementById('joinBtn').onclick = async () => {
      const errEl = document.getElementById('joinErr');
      hideErr(errEl);
      try {
        await API.post(`/api/projects/${projectId}/join`);
        window.location.reload();
      } catch (err) { showErr(errEl, err.detail || t('common.joinFailed')); }
    };
    document.getElementById('uploadBtn').classList.add('hidden');
    document.getElementById('claimPanel').classList.add('hidden');
    document.getElementById('tabMine').classList.add('hidden');
    document.getElementById('settingsBtn').classList.add('hidden');
    document.getElementById('exportBtn').classList.add('hidden');
  }

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
      } catch (err) { showErr(errEl, err.detail || t('common.failed')); }
    };
  }
  loadMembers();

  // Tabs
  document.getElementById('tabBrowse').onclick = () => setTab('browse');
  document.getElementById('tabMine').onclick = () => setTab('mine');
  document.getElementById('tabStats').onclick = () => setTab('stats');

  // Filters
  document.getElementById('filterAll').onclick = () => { currentFilter = 'all'; renderImages(); };
  document.getElementById('filterUnlabeled').onclick = () => { currentFilter = 'unlabeled'; renderImages(); };

  // Batch claim
  const claimCount = document.getElementById('claimCount');
  claimCount.addEventListener('wheel', (e) => {
    e.preventDefault();
    const step = e.deltaY < 0 ? 1 : -1;
    const v = (parseInt(claimCount.value) || 1) + step;
    claimCount.value = Math.max(1, Math.min(500, v));
  }, { passive: false });
  document.getElementById('claimBtn').onclick = claimBatch;

  // Dataset management (owner only, browse tab)
  if (project.role === 'owner') {
    const manageBtn = document.getElementById('manageBtn');
    manageBtn.classList.remove('hidden');
    manageBtn.onclick = () => enterManage();
    document.getElementById('manageExit').onclick = () => exitManage();
    document.getElementById('manageSelectAll').onclick = () => {
      visibleImages().forEach(i => selected.add(i.id));
      renderImages();
    };
    document.getElementById('manageClear').onclick = () => { selected.clear(); renderImages(); };
    document.getElementById('manageDelete').onclick = deleteSelected;
  }

  // Card clicks: navigate normally, or toggle selection while managing.
  document.getElementById('imageGrid').addEventListener('click', (e) => {
    const card = e.target.closest('.thumb-card');
    if (!card) return;
    if (manageOn) { toggleSelect(parseInt(card.dataset.id)); return; }
    openImage(parseInt(card.dataset.id));
  });

  loadImages();
}

function setTab(tab) {
  currentTab = tab;
  document.getElementById('tabBrowse').classList.toggle('tab-active', tab === 'browse');
  document.getElementById('tabMine').classList.toggle('tab-active', tab === 'mine');
  document.getElementById('tabStats').classList.toggle('tab-active', tab === 'stats');
  document.getElementById('claimPanel').classList.toggle('hidden', tab !== 'browse' || !isMember);
  document.getElementById('browseControls').classList.toggle('hidden', tab !== 'browse');
  document.getElementById('imageGrid').classList.toggle('hidden', tab === 'stats');
  document.getElementById('statsPanel').classList.toggle('hidden', tab !== 'stats');
  if (tab === 'stats') loadStats();
  if (tab !== 'browse' && manageOn) exitManage();
  renderImages();
}

function enterManage() {
  manageOn = true;
  selected.clear();
  document.getElementById('manageBar').classList.remove('hidden');
  document.getElementById('filterUnlabeled').disabled = false;
  renderImages();
}

function exitManage() {
  manageOn = false;
  selected.clear();
  document.getElementById('manageBar').classList.add('hidden');
  renderImages();
}

function toggleSelect(id) {
  if (selected.has(id)) selected.delete(id);
  else selected.add(id);
  renderImages();
}

async function deleteSelected() {
  if (!selected.size) return;
  if (!confirm(t('project.deleteConfirm', { count: selected.size }))) return;
  const errEl = document.getElementById('manageErr');
  hideErr(errEl);
  try {
    await API.post(`/api/projects/${projectId}/images/delete-batch`, { ids: [...selected] });
    selected.clear();
    await loadImages();
  } catch (err) { showErr(errEl, err.detail || t('common.deleteFailed')); }
}

async function loadMembers() {
  try {
    const members = await API.get(`/api/projects/${projectId}/members`);
    window.__projectMembers = members;
    renderMembers(members);
  } catch {}
}

function renderMembers(members = window.__projectMembers || []) {
  document.getElementById('memberList').innerHTML = members.map(m =>
    `<div class="row-between"><span>${esc(m.name)} <span class="text-mute" style="font-size:12px">${esc(m.email)}</span></span><span class="badge">${esc(t(`role.${m.role}`))}</span></div>`
  ).join('');
}

function updateProjectHeader() {
  if (!project) return;
  const role = project.role || 'guest';
  document.getElementById('projRole').textContent = t('project.roleMode', { role: t(`role.${role}`), mode: t(`mode.${project.mode}`) });
  document.getElementById('projMeta').textContent = t('project.meta', {
    classes: project.classes.map(c => c.name).join(' · ') || t('projects.noClasses'),
    labeled: project.labeled_count || 0,
    total: project.image_count || 0,
  });
}

function isClaimed(img) { return img.claimed_by && !img.claim_expired; }
function isMine(img) { return img.claimed_by === currentUser.id && !img.claim_expired; }
function isAvailable(img) { return img.status === 'unlabeled' && !isClaimed(img); }

function visibleImages() {
  if (currentTab === 'mine') return allImages.filter(isMine);
  if (currentFilter === 'unlabeled') return allImages.filter(i => i.status === 'unlabeled');
  return allImages;
}

async function loadImages() {
  try {
    allImages = await API.get(`/api/projects/${projectId}/images`);
    renderImages();
    // update header count
    const labeled = allImages.filter(i => i.status === 'labeled').length;
    document.getElementById('projMeta').textContent = t('project.meta', {
      classes: project.classes.map(c => c.name).join(' · ') || t('projects.noClasses'), labeled, total: allImages.length,
    });
    const avail = allImages.filter(isAvailable).length;
    document.getElementById('claimAvail').textContent = t('project.available', { count: avail });
  } catch {}
}

function renderImages() {
  const grid = document.getElementById('imageGrid');
  const empty = document.getElementById('emptyImages');
  const filtered = visibleImages();
  empty.textContent = currentTab === 'mine' ? t('project.noClaims') : t('project.noImages');
  empty.classList.toggle('hidden', filtered.length > 0 || currentTab === 'stats');
  if (manageOn) {
    document.getElementById('manageCount').textContent = t('project.selected', { count: selected.size });
  }
  grid.innerHTML = filtered.map(img => {
    const claimed = isClaimed(img);
    const mine = isMine(img);
    const badgeClass = img.status === 'labeled' ? 'badge-labeled' : (claimed ? 'badge-claimed' : 'badge-unlabeled');
    const badgeText = img.status === 'labeled' ? t('project.labeledStatus') : (claimed ? (mine ? t('project.mineStatus') : t('project.claimedStatus')) : t('project.unlabeledStatus'));
    const releaseBtn = currentTab === 'mine'
      ? `<button class="btn btn-ghost-dark btn-sm thumb-release" onclick="releaseImage(${img.id}, event)">${t('project.release')}</button>`
      : '';
    const selectedClass = manageOn && selected.has(img.id) ? ' thumb-selected' : '';
    const check = manageOn ? `<span class="thumb-check">${selected.has(img.id) ? '✓' : ''}</span>` : '';
    return `
      <div class="thumb-card${manageOn ? ' thumb-selectable' : ''}${selectedClass}" data-id="${img.id}">
        ${check}
        <img src="${appPath(img.url)}" alt="${esc(img.filename)}" loading="lazy">
        <div class="thumb-info">
          <div class="row-between">
            <span style="font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(img.filename)}</span>
            <span class="badge ${badgeClass}">${badgeText}</span>
          </div>
          <p class="text-mute" style="font-size:12px;margin-top:4px;">${t('project.boxes', { count: img.annotation_count })}${claimed && img.claimed_by_name ? ' · ' + esc(img.claimed_by_name) : ''}</p>
          ${releaseBtn}
        </div>
      </div>`;
  }).join('');
}

async function claimBatch() {
  const errEl = document.getElementById('claimErr');
  const okEl = document.getElementById('claimOk');
  hideErr(errEl); okEl.classList.add('hidden');
  const count = parseInt(document.getElementById('claimCount').value);
  if (!count || count < 1) { showErr(errEl, t('project.enterCount')); return; }
  try {
    const r = await API.post(`/api/projects/${projectId}/images/claim`, { count });
    okEl.textContent = t('project.claimedCount', { count: r.count });
    okEl.classList.remove('hidden');
    await loadImages();
    if (r.count > 0) setTab('mine');
  } catch (err) { showErr(errEl, err.detail || t('common.claimFailed')); }
}

async function releaseImage(imageId, e) {
  if (e) e.stopPropagation();
  try {
    await API.post(`/api/projects/${projectId}/images/${imageId}/release`);
    await loadImages();
  } catch {}
}

async function loadStats() {
  const panel = document.getElementById('statsPanel');
  try {
    const stats = await API.get(`/api/projects/${projectId}/stats`);
    panel.innerHTML = `
      <div class="panel">
        <h2 class="micro-cap mb-2">${t('project.memberProgress')}</h2>
        <table class="stats-table">
          <tr class="text-mute"><th>${t('project.nameColumn')}</th><th>${t('project.roleColumn')}</th><th>${t('project.labeledColumn')}</th><th>${t('project.claimingColumn')}</th></tr>
          ${stats.map(s => `<tr><td>${esc(s.name)}</td><td>${esc(t(`role.${s.role}`))}</td><td>${s.labeled_count}</td><td>${s.claimed_count}</td></tr>`).join('')}
        </table>
      </div>`;
  } catch {}
}

function openImage(imageId) {
  // View-only by default; the annotate page enables editing when claimed by you.
  window.location.href = appPath(`/annotate.html?project=${projectId}&image=${imageId}`);
}

window.addEventListener('languagechange', () => {
  if (!project) return;
  updateProjectHeader();
  renderMembers();
  renderImages();
  if (currentTab === 'stats') loadStats();
});

init();
