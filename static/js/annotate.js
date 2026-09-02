/* Annotation canvas: detection (bbox) + pose (bbox + keypoints) modes */
const params = new URLSearchParams(window.location.search);
const projectId = params.get('project');
const imageId = parseInt(params.get('image'));

let project = null;
let imageMeta = null;
let currentUser = null;
let readOnly = false;
let boxes = [];       // { class_id, x, y, w, h, keypoints: [{x,y,v}]|null }
let selectedClassIdx = 0;
let selectedBoxIdx = -1;
let drawing = false;
let drawStart = null;
let drawCurrent = null;
let placing = null;          // { boxIdx, nextKp } while placing keypoints
let placingVis = 2;          // visibility for the next placed keypoint
let draggingKp = null;       // { boxIdx, kpIdx } while dragging a keypoint
let imgElement = new Image();
let imgLoaded = false;
let scale = 1;

const CLASS_COLORS = ['#ff6b6b','#51cf66','#339af0','#ffd43b','#cc5de8','#ff922b','#20c997','#f783ac'];

const canvas = document.getElementById('annotCanvas');
const ctx = canvas.getContext('2d');
const wrap = document.querySelector('.annotate-canvas-wrap');
const classList = document.getElementById('classList');
const boxCount = document.getElementById('boxCount');
const errMsg = document.getElementById('errMsg');
const okMsg = document.getElementById('okMsg');
const kpPanel = document.getElementById('kpPanel');
const kpList = document.getElementById('kpList');
const kpStatus = document.getElementById('kpStatus');

const isPose = () => project && project.mode === 'pose';

async function init() {
  if (!projectId || !imageId) { window.location.href = '/projects.html'; return; }
  try { currentUser = await API.get('/api/auth/me'); } catch { window.location.href = '/'; return; }

  document.getElementById('backLink').href = `/project.html?id=${projectId}`;
  project = await API.get(`/api/projects/${projectId}`);
  document.title = `Annotate — ${project.name}`;

  renderClasses();
  if (isPose()) {
    kpPanel.classList.remove('hidden');
    renderKpPanel();
  }
  await loadImageMeta();
  await loadAnnotations();
  applyReadOnly();

  document.getElementById('saveBtn').onclick = save;
  document.getElementById('clearBtn').onclick = clearAll;
  document.getElementById('releaseBtn').onclick = releaseClaim;
  document.getElementById('claimThisBtn').onclick = claimThis;
  document.getElementById('prevBtn').onclick = () => navigate(-1);
  document.getElementById('nextBtn').onclick = () => navigate(1);

  canvas.addEventListener('mousedown', onMouseDown);
  canvas.addEventListener('mousemove', onMouseMove);
  canvas.addEventListener('mouseup', onMouseUp);
  canvas.addEventListener('mouseleave', () => { if (drawing) { drawing = false; redraw(); } draggingKp = null; });
  document.addEventListener('keydown', onKeyDown);
  window.addEventListener('resize', fitCanvas);
}

function applyReadOnly() {
  const isMember = project.role !== null;
  readOnly = !isMember || !(imageMeta.claimed_by === currentUser.id && !imageMeta.claim_expired);
  document.getElementById('saveBtn').classList.toggle('hidden', readOnly);
  document.getElementById('clearBtn').classList.toggle('hidden', readOnly);
  document.getElementById('releaseBtn').classList.toggle('hidden', readOnly);
  document.getElementById('roBanner').classList.toggle('hidden', !readOnly);
  if (readOnly) {
    document.getElementById('roBanner').textContent = isMember
      ? 'Read only — claim this image to annotate it.'
      : 'Not a member — join the project from its page to annotate.';
  }
  document.getElementById('claimThisBtn').classList.toggle('hidden', !readOnly || !isMember);
}

async function claimThis() {
  try {
    await API.post(`/api/projects/${projectId}/images/${imageId}/claim`);
    window.location.reload();
  } catch (err) { showErr(errMsg, err.detail || 'Claim failed'); }
}

function renderClasses() {
  classList.innerHTML = project.classes.map((c, i) => `
    <div class="class-item ${i === selectedClassIdx ? 'active' : ''}" data-idx="${i}" title="${esc(c.description || '')}">
      <span class="class-swatch" style="background:${CLASS_COLORS[i % CLASS_COLORS.length]}"></span>
      <span>${esc(c.name)}</span>
      <span class="text-mute" style="margin-left:auto;font-size:12px">${i + 1}</span>
    </div>
    ${c.description ? `<p class="text-mute" style="font-size:11px;padding:0 12px 4px;">${esc(c.description)}</p>` : ''}
  `).join('');
  classList.querySelectorAll('.class-item').forEach(el => {
    el.onclick = () => { selectedClassIdx = parseInt(el.dataset.idx); renderClasses(); };
  });
}

function renderKpPanel() {
  kpList.innerHTML = project.keypoints.map((name, i) => {
    const active = placing && i === placing.nextKp ? ' active' : '';
    return `<div class="class-item${active}" data-kp="${i}">
      <span class="text-mute" style="width:18px;font-size:11px">${i}</span><span>${esc(name)}</span>
    </div>`;
  }).join('');
  if (placing) {
    kpStatus.textContent = `Click: ${project.keypoints[placing.nextKp]} (v=${placingVis}, V to toggle, Esc to cancel)`;
  } else {
    kpStatus.textContent = 'Draw a box to start an instance.';
  }
}

async function loadImageMeta() {
  const images = await API.get(`/api/projects/${projectId}/images`);
  imageMeta = images.find(i => i.id === imageId);
  if (!imageMeta) { window.location.href = `/project.html?id=${projectId}`; return; }
  document.getElementById('imageName').textContent = imageMeta.filename;
  updateNavInfo(images);
  imgElement.onload = () => { imgLoaded = true; fitCanvas(); };
  imgElement.src = imageMeta.url;
}

function updateNavInfo(images) {
  const idx = images.findIndex(i => i.id === imageId);
  document.getElementById('navInfo').textContent = `${idx + 1} / ${images.length}`;
}

async function loadAnnotations() {
  try {
    const anns = await API.get(`/api/images/${imageId}/annotations`);
    boxes = anns.map(a => ({ class_id: a.class_id, x: a.x, y: a.y, w: a.w, h: a.h, keypoints: a.keypoints || null }));
    updateBoxCount();
    redraw();
  } catch {}
}

function fitCanvas() {
  if (!imgLoaded) return;
  const pad = 32;
  const availW = wrap.clientWidth - pad * 2;
  const availH = wrap.clientHeight - pad * 2;
  scale = Math.min(availW / imageMeta.width, availH / imageMeta.height, 1);
  canvas.width = Math.round(imageMeta.width * scale);
  canvas.height = Math.round(imageMeta.height * scale);
  redraw();
}

function toCanvas(nx, ny) { return [nx * canvas.width, ny * canvas.height]; }
function toNorm(cx, cy) { return [cx / canvas.width, cy / canvas.height]; }

function boxToCanvas(b) {
  const [cx, cy] = toCanvas(b.x - b.w / 2, b.y - b.h / 2);
  return [cx, cy, b.w * canvas.width, b.h * canvas.height];
}

function classColor(classId) {
  const idx = project.classes.findIndex(c => c.id === classId);
  return CLASS_COLORS[(idx >= 0 ? idx : 0) % CLASS_COLORS.length];
}

function redraw() {
  if (!imgLoaded) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(imgElement, 0, 0, canvas.width, canvas.height);

  boxes.forEach((b, i) => {
    const color = classColor(b.class_id);
    const [x, y, w, h] = boxToCanvas(b);
    ctx.strokeStyle = color;
    ctx.lineWidth = i === selectedBoxIdx ? 3 : 2;
    ctx.strokeRect(x, y, w, h);
    const cls = project.classes.find(c => c.id === b.class_id);
    ctx.font = '12px serif';
    ctx.fillStyle = color;
    ctx.fillText(cls ? cls.name : '?', x + 4, y - 4);

    if (b.keypoints) {
      // skeleton edges between labeled keypoints
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      for (const [a, c] of project.skeleton) {
        const kpa = b.keypoints[a], kpb = b.keypoints[c];
        if (kpa && kpb && kpa.v > 0 && kpb.v > 0) {
          const [ax, ay] = toCanvas(kpa.x, kpa.y);
          const [bx, by] = toCanvas(kpb.x, kpb.y);
          ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
        }
      }
      // keypoint dots: filled = visible(2), hollow = occluded(1), v=0 not drawn
      b.keypoints.forEach((kp, ki) => {
        if (!kp || kp.v === 0) return;
        const [kx, ky] = toCanvas(kp.x, kp.y);
        ctx.beginPath(); ctx.arc(kx, ky, 4, 0, Math.PI * 2);
        if (kp.v === 2) { ctx.fillStyle = color; ctx.fill(); }
        else { ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke(); }
        if (i === selectedBoxIdx) {
          ctx.font = '10px serif';
          ctx.fillStyle = color;
          ctx.fillText(String(ki), kx + 6, ky - 4);
        }
      });
    }
  });

  if (drawing && drawStart && drawCurrent) {
    const x = Math.min(drawStart.x, drawCurrent.x);
    const y = Math.min(drawStart.y, drawCurrent.y);
    const w = Math.abs(drawCurrent.x - drawStart.x);
    const h = Math.abs(drawCurrent.y - drawStart.y);
    ctx.strokeStyle = CLASS_COLORS[selectedClassIdx % CLASS_COLORS.length];
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 4]);
    ctx.strokeRect(x, y, w, h);
    ctx.setLineDash([]);
  }
}

function getMousePos(e) {
  const rect = canvas.getBoundingClientRect();
  return { x: e.clientX - rect.left, y: e.clientY - rect.top };
}

function hitTestBox(pos) {
  for (let i = boxes.length - 1; i >= 0; i--) {
    const [x, y, w, h] = boxToCanvas(boxes[i]);
    if (pos.x >= x && pos.x <= x + w && pos.y >= y && pos.y <= y + h) return i;
  }
  return -1;
}

function hitTestKeypoint(pos) {
  if (selectedBoxIdx < 0) return null;
  const kps = boxes[selectedBoxIdx].keypoints;
  if (!kps) return null;
  for (let i = kps.length - 1; i >= 0; i--) {
    const kp = kps[i];
    if (!kp || kp.v === 0) continue;
    const [kx, ky] = toCanvas(kp.x, kp.y);
    if (Math.hypot(pos.x - kx, pos.y - ky) <= 8) return i;
  }
  return null;
}

function onMouseDown(e) {
  if (readOnly) return;
  const pos = getMousePos(e);

  if (placing) { placeKeypoint(pos); return; }

  if (isPose() && selectedBoxIdx >= 0) {
    const kpIdx = hitTestKeypoint(pos);
    if (kpIdx !== null) {
      draggingKp = { boxIdx: selectedBoxIdx, kpIdx };
      return;
    }
  }

  const hit = hitTestBox(pos);
  if (hit >= 0) {
    selectedBoxIdx = hit;
    redraw();
    return;
  }
  selectedBoxIdx = -1;
  drawing = true;
  drawStart = pos;
  drawCurrent = pos;
}

function onMouseMove(e) {
  if (readOnly) return;
  const pos = getMousePos(e);
  if (draggingKp) {
    const [nx, ny] = toNorm(pos.x, pos.y);
    const kp = boxes[draggingKp.boxIdx].keypoints[draggingKp.kpIdx];
    kp.x = Math.min(1, Math.max(0, nx));
    kp.y = Math.min(1, Math.max(0, ny));
    redraw();
    return;
  }
  if (!drawing) return;
  drawCurrent = pos;
  redraw();
}

function onMouseUp(e) {
  if (readOnly) return;
  if (draggingKp) { draggingKp = null; return; }
  if (!drawing) return;
  drawing = false;
  const pos = getMousePos(e);
  const x1 = Math.min(drawStart.x, pos.x), y1 = Math.min(drawStart.y, pos.y);
  const x2 = Math.max(drawStart.x, pos.x), y2 = Math.max(drawStart.y, pos.y);
  const w = x2 - x1, h = y2 - y1;
  if (w < 4 || h < 4) { redraw(); return; }
  const [nx1, ny1] = toNorm(x1, y1);
  const [nx2, ny2] = toNorm(x2, y2);
  const cls = project.classes[selectedClassIdx];
  if (!cls) return;
  boxes.push({
    class_id: cls.id,
    x: (nx1 + nx2) / 2, y: (ny1 + ny2) / 2,
    w: nx2 - nx1, h: ny2 - ny1,
    keypoints: isPose() ? [] : null,
  });
  selectedBoxIdx = boxes.length - 1;
  updateBoxCount();
  if (isPose()) {
    placing = { boxIdx: selectedBoxIdx, nextKp: 0 };
    renderKpPanel();
  }
  redraw();
}

function placeKeypoint(pos) {
  const box = boxes[placing.boxIdx];
  if (placingVis === 0) {
    box.keypoints.push({ x: 0, y: 0, v: 0 });
  } else {
    const [nx, ny] = toNorm(pos.x, pos.y);
    box.keypoints.push({
      x: Math.min(1, Math.max(0, nx)),
      y: Math.min(1, Math.max(0, ny)),
      v: placingVis,
    });
  }
  placing.nextKp++;
  if (placing.nextKp >= project.keypoints.length) {
    placing = null;
    placingVis = 2;
  }
  renderKpPanel();
  redraw();
}

function cancelPlacing() {
  if (!placing) return;
  boxes.splice(placing.boxIdx, 1);
  placing = null;
  placingVis = 2;
  selectedBoxIdx = -1;
  updateBoxCount();
  renderKpPanel();
  redraw();
}

function onKeyDown(e) {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (readOnly) return;
  const n = parseInt(e.key);
  if (n >= 1 && n <= Math.min(project.classes.length, 8)) {
    selectedClassIdx = n - 1;
    renderClasses();
    return;
  }
  if (e.key === 'Delete' || e.key === 'Backspace') {
    if (placing) { cancelPlacing(); return; }
    if (selectedBoxIdx >= 0 && selectedBoxIdx < boxes.length) {
      boxes.splice(selectedBoxIdx, 1);
      selectedBoxIdx = -1;
      updateBoxCount();
      redraw();
    }
    return;
  }
  if (e.key === 'v' || e.key === 'V') {
    if (placing) {
      placingVis = (placingVis + 2) % 3;  // 2 → 1 → 0 → 2
      renderKpPanel();
    }
    return;
  }
  if (e.key === 's' || e.key === 'S') { save(); return; }
  if (e.key === 'Escape') {
    if (placing) { cancelPlacing(); return; }
    selectedBoxIdx = -1; drawing = false; redraw();
  }
}

function updateBoxCount() { boxCount.textContent = String(boxes.length); }

async function save() {
  if (readOnly) return;
  hideErr(errMsg); okMsg.classList.add('hidden');
  if (placing) { showErr(errMsg, 'Finish or cancel the current keypoint placement first (Esc).'); return; }
  try {
    await API.put(`/api/images/${imageId}/annotations`, boxes);
    okMsg.textContent = `Saved ${boxes.length} instance(s).`;
    okMsg.classList.remove('hidden');
  } catch (err) { showErr(errMsg, err.detail || 'Save failed'); }
}

async function clearAll() {
  if (readOnly) return;
  boxes = [];
  selectedBoxIdx = -1;
  placing = null;
  if (isPose()) renderKpPanel();
  updateBoxCount();
  redraw();
  try {
    await API.del(`/api/images/${imageId}/annotations`);
    okMsg.textContent = 'Cleared.';
    okMsg.classList.remove('hidden');
  } catch (err) { showErr(errMsg, err.detail || 'Clear failed'); }
}

async function releaseClaim() {
  try {
    await API.post(`/api/projects/${projectId}/images/${imageId}/release`);
    window.location.href = `/project.html?id=${projectId}`;
  } catch (err) { showErr(errMsg, err.detail || 'Release failed'); }
}

async function navigate(dir) {
  try {
    const images = await API.get(`/api/projects/${projectId}/images`);
    const idx = images.findIndex(i => i.id === imageId);
    const next = images[idx + dir];
    if (!next) return;
    window.location.href = `/annotate.html?project=${projectId}&image=${next.id}`;
  } catch {}
}

init();
