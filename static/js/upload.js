/* Upload page: bulk image upload + parallel, resumable video import. */
const params = new URLSearchParams(window.location.search);
const projectId = params.get('project');
let currentUser = null;
let project = null;
let videoJobs = [];
let videoPollTimer = null;
const uploadItems = new Map();

async function init() {
  if (!projectId) { window.location.href = appPath('/projects.html'); return; }
  try { currentUser = await API.get('/api/auth/me'); } catch { window.location.href = appPath('/'); return; }
  document.getElementById('userName').textContent = currentUser.name;
  document.getElementById('logoutBtn').onclick = async () => {
    await API.post('/api/auth/logout');
    window.location.href = appPath('/');
  };

  try { project = await API.get(`/api/projects/${projectId}`); } catch { window.location.href = appPath('/projects.html'); return; }
  document.getElementById('backLink').href = appPath(`/project.html?id=${projectId}`);
  document.getElementById('projName').textContent = project.name;
  document.getElementById('projRole').textContent = `${project.role || 'guest'} · ${project.mode}`;

  if (!project.role) {
    document.getElementById('uploadPanel').classList.add('hidden');
    document.getElementById('videoPanel').classList.add('hidden');
    const err = document.getElementById('pageErr');
    err.textContent = 'Join this project from its page to upload images and videos.';
    err.classList.remove('hidden');
    return;
  }

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
    } catch (err) { showErr(uploadErr, err.detail || 'Upload failed'); }
  };

  const videoErr = document.getElementById('videoErr');
  const videoOk = document.getElementById('videoOk');
  const videoBtn = document.getElementById('videoSubmitBtn');
  document.getElementById('videoForm').onsubmit = async (e) => {
    e.preventDefault();
    hideErr(videoErr); videoOk.classList.add('hidden');
    const files = [...document.getElementById('videoInput').files];
    if (!files.length) {
      showErr(videoErr, 'Choose at least one video before clicking Upload & Extract.');
      return;
    }
    const sampling = videoParams();
    const items = files.map(file => ({
      key: fileKey(file), file, status: 'waiting', progress: 0, detail: 'Waiting to upload',
    }));
    items.forEach(item => uploadItems.set(item.key, item));
    renderVideoLists();
    videoBtn.disabled = true;

    let cursor = 0;
    const worker = async () => {
      while (cursor < items.length) {
        const item = items[cursor++];
        await uploadVideo(item, sampling);
      }
    };
    try {
      const workers = Math.min(2, items.length);
      await Promise.all(Array.from({ length: workers }, worker));
      const queued = items.filter(item => item.jobId).length;
      const failed = items.filter(item => item.status === 'failed').length;
      if (queued) {
        videoOk.textContent = `${queued} video(s) queued for extraction${failed ? `; ${failed} failed` : ''}.`;
        videoOk.classList.remove('hidden');
      }
      document.getElementById('videoInput').value = '';
      await loadVideoJobs();
    } catch (err) {
      showErr(videoErr, err.detail || 'Upload failed');
    } finally {
      videoBtn.disabled = false;
      renderVideoLists();
    }
  };

  document.getElementById('videoHistoryToggle').onclick = () => {
    const history = document.getElementById('videoHistory');
    const hidden = history.classList.toggle('hidden');
    document.getElementById('videoHistoryToggle').textContent = hidden
      ? 'View completed videos' : 'Hide completed videos';
    if (!hidden) renderVideoHistory();
  };
  document.getElementById('videoDetailsClose').onclick = () => {
    document.getElementById('videoDetailsDialog').close();
  };

  loadVideoJobs();
}

function fileKey(file) {
  return `${file.name}:${file.size}:${file.lastModified}`;
}

async function uploadVideo(item, sampling) {
  const file = item.file;
  if (!file.size) {
    item.status = 'failed'; item.error = `${file.name} is empty`; renderVideoLists(); return;
  }
  item.status = 'uploading'; item.detail = 'Starting resumable upload'; renderVideoLists();
  try {
    const uploadId = await chunkedUpload(file, (done, total) => {
      item.progress = total ? done / total : 0;
      item.detail = `Uploading ${Math.round(done / 1048576)} / ${Math.round(total / 1048576)} MB`;
      renderVideoLists();
    });
    item.progress = 1; item.status = 'queued'; item.detail = 'Queued for extraction'; renderVideoLists();
    const job = await API.post(`/api/uploads/${uploadId}/complete`, {
      project_id: parseInt(projectId), params: sampling,
    });
    item.jobId = job.id;
    item.status = job.status || 'pending';
    item.detail = 'Waiting for extraction';
    localStorage.removeItem(resumeKey(file));
  } catch (err) {
    const msg = typeof err.detail === 'string' ? err.detail : 'Upload failed';
    item.status = 'failed'; item.error = `${msg} — submit again to resume.`;
  }
  renderVideoLists();
}

// --- Chunked resumable video upload ------------------------------------------
const CHUNK_SIZE = 16 * 1024 * 1024;
const UPLOAD_CONCURRENCY = 4;

function resumeKey(file) {
  return `fl_vidup_${projectId}_${file.name}_${file.size}`;
}

function uploadChunk(url, blob, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', appPath(url));
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = (e) => { if (onProgress) onProgress(e.loaded); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch {}
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject({ status: xhr.status, detail: data.detail || xhr.statusText || 'Upload failed' });
    };
    xhr.onerror = () => reject({ detail: 'Network error' });
    xhr.send(blob);
  });
}

function rangesCover(ranges, start, end) {
  return (ranges || []).some(r => r[0] <= start && end <= r[1]);
}

function sleep(ms) { return new Promise(res => setTimeout(res, ms)); }

async function chunkedUpload(file, onProgress, concurrency = UPLOAD_CONCURRENCY) {
  const key = resumeKey(file);
  const size = file.size;
  let uploadId = localStorage.getItem(key);
  let ranges = [];
  if (uploadId) {
    try {
      const st = await API.get(`/api/uploads/${uploadId}`);
      if (st.size === size) ranges = st.ranges || (st.received ? [[0, st.received]] : []);
      else { uploadId = null; localStorage.removeItem(key); }
    } catch { uploadId = null; localStorage.removeItem(key); }
  }
  if (!uploadId) {
    const init = await API.post('/api/uploads', { filename: file.name, size });
    uploadId = init.upload_id;
    localStorage.setItem(key, uploadId);
  }

  for (let pass = 0; pass < 3; pass++) {
    const st = await API.get(`/api/uploads/${uploadId}`);
    if (st.received >= size) return uploadId;
    ranges = st.ranges || (st.received ? [[0, st.received]] : []);
    const bounds = [];
    for (let s = 0; s < size; s += CHUNK_SIZE) bounds.push([s, Math.min(s + CHUNK_SIZE, size)]);
    const missing = new Set();
    let baseBytes = 0;
    bounds.forEach(([s, e], i) => {
      if (rangesCover(ranges, s, e)) baseBytes += e - s;
      else missing.add(i);
    });
    if (!missing.size) continue;

    let completedBytes = 0;
    const inFlight = new Map();
    let next = 0;
    const tick = () => {
      let bytes = baseBytes + completedBytes;
      for (const inf of inFlight.values()) bytes += inf.loaded;
      onProgress(Math.min(bytes, size), size);
    };
    const worker = async () => {
      for (;;) {
        while (next < bounds.length && !missing.has(next)) next++;
        if (next >= bounds.length) return;
        const idx = next++;
        const [s, e] = bounds[idx];
        inFlight.set(idx, { loaded: 0 });
        let attempts = 0;
        for (;;) {
          attempts++;
          try {
            await uploadChunk(`/api/uploads/${uploadId}/chunk?offset=${s}`, file.slice(s, e),
              loaded => { const inf = inFlight.get(idx); if (inf) inf.loaded = loaded; tick(); });
            inFlight.delete(idx); completedBytes += e - s; tick(); break;
          } catch (err) {
            if (attempts >= 5) throw err;
            await sleep(1000 * attempts);
          }
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(concurrency, missing.size) }, () => worker()));
  }
  const st = await API.get(`/api/uploads/${uploadId}`);
  if (st.received < size) throw { detail: 'upload did not complete after retries — submit again to resume' };
  return uploadId;
}

function videoParams() {
  const num = (id, fallback) => {
    const v = parseFloat(document.getElementById(id).value);
    return Number.isFinite(v) ? v : fallback;
  };
  const common = {
    max_frames: Math.round(num('maxFrames', 5000)),
    jpeg_quality: Math.round(num('jpegQuality', 90)),
  };
  return {
    ...common,
    auto: {
      conf: num('autoConf', 0.2),
      dilate_s: num('autoDilate', 3),
      sample_fps: num('autoFps', 10),
      ...common,
    },
  };
}

async function loadVideoJobs() {
  try {
    videoJobs = await API.get(`/api/projects/${projectId}/videos`);
    for (const [key, item] of uploadItems) {
      const job = videoJobs.find(j => j.id === item.jobId);
      if (job && ['done', 'failed', 'cancelled'].includes(job.status)) uploadItems.delete(key);
    }
    renderVideoLists();
    const active = videoJobs.some(j => j.status === 'pending' || j.status === 'running' || j.cancel_requested);
    if (active && !videoPollTimer) videoPollTimer = setInterval(loadVideoJobs, 1500);
    else if (!active && videoPollTimer) { clearInterval(videoPollTimer); videoPollTimer = null; }
  } catch {}
}

function renderVideoLists() {
  const activeJobs = videoJobs.filter(j => j.status === 'pending' || j.status === 'running' || j.cancel_requested);
  const mapped = new Set(activeJobs.map(j => j.id));
  const pendingUploads = [...uploadItems.values()].filter(item => !item.jobId || !mapped.has(item.jobId));
  const cards = pendingUploads.map(renderUploadCard).concat(activeJobs.map(renderJobCard));
  document.getElementById('videoJobs').innerHTML = cards.join('');
  document.getElementById('videoQueueCount').textContent = cards.length ? `${cards.length} active` : '';

  const history = videoJobs.filter(j => ['done', 'failed', 'cancelled'].includes(j.status));
  const section = document.getElementById('videoHistorySection');
  section.classList.toggle('hidden', !history.length);
  document.getElementById('videoHistoryToggle').textContent =
    document.getElementById('videoHistory').classList.contains('hidden')
      ? `View completed videos (${history.length})` : 'Hide completed videos';
  if (!document.getElementById('videoHistory').classList.contains('hidden')) renderVideoHistory();
}

function renderUploadCard(item) {
  const pct = Math.round(Math.max(0, Math.min(1, item.progress || 0)) * 100);
  const failed = item.status === 'failed';
  return `<article class="video-card upload-card ${failed ? 'is-failed' : ''}">
    <div class="video-card-head"><strong title="${esc(item.file.name)}">${esc(item.file.name)}</strong><span class="micro-cap">${esc(item.status)}</span></div>
    <div class="progress"><div class="progress-fill" style="width:${pct}%"></div></div>
    <p class="text-mute video-card-meta">${esc(item.detail || '')}</p>
    ${failed ? `<p class="error">${esc(item.error || 'Upload failed')}</p>` : ''}
  </article>`;
}

function renderJobCard(j) {
  const knownTotal = (j.total_frames || 0) > 0;
  const pct = knownTotal ? Math.min(100, Math.round((j.progress || 0) * 100)) : (j.status === 'running' ? 100 : 0);
  const indeterminate = j.status === 'running' && !knownTotal;
  const decoded = (j.decoded_frames || 0).toLocaleString();
  const extracted = (j.extracted_frames || 0).toLocaleString();
  const stats = knownTotal ? `${decoded} / ${j.total_frames.toLocaleString()} frames decoded · ${extracted} extracted` : `${decoded} frames decoded · ${extracted} extracted`;
  const cancelBtn = (j.status === 'pending' || j.status === 'running') && !j.cancel_requested
    ? `<button class="btn btn-ghost-dark btn-sm" onclick="cancelVideoJob(${j.id})">Cancel</button>` : '';
  const cancelling = j.cancel_requested ? '<span class="text-mute" style="font-size:12px">cancelling...</span>' : '';
  return `<article class="video-card">
    <div class="video-card-head"><strong title="${esc(j.filename)}">${esc(j.filename)}</strong><span class="row" style="gap:8px">${cancelling}${cancelBtn}<span class="micro-cap">${esc(j.status)}</span></span></div>
    <div class="progress${indeterminate ? ' indeterminate' : ''}"><div class="progress-fill" style="width:${pct}%"></div></div>
    <p class="text-mute video-card-meta">${stats}</p>
  </article>`;
}

function renderVideoHistory() {
  const history = videoJobs.filter(j => ['done', 'failed', 'cancelled'].includes(j.status));
  const el = document.getElementById('videoHistory');
  el.innerHTML = history.map(j => {
    const extra = j.status === 'done' ? `${(j.extracted_frames || 0).toLocaleString()} frames extracted` : (j.error || j.status);
    return `<button type="button" class="video-card video-history-card" data-job-id="${j.id}">
      <span class="video-card-head"><strong title="${esc(j.filename)}">${esc(j.filename)}</strong><span class="micro-cap">${esc(j.status)}</span></span>
      <span class="text-mute video-card-meta">${esc(extra)}</span>
    </button>`;
  }).join('');
  el.querySelectorAll('.video-history-card').forEach(card => {
    card.onclick = () => showVideoDetails(videoJobs.find(j => j.id === parseInt(card.dataset.jobId)));
  });
}

function showVideoDetails(job) {
  if (!job) return;
  const body = document.getElementById('videoDetailsBody');
  const videoUrl = appPath(`/api/projects/${projectId}/videos/${job.id}/file`);
  body.innerHTML = `<p><strong>${esc(job.filename)}</strong></p>
    <p class="text-mute">Status: ${esc(job.status)}<br>Extracted frames: ${(job.extracted_frames || 0).toLocaleString()}<br>Decoded frames: ${(job.decoded_frames || 0).toLocaleString()}</p>
    ${job.status === 'done' ? `<video controls preload="metadata" src="${videoUrl}" style="width:100%;max-height:420px;margin-top:12px"></video>` : ''}
    ${job.error ? `<p class="error">${esc(job.error)}</p>` : ''}`;
  document.getElementById('videoDetailsDialog').showModal();
}

async function cancelVideoJob(jobId) {
  try {
    await API.post(`/api/projects/${projectId}/videos/${jobId}/cancel`);
    loadVideoJobs();
  } catch {}
}

init();
