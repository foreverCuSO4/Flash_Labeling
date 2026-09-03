/* Upload page: image upload + video import (chunked, resumable, parallel). */
const params = new URLSearchParams(window.location.search);
const projectId = params.get('project');
let currentUser = null;
let project = null;
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
  document.getElementById('backLink').href = `/project.html?id=${projectId}`;
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

  // Video import — chunked resumable upload (100fps videos are GB-scale).
  const videoErr = document.getElementById('videoErr');
  const videoOk = document.getElementById('videoOk');
  const videoBtn = document.getElementById('videoSubmitBtn');
  const videoUploading = document.getElementById('videoUploading');
  const videoUploadFill = document.getElementById('videoUploadFill');
  const videoUploadStatus = document.getElementById('videoUploadStatus');
  document.getElementById('videoForm').onsubmit = async (e) => {
    e.preventDefault();
    hideErr(videoErr); videoOk.classList.add('hidden');
    const files = [...document.getElementById('videoInput').files];
    if (!files.length) return;
    const sampling = videoParams();
    videoBtn.disabled = true;
    videoUploading.classList.remove('hidden');
    let queued = 0;
    try {
      for (let i = 0; i < files.length; i++) {
        const f = files[i];
        if (!f.size) { showErr(videoErr, `${f.name} is empty`); continue; }
        const which = files.length > 1 ? ` (${i + 1}/${files.length})` : '';
        const uploadId = await chunkedUpload(f, (done, total) => {
          const pct = Math.round(100 * done / total);
          videoUploadFill.style.width = `${pct}%`;
          videoUploadStatus.textContent =
            `Uploading${which} ${f.name}… ${pct}% (${Math.round(done / 1048576)} / ${Math.round(total / 1048576)} MB)`;
        });
        videoUploadFill.style.width = '100%';
        videoUploadStatus.textContent = `Queued for extraction${which}…`;
        await API.post(`/api/uploads/${uploadId}/complete`, { project_id: parseInt(projectId), params: sampling });
        localStorage.removeItem(resumeKey(f));
        queued++;
      }
      if (queued) {
        videoOk.textContent = `${queued} video(s) queued for extraction.`;
        videoOk.classList.remove('hidden');
        document.getElementById('videoInput').value = '';
        loadVideoJobs();
      }
    } catch (err) {
      const msg = typeof err.detail === 'string' ? err.detail : 'Upload failed';
      showErr(videoErr, `${msg} — progress is saved, submit again to resume.`);
    }
    videoBtn.disabled = false;
    videoUploading.classList.add('hidden');
  };

  loadVideoJobs();
}

// --- Chunked resumable video upload ------------------------------------------
// 16 MiB chunks, up to UPLOAD_CONCURRENCY in flight per file. The server
// accepts out-of-order chunks and tracks received ranges, so each request is
// small, retries are idempotent, and reloads resume from the server's state.
const CHUNK_SIZE = 16 * 1024 * 1024;
const UPLOAD_CONCURRENCY = 4;

function resumeKey(file) {
  return `fl_vidup_${projectId}_${file.name}_${file.size}`;
}

function uploadChunk(url, blob, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', url);
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

// Upload `file`, resuming from the server's received ranges. Four chunks are
// kept in flight; onProgress(bytes_done, total) fires on every movement.
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
    } catch { uploadId = null; localStorage.removeItem(key); }  // gone or stale
  }
  if (!uploadId) {
    const init = await API.post('/api/uploads', { filename: file.name, size });
    uploadId = init.upload_id;
    localStorage.setItem(key, uploadId);
  }

  // Up to 3 passes: normally one pass plus a verify; a pass is repeated only if
  // the server reports something different from what we believe we sent.
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
        let idx;
        while (next < bounds.length && !missing.has(next)) next++;
        if (next >= bounds.length) return;
        idx = next++;
        const [s, e] = bounds[idx];
        const len = e - s;
        inFlight.set(idx, { loaded: 0 });
        let attempts = 0;
        for (;;) {
          attempts++;
          try {
            await uploadChunk(`/api/uploads/${uploadId}/chunk?offset=${s}`, file.slice(s, e),
              (loaded) => { const inf = inFlight.get(idx); if (inf) inf.loaded = loaded; tick(); });
            inFlight.delete(idx);
            completedBytes += len;
            tick();
            break;
          } catch (err) {
            if (attempts >= 5) throw err;
            await sleep(1000 * attempts);
          }
        }
      }
    };
    const n = Math.min(concurrency, missing.size);
    await Promise.all(Array.from({ length: n }, () => worker()));
  }
  const st = await API.get(`/api/uploads/${uploadId}`);
  if (st.received < size) {
    throw { detail: 'upload did not complete after retries — submit again to resume' };
  }
  return uploadId;
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
    const jobs = await API.get(`/api/projects/${projectId}/videos`);
    videoJobs = jobs;
    renderVideoJobs();
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
    const knownTotal = (j.total_frames || 0) > 0;
    const pct = knownTotal ? Math.min(100, Math.round((j.progress || 0) * 100)) : 100;
    const decoded = (j.decoded_frames || 0).toLocaleString();
    const extracted = (j.extracted_frames || 0).toLocaleString();
    const stats = knownTotal
      ? `${decoded} / ${j.total_frames.toLocaleString()} frames decoded · ${extracted} extracted`
      : `${decoded} frames decoded · ${extracted} extracted`;
    const indeterminate = running && !knownTotal;
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
        <div class="progress${indeterminate ? ' indeterminate' : ''}" style="margin-top:8px;"><div class="progress-fill" style="width:${pct}%"></div></div>
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

init();
