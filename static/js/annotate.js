/* Annotation canvas: detection (bbox) + pose (bbox + keypoints) + segment (polygon) modes */
const params = new URLSearchParams(window.location.search);
const projectId = params.get('project');
const imageId = parseInt(params.get('image'));

let project = null;
let imageMeta = null;
let currentUser = null;
let readOnly = false;
let boxes = [];       // { class_id, x, y, w, h, keypoints: [{x,y,v}]|null, polygon: [[x,y],...]|null }
let selectedClassIdx = 0;
let selectedBoxIdx = -1;
let drawing = false;
let drawStart = null;
let drawCurrent = null;
let placing = null;          // { boxIdx, nextKp } while placing keypoints
let placingVis = 2;          // visibility for the next placed keypoint
let draggingKp = null;       // { boxIdx, kpIdx } while dragging a keypoint
let polyDraft = null;        // normalized [[x,y],...] of the polygon being drawn
let draftCursor = null;      // canvas coords of the cursor, for draft preview
let draggingVert = null;     // { boxIdx, ptIdx } while dragging a polygon vertex
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
const isSeg = () => project && project.mode === 'segment';

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
  if (isSeg()) document.getElementById('segHint').classList.remove('hidden');
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
  canvas.addEventListener('dblclick', onDblClick);
  canvas.addEventListener('mouseleave', () => { if (drawing) { drawing = false; redraw(); } draggingKp = null; draggingVert = null; draftCursor = null; if (polyDraft) redraw(); });
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
    kpStatus.textContent = 'Draw a box to start an instance, or click empty canvas to place keypoints directly (box is derived).';
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
    boxes = anns.map(a => ({ class_id: a.class_id, x: a.x, y: a.y, w: a.w, h: a.h, keypoints: a.keypoints || null, polygon: a.polygon || null }));
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
    const cls = project.classes.find(c => c.id === b.class_id);

    if (b.polygon) {
      const pts = b.polygon.map(([nx, ny]) => toCanvas(nx, ny));
      ctx.beginPath();
      pts.forEach(([px, py], pi) => { pi ? ctx.lineTo(px, py) : ctx.moveTo(px, py); });
      ctx.closePath();
      ctx.fillStyle = color + '26';  // ~15% fill so the image stays readable
      ctx.fill();
      ctx.strokeStyle = color;
      ctx.lineWidth = i === selectedBoxIdx ? 3 : 2;
      ctx.stroke();
      ctx.font = '12px serif';
      ctx.fillStyle = color;
      ctx.fillText(cls ? cls.name : '?', pts[0][0] + 4, pts[0][1] - 6);
      if (i === selectedBoxIdx) {
        // vertex handles + indices on the selected polygon
        pts.forEach(([px, py], pi) => {
          ctx.beginPath(); ctx.arc(px, py, 4, 0, Math.PI * 2);
          ctx.fillStyle = color; ctx.fill();
          ctx.font = '10px serif';
          ctx.fillText(String(pi), px + 6, py - 4);
        });
      }
      return;
    }

    const [x, y, w, h] = boxToCanvas(b);
    ctx.strokeStyle = color;
    ctx.lineWidth = i === selectedBoxIdx ? 3 : 2;
    ctx.strokeRect(x, y, w, h);
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

  if (placing && placing.boxIdx === null && placing.draft.length) {
    // keypoints-first draft: dots + skeleton edges between visible draft points
    const color = CLASS_COLORS[selectedClassIdx % CLASS_COLORS.length];
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    for (const [a, c] of project.skeleton) {
      const ka = placing.draft[a], kb = placing.draft[c];
      if (ka && kb && ka.v > 0 && kb.v > 0) {
        const [ax, ay] = toCanvas(ka.x, ka.y);
        const [bx, by] = toCanvas(kb.x, kb.y);
        ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
      }
    }
    placing.draft.forEach((kp, ki) => {
      if (kp.v === 0) return;
      const [kx, ky] = toCanvas(kp.x, kp.y);
      ctx.beginPath(); ctx.arc(kx, ky, 4, 0, Math.PI * 2);
      ctx.fillStyle = color; ctx.fill();
      ctx.font = '10px serif';
      ctx.fillText(String(ki), kx + 6, ky - 4);
    });
  }

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

  if (polyDraft && polyDraft.length) {
    // in-progress polygon: dashed edges, rubber-band to cursor, first point ringed
    const color = CLASS_COLORS[selectedClassIdx % CLASS_COLORS.length];
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    polyDraft.forEach(([nx, ny], pi) => {
      const [px, py] = toCanvas(nx, ny);
      pi ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
    });
    if (draftCursor) ctx.lineTo(draftCursor.x, draftCursor.y);
    ctx.stroke();
    ctx.setLineDash([]);
    polyDraft.forEach(([nx, ny], pi) => {
      const [px, py] = toCanvas(nx, ny);
      ctx.beginPath();
      ctx.arc(px, py, pi === 0 ? 5 : 3.5, 0, Math.PI * 2);
      if (pi === 0) { ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke(); }
      else { ctx.fillStyle = color; ctx.fill(); }
    });
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

function polyBBox(pts) {
  // normalized [[x,y],...] -> normalized center bbox
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  const x1 = Math.min(...xs), x2 = Math.max(...xs);
  const y1 = Math.min(...ys), y2 = Math.max(...ys);
  return { x: (x1 + x2) / 2, y: (y1 + y2) / 2, w: Math.max(x2 - x1, 1e-9), h: Math.max(y2 - y1, 1e-9) };
}

function pointInPolygon(pos, canvasPts) {
  // even-odd ray cast
  let inside = false;
  for (let i = 0, j = canvasPts.length - 1; i < canvasPts.length; j = i++) {
    const [xi, yi] = canvasPts[i], [xj, yj] = canvasPts[j];
    if ((yi > pos.y) !== (yj > pos.y) && pos.x < (xj - xi) * (pos.y - yi) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function hitTestPolygon(pos) {
  for (let i = boxes.length - 1; i >= 0; i--) {
    const b = boxes[i];
    if (!b.polygon) continue;
    if (pointInPolygon(pos, b.polygon.map(([nx, ny]) => toCanvas(nx, ny)))) return i;
  }
  return -1;
}

function hitTestVertex(pos) {
  if (selectedBoxIdx < 0) return null;
  const poly = boxes[selectedBoxIdx].polygon;
  if (!poly) return null;
  for (let i = poly.length - 1; i >= 0; i--) {
    const [vx, vy] = toCanvas(poly[i][0], poly[i][1]);
    if (Math.hypot(pos.x - vx, pos.y - vy) <= 8) return i;
  }
  return null;
}

function onMouseDown(e) {
  if (readOnly) return;
  const pos = getMousePos(e);

  if (placing) { placeKeypoint(pos); return; }

  if (isSeg()) {
    if (polyDraft) { addDraftPoint(pos); return; }
    if (selectedBoxIdx >= 0) {
      const vIdx = hitTestVertex(pos);
      if (vIdx !== null) { draggingVert = { boxIdx: selectedBoxIdx, ptIdx: vIdx }; return; }
    }
    const hit = hitTestPolygon(pos);
    if (hit >= 0) { selectedBoxIdx = hit; redraw(); return; }
    // empty canvas: start a new polygon with this click
    selectedBoxIdx = -1;
    polyDraft = [];
    draftCursor = pos;
    addDraftPoint(pos);
    return;
  }

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
  if (draggingVert) {
    const [nx, ny] = toNorm(pos.x, pos.y);
    const b = boxes[draggingVert.boxIdx];
    b.polygon[draggingVert.ptIdx] = [Math.min(1, Math.max(0, nx)), Math.min(1, Math.max(0, ny))];
    Object.assign(b, polyBBox(b.polygon));
    redraw();
    return;
  }
  if (polyDraft) { draftCursor = pos; redraw(); }
  if (!drawing) return;
  drawCurrent = pos;
  redraw();
}

function onMouseUp(e) {
  if (readOnly) return;
  if (draggingKp) { draggingKp = null; return; }
  if (draggingVert) { draggingVert = null; return; }
  if (!drawing) return;
  drawing = false;
  const pos = getMousePos(e);
  const x1 = Math.min(drawStart.x, pos.x), y1 = Math.min(drawStart.y, pos.y);
  const x2 = Math.max(drawStart.x, pos.x), y2 = Math.max(drawStart.y, pos.y);
  const w = x2 - x1, h = y2 - y1;
  if (w < 4 || h < 4) {
    // A click (not a drag) on empty canvas in pose mode starts keypoints-first
    // placement: click the keypoints in order, the box is derived from them.
    if (isPose()) {
      placing = { boxIdx: null, nextKp: 0, draft: [] };
      renderKpPanel();
      placeKeypoint(pos);
      return;
    }
    redraw();
    return;
  }
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

function kpsBBox(kps) {
  // Derive the bbox from the visible keypoints; pad degenerate axes so w,h stay > 0
  // and clamp the center so the box stays inside the image.
  const vis = kps.filter(k => k.v > 0);
  const xs = vis.map(k => k.x), ys = vis.map(k => k.y);
  const w = Math.max(Math.max(...xs) - Math.min(...xs), 2e-3);
  const h = Math.max(Math.max(...ys) - Math.min(...ys), 2e-3);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2;
  const cy = (Math.min(...ys) + Math.max(...ys)) / 2;
  return {
    x: Math.min(1 - w / 2, Math.max(w / 2, cx)),
    y: Math.min(1 - h / 2, Math.max(h / 2, cy)),
    w, h,
  };
}

function placeKeypoint(pos) {
  // boxIdx === null means keypoints-first placement: points collect in a draft
  // and the box is derived from them once the last one is placed.
  const target = placing.boxIdx === null ? placing.draft : boxes[placing.boxIdx].keypoints;
  if (placingVis === 0) {
    target.push({ x: 0, y: 0, v: 0 });
  } else {
    const [nx, ny] = toNorm(pos.x, pos.y);
    target.push({
      x: Math.min(1, Math.max(0, nx)),
      y: Math.min(1, Math.max(0, ny)),
      v: placingVis,
    });
  }
  placing.nextKp++;
  if (placing.nextKp >= project.keypoints.length) {
    if (placing.boxIdx === null && placing.draft.some(k => k.v > 0)) {
      const cls = project.classes[selectedClassIdx];
      boxes.push({ class_id: cls.id, ...kpsBBox(placing.draft), keypoints: placing.draft, polygon: null });
      selectedBoxIdx = boxes.length - 1;
      updateBoxCount();
    }
    placing = null;
    placingVis = 2;
  }
  renderKpPanel();
  redraw();
}

function cancelPlacing() {
  if (!placing) return;
  if (placing.boxIdx !== null) boxes.splice(placing.boxIdx, 1);
  placing = null;
  placingVis = 2;
  selectedBoxIdx = -1;
  updateBoxCount();
  renderKpPanel();
  redraw();
}

// --- segment mode: polygon draft -------------------------------------------

function addDraftPoint(pos) {
  // Clicking near the first point closes the polygon.
  if (polyDraft.length >= 3) {
    const [fx, fy] = toCanvas(polyDraft[0][0], polyDraft[0][1]);
    if (Math.hypot(pos.x - fx, pos.y - fy) <= 10) { closeDraft(); return; }
  }
  const [nx, ny] = toNorm(pos.x, pos.y);
  polyDraft.push([Math.min(1, Math.max(0, nx)), Math.min(1, Math.max(0, ny))]);
  redraw();
}

function closeDraft() {
  if (!polyDraft) return;
  if (polyDraft.length >= 3) {
    const cls = project.classes[selectedClassIdx];
    if (cls) {
      boxes.push({ class_id: cls.id, ...polyBBox(polyDraft), keypoints: null, polygon: polyDraft });
      selectedBoxIdx = boxes.length - 1;
    }
  }
  polyDraft = null;
  draftCursor = null;
  updateBoxCount();
  redraw();
}

function cancelDraft() {
  if (!polyDraft) return;
  polyDraft = null;
  draftCursor = null;
  redraw();
}

function onDblClick(e) {
  if (readOnly || !polyDraft) return;
  const pos = getMousePos(e);
  // The double-click's own clicks already added duplicate points — drop them.
  while (polyDraft.length) {
    const last = polyDraft[polyDraft.length - 1];
    const [px, py] = toCanvas(last[0], last[1]);
    if (Math.hypot(pos.x - px, pos.y - py) <= 10) polyDraft.pop();
    else break;
  }
  closeDraft();
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
    if (polyDraft) { cancelDraft(); return; }
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
  if (e.key === 'Enter') { if (polyDraft) { closeDraft(); return; } }
  if (e.key === 'Escape') {
    if (placing) { cancelPlacing(); return; }
    if (polyDraft) { cancelDraft(); return; }
    selectedBoxIdx = -1; drawing = false; redraw();
  }
}

function updateBoxCount() { boxCount.textContent = String(boxes.length); }

async function save() {
  if (readOnly) return;
  hideErr(errMsg); okMsg.classList.add('hidden');
  if (placing) { showErr(errMsg, 'Finish or cancel the current keypoint placement first (Esc).'); return; }
  if (polyDraft) { showErr(errMsg, 'Finish or cancel the current polygon first (Enter to close, Esc to cancel).'); return; }
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
  polyDraft = null;
  draftCursor = null;
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
