/* Bounding-box annotation canvas */
const params = new URLSearchParams(window.location.search);
const projectId = params.get('project');
const imageId = parseInt(params.get('image'));

let project = null;
let imageMeta = null;
let boxes = [];       // { class_id, x, y, w, h } normalized
let selectedClassIdx = 0;
let selectedBoxIdx = -1;
let drawing = false;
let drawStart = null;  // { x, y } canvas px
let drawCurrent = null;
let imgElement = new Image();
let imgLoaded = false;
let scale = 1, offsetX = 0, offsetY = 0;

const CLASS_COLORS = ['#ff6b6b','#51cf66','#339af0','#ffd43b','#cc5de8','#ff922b','#20c997','#f783ac'];

const canvas = document.getElementById('annotCanvas');
const ctx = canvas.getContext('2d');
const wrap = document.querySelector('.annotate-canvas-wrap');
const classList = document.getElementById('classList');
const boxCount = document.getElementById('boxCount');
const errMsg = document.getElementById('errMsg');
const okMsg = document.getElementById('okMsg');

async function init() {
  if (!projectId || !imageId) { window.location.href = '/projects.html'; return; }
  try { await API.get('/api/auth/me'); } catch { window.location.href = '/'; return; }

  document.getElementById('backLink').href = `/project.html?id=${projectId}`;
  project = await API.get(`/api/projects/${projectId}`);
  document.title = `Annotate — ${project.name}`;

  renderClasses();
  await loadImageMeta();
  await loadAnnotations();

  document.getElementById('saveBtn').onclick = save;
  document.getElementById('clearBtn').onclick = clearAll;
  document.getElementById('releaseBtn').onclick = releaseClaim;
  document.getElementById('prevBtn').onclick = () => navigate(-1);
  document.getElementById('nextBtn').onclick = () => navigate(1);

  canvas.addEventListener('mousedown', onMouseDown);
  canvas.addEventListener('mousemove', onMouseMove);
  canvas.addEventListener('mouseup', onMouseUp);
  canvas.addEventListener('mouseleave', () => { if (drawing) { drawing = false; redraw(); } });
  document.addEventListener('keydown', onKeyDown);
  window.addEventListener('resize', fitCanvas);
}

function renderClasses() {
  classList.innerHTML = project.classes.map((c, i) => `
    <div class="class-item ${i === selectedClassIdx ? 'active' : ''}" data-idx="${i}">
      <span class="class-swatch" style="background:${CLASS_COLORS[i % CLASS_COLORS.length]}"></span>
      <span>${esc(c.name)}</span>
      <span class="text-mute" style="margin-left:auto;font-size:12px">${i + 1}</span>
    </div>
  `).join('');
  classList.querySelectorAll('.class-item').forEach(el => {
    el.onclick = () => { selectedClassIdx = parseInt(el.dataset.idx); renderClasses(); };
  });
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
    boxes = anns.map(a => ({ class_id: a.class_id, x: a.x, y: a.y, w: a.w, h: a.h }));
    updateBoxCount();
    redraw();
  } catch {}
}

function fitCanvas() {
  if (!imgLoaded) return;
  const pad = 32;
  const availW = wrap.clientWidth - pad * 2;
  const availH = wrap.clientHeight - pad * 2;
  const scaleW = availW / imageMeta.width;
  const scaleH = availH / imageMeta.height;
  scale = Math.min(scaleW, scaleH, 1);
  const w = Math.round(imageMeta.width * scale);
  const h = Math.round(imageMeta.height * scale);
  canvas.width = w;
  canvas.height = h;
  offsetX = 0;
  offsetY = 0;
  redraw();
}

function toCanvas(nx, ny) { return [nx * canvas.width, ny * canvas.height]; }
function toNorm(cx, cy) { return [cx / canvas.width, cy / canvas.height]; }

function boxToCanvas(b) {
  const [cx, cy] = toCanvas(b.x - b.w / 2, b.y - b.h / 2);
  return [cx, cy, b.w * canvas.width, b.h * canvas.height];
}

function redraw() {
  if (!imgLoaded) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(imgElement, 0, 0, canvas.width, canvas.height);

  boxes.forEach((b, i) => {
    const clsIdx = project.classes.findIndex(c => c.id === b.class_id);
    const color = CLASS_COLORS[(clsIdx >= 0 ? clsIdx : 0) % CLASS_COLORS.length];
    const [x, y, w, h] = boxToCanvas(b);
    ctx.strokeStyle = color;
    ctx.lineWidth = i === selectedBoxIdx ? 3 : 2;
    ctx.strokeRect(x, y, w, h);
    const label = project.classes[clsIdx] ? project.classes[clsIdx].name : '?';
    ctx.font = '12px Arial';
    ctx.fillStyle = color;
    ctx.fillText(label, x + 4, y - 4);
    if (i === selectedBoxIdx) {
      ctx.fillStyle = color;
      const handles = [
        [x, y], [x + w, y], [x, y + h], [x + w, y + h],
        [x + w/2, y], [x + w/2, y + h], [x, y + h/2], [x + w, y + h/2],
      ];
      handles.forEach(([hx, hy]) => { ctx.beginPath(); ctx.arc(hx, hy, 4, 0, Math.PI * 2); ctx.fill(); });
    }
  });

  // Draw in-progress box
  if (drawing && drawStart && drawCurrent) {
    const x = Math.min(drawStart.x, drawCurrent.x);
    const y = Math.min(drawStart.y, drawCurrent.y);
    const w = Math.abs(drawCurrent.x - drawStart.x);
    const h = Math.abs(drawCurrent.y - drawStart.y);
    const color = CLASS_COLORS[selectedClassIdx % CLASS_COLORS.length];
    ctx.strokeStyle = color;
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

function hitTest(pos) {
  for (let i = boxes.length - 1; i >= 0; i--) {
    const [x, y, w, h] = boxToCanvas(boxes[i]);
    if (pos.x >= x && pos.x <= x + w && pos.y >= y && pos.y <= y + h) return i;
  }
  return -1;
}

function onMouseDown(e) {
  const pos = getMousePos(e);
  const hit = hitTest(pos);
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
  if (!drawing) return;
  drawCurrent = getMousePos(e);
  redraw();
}

function onMouseUp(e) {
  if (!drawing) return;
  drawing = false;
  const pos = getMousePos(e);
  const x1 = Math.min(drawStart.x, pos.x), y1 = Math.min(drawStart.y, pos.y);
  const x2 = Math.max(drawStart.x, pos.x), y2 = Math.max(drawStart.y, pos.y);
  const w = x2 - x1, h = y2 - y1;
  if (w < 4 || h < 4) { redraw(); return; }  // too small, ignore
  const [nx1, ny1] = toNorm(x1, y1);
  const [nx2, ny2] = toNorm(x2, y2);
  const cls = project.classes[selectedClassIdx];
  if (!cls) return;
  boxes.push({
    class_id: cls.id,
    x: (nx1 + nx2) / 2, y: (ny1 + ny2) / 2,
    w: nx2 - nx1, h: ny2 - ny1,
  });
  selectedBoxIdx = boxes.length - 1;
  updateBoxCount();
  redraw();
}

function onKeyDown(e) {
  if (e.target.tagName === 'INPUT') return;
  const n = parseInt(e.key);
  if (n >= 1 && n <= Math.min(project.classes.length, 8)) {
    selectedClassIdx = n - 1;
    renderClasses();
    return;
  }
  if (e.key === 'Delete' || e.key === 'Backspace') {
    if (selectedBoxIdx >= 0 && selectedBoxIdx < boxes.length) {
      boxes.splice(selectedBoxIdx, 1);
      selectedBoxIdx = -1;
      updateBoxCount();
      redraw();
    }
    return;
  }
  if (e.key === 's' || e.key === 'S') { save(); return; }
  if (e.key === 'Escape') { selectedBoxIdx = -1; drawing = false; redraw(); }
}

function updateBoxCount() { boxCount.textContent = String(boxes.length); }

async function save() {
  hideErr(errMsg); okMsg.classList.add('hidden');
  try {
    await API.put(`/api/images/${imageId}/annotations`, boxes);
    okMsg.textContent = `Saved ${boxes.length} box(es).`;
    okMsg.classList.remove('hidden');
  } catch (err) { showErr(errMsg, err.detail || 'Save failed'); }
}

async function clearAll() {
  boxes = [];
  selectedBoxIdx = -1;
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
    try { await API.post(`/api/projects/${projectId}/images/${next.id}/claim`); } catch {}
    window.location.href = `/annotate.html?project=${projectId}&image=${next.id}`;
  } catch {}
}

init();
