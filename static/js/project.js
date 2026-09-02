const params = new URLSearchParams(window.location.search);
const projectId = params.get('id');
let currentUser = null;
let project = null;
let allImages = [];
let currentFilter = 'all';
let currentTab = 'browse';
let isMember = false;
let videoJobs = null;
let videoPollTimer = null;

async function init() {
  if (!projectId) { window.location.href = '/projects.html'; return; }
  try { currentUser = await API.get('/api/auth/me'); } catch { window.location.href = '/'; return; }
  document.getElementById('userName').textContent = currentUser.name;
  document.getElementById('logoutBtn').onclick = async () => {
    await API.post('/api/auth/logout');
    window.location.href = '/';
  };

  try { project = await API.get(`/api/projects/${projectId}`); } catch { window.location.href = '/projects.html'; return; }
  isMember = project.role !== null;
  document.getElementById('projName').textContent = project.name;
  document.getElementById('projRole').textContent = `${project.role || 'guest'} · ${project.mode}`;
  document.getElementById('projMeta').textContent =
    `${project.classes.map(c => c.name).join(' · ') || 'No classes'} — ${project.labeled_count}/${project.image_count} labeled`;
  document.getElementById('exportBtn').href = `/api/projects/${projectId}/export`;
  document.getElementById('settingsBtn').href = `/project_settings.html?id=${projectId}`;
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
      } catch (err) { showErr(errEl, err.detail || 'Join failed'); }
    };
    document.getElementById('uploadPanel').classList.add('hidden');
    document.getElementById('videoPanel').classList.add('hidden');
    document.getElementById('claimPanel').classList.add('hidden');
    document.getElementById('tabMine').classList.add('hidden');
    document.getElementById('settingsBtn').classList.add('hidden');
    document.getElementById('exportBtn').classList.add('hidden');
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

  // Video import
  const videoErr = document.getElementById('videoErr');
  const videoOk = document.getElementById('videoOk');
  document.getElementById('videoForm').onsubmit = async (e) => {
    e.preventDefault();
    hideErr(videoErr); videoOk.classList.add('hidden');
    const files = document.getElementById('videoInput').files;
    if (!files.length) return;
    const fd = new FormData();
    for (const f of files) fd.append('files', f);
    fd.append('params', JSON.stringify(videoParams()));
    try {
      const jobs = await API.post(`/api/projects/${projectId}/videos/upload`, fd, true);
      videoOk.textContent = `${jobs.length} video(s) queued for extraction.`;
      videoOk.classList.remove('hidden');
      document.getElementById('videoInput').value = '';
      loadVideoJobs();
    } catch (err) { showErr(videoErr, err.detail || 'Upload failed'); }
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

  loadImages();
  loadVideoJobs();
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
  renderImages();
}

async function loadMembers() {
  try {
    const members = await API.get(`/api/projects/${projectId}/members`);
    document.getElementById('memberList').innerHTML = members.map(m =>
      `<div class="row-between"><span>${esc(m.name)} <span class="text-mute" style="font-size:12px">${esc(m.email)}</span></span><span class="badge">${m.role}</span></div>`
    ).join('');
  } catch {}
}

function isClaimed(img) { return img.claimed_by && !img.claim_expired; }
function isMine(img) { return img.claimed_by === currentUser.id && !img.claim_expired; }
function isAvailable(img) { return img.status === 'unlabeled' && !isClaimed(img); }

async function loadImages() {
  try {
    allImages = await API.get(`/api/projects/${projectId}/images`);
    renderImages();
    // update header count
    const labeled = allImages.filter(i => i.status === 'labeled').length;
    document.getElementById('projMeta').textContent =
      `${project.classes.map(c => c.name).join(' · ') || 'No classes'} — ${labeled}/${allImages.length} labeled`;
    const avail = allImages.filter(isAvailable).length;
    document.getElementById('claimAvail').textContent = `${avail} available to claim`;
  } catch {}
}

function renderImages() {
  const grid = document.getElementById('imageGrid');
  const empty = document.getElementById('emptyImages');
  let filtered = allImages;
  if (currentTab === 'mine') {
    filtered = allImages.filter(i => isMine(i) && i.status === 'unlabeled');
  } else if (currentFilter === 'unlabeled') {
    filtered = allImages.filter(i => i.status === 'unlabeled');
  }
  empty.textContent = currentTab === 'mine' ? 'No active claims.' : 'No images uploaded yet.';
  empty.classList.toggle('hidden', filtered.length > 0 || currentTab === 'stats');
  grid.innerHTML = filtered.map(img => {
    const claimed = isClaimed(img);
    const mine = isMine(img);
    const badgeClass = img.status === 'labeled' ? 'badge-labeled' : (claimed ? 'badge-claimed' : 'badge-unlabeled');
    const badgeText = img.status === 'labeled' ? 'Labeled' : (claimed ? (mine ? 'Mine' : 'Claimed') : 'Unlabeled');
    const releaseBtn = currentTab === 'mine'
      ? `<button class="btn btn-ghost-dark btn-sm thumb-release" onclick="releaseImage(${img.id}, event)">Release</button>`
      : '';
    return `
      <div class="thumb-card" onclick="openImage(${img.id})">
        <img src="${img.url}" alt="${esc(img.filename)}" loading="lazy">
        <div class="thumb-info">
          <div class="row-between">
            <span style="font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(img.filename)}</span>
            <span class="badge ${badgeClass}">${badgeText}</span>
          </div>
          <p class="text-mute" style="font-size:12px;margin-top:4px;">${img.annotation_count} box(es)${claimed && img.claimed_by_name ? ' · ' + esc(img.claimed_by_name) : ''}</p>
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
  if (!count || count < 1) { showErr(errEl, 'Enter a count of at least 1.'); return; }
  try {
    const r = await API.post(`/api/projects/${projectId}/images/claim`, { count });
    okEl.textContent = `Claimed ${r.count} image(s).`;
    okEl.classList.remove('hidden');
    await loadImages();
    if (r.count > 0) setTab('mine');
  } catch (err) { showErr(errEl, err.detail || 'Claim failed'); }
}

async function releaseImage(imageId, e) {
  if (e) e.stopPropagation();
  try {
    await API.post(`/api/projects/${projectId}/images/${imageId}/release`);
    await loadImages();
  } catch {}
}

// Video import — params from the advanced settings form (ceilings are % in the UI, 0..1 in the API)
function videoParams() {
  const num = (id, fallback) => {
    const v = parseFloat(document.getElementById(id).value);
    return Number.isFinite(v) ? v : fallback;
  };
  return {
    tiers: [
      [num('tierCeil0', 0.5) / 100, num('tierInt0', 10)],
      [num('tierCeil1', 2) / 100, num('tierInt1', 5)],
      [num('tierCeil2', 8) / 100, num('tierInt2', 1)],
      [null, num('tierInt3', 0.2)],
    ],
    min_interval: num('minInterval', 0.1),
    max_interval: num('maxInterval', 30),
    max_frames: Math.round(num('maxFrames', 5000)),
    jpeg_quality: Math.round(num('jpegQuality', 90)),
  };
}

async function loadVideoJobs() {
  try {
    const prevDone = new Set((videoJobs || []).filter(j => j.status === 'done').map(j => j.id));
    const jobs = await API.get(`/api/projects/${projectId}/videos`);
    const newlyDone = videoJobs !== null && jobs.some(j => j.status === 'done' && !prevDone.has(j.id));
    videoJobs = jobs;
    renderVideoJobs();
    if (newlyDone) loadImages();
    const active = jobs.some(j => j.status === 'pending' || j.status === 'running' || j.cancel_requested);
    if (active && !videoPollTimer) {
      videoPollTimer = setInterval(loadVideoJobs, 1500);
    } else if (!active && videoPollTimer) {
      clearInterval(videoPollTimer);
      videoPollTimer = null;
    }
  } catch {}
}

function renderVideoJobs() {
  const el = document.getElementById('videoJobs');
  el.innerHTML = (videoJobs || []).map(j => {
    const running = j.status === 'pending' || j.status === 'running';
    const pct = Math.min(100, Math.round((j.progress || 0) * 100));
    const decoded = j.total_frames ? Math.round((j.progress || 0) * j.total_frames) : 0;
    const stats = j.total_frames
      ? `${decoded.toLocaleString()} / ${j.total_frames.toLocaleString()} frames decoded · ${(j.extracted_frames || 0).toLocaleString()} extracted`
      : `${(j.extracted_frames || 0).toLocaleString()} extracted`;
    const cancelBtn = running && !j.cancel_requested
      ? `<button class="btn btn-ghost-dark btn-sm" onclick="cancelVideoJob(${j.id})">Cancel</button>` : '';
    const cancelling = running && j.cancel_requested
      ? `<span class="text-mute" style="font-size:12px">cancelling…</span>` : '';
    return `
      <div class="video-job">
        <div class="row-between">
          <span style="font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(j.filename)}</span>
          <span class="row" style="gap:12px">${cancelling}${cancelBtn}<span class="micro-cap">${esc(j.status)}</span></span>
        </div>
        <div class="progress" style="margin-top:8px;"><div class="progress-fill" style="width:${pct}%"></div></div>
        <p class="text-mute" style="font-size:12px;margin-top:4px;">${stats}</p>
        ${j.status === 'failed' && j.error ? `<p class="error">${esc(j.error)}</p>` : ''}
      </div>`;
  }).join('');
}

async function cancelVideoJob(jobId) {
  try {
    await API.post(`/api/projects/${projectId}/videos/${jobId}/cancel`);
    loadVideoJobs();
  } catch {}
}

async function loadStats() {
  const panel = document.getElementById('statsPanel');
  try {
    const stats = await API.get(`/api/projects/${projectId}/stats`);
    panel.innerHTML = `
      <div class="panel">
        <h2 class="micro-cap mb-2">Member Progress</h2>
        <table class="stats-table">
          <tr class="text-mute"><th>Name</th><th>Role</th><th>Labeled</th><th>Claiming</th></tr>
          ${stats.map(s => `<tr><td>${esc(s.name)}</td><td>${esc(s.role)}</td><td>${s.labeled_count}</td><td>${s.claimed_count}</td></tr>`).join('')}
        </table>
      </div>`;
  } catch {}
}

function openImage(imageId) {
  // View-only by default; the annotate page enables editing when claimed by you.
  window.location.href = `/annotate.html?project=${projectId}&image=${imageId}`;
}

init();
