/* Annotation canvas: detection (bbox) + pose (bbox + keypoints) + segment (polygon) modes */
const params = new URLSearchParams(window.location.search);
const projectId = params.get('project');
let imageId = parseInt(params.get('image'));

let project = null;
let imageMeta = null;
let currentUser = null;
let readOnly = false;
let boxes = [];       // { class_id, x, y, w, h, corners: [[x,y],...]|null, keypoints: [{x,y,v}]|null, polygon: [[x,y],...]|null }
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
const classPickerBtn = document.getElementById('classPickerBtn');
const classPickerCard = document.getElementById('classPickerCard');
const annotationEditor = document.getElementById('annotationEditor');
const readonlySummary = document.getElementById('readonlySummary');
const annotateControls = document.getElementById('annotateControls');
const boxCount = document.getElementById('boxCount');
const errMsg = document.getElementById('errMsg');
const okMsg = document.getElementById('okMsg');
const kpPanel = document.getElementById('kpPanel');
const kpList = document.getElementById('kpList');
const kpStatus = document.getElementById('kpStatus');
const brushPanel = document.getElementById('brushPanel');
const brushBtn = document.getElementById('brushBtn');
const brushStatus = document.getElementById('brushStatus');
const brushRadiusInput = document.getElementById('brushRadius');
const shortcutBtn = document.getElementById('shortcutBtn');
const shortcutCard = document.getElementById('shortcutCard');

const isPose = () => project && project.mode === 'pose';
const isSeg = () => project && project.mode === 'segment';

let brushOn = false;
let brushCursor = null;   // canvas coords while the brush is on, for the circle preview
let brushRadius = 60;     // display pixels (converted to image px by /scale)
let saveQueue = Promise.resolve();
const pendingOperations = new Set();
let leavingPage = false;
let switchingImage = false;
let imageList = null;
let imageLoadToken = 0;
const imageAssets = new Map();
const annotationCache = new Map();

async function init() {
  if (!projectId || !imageId) { window.location.href = appPath('/projects.html'); return; }
  try { currentUser = await API.get('/api/auth/me'); } catch { window.location.href = appPath('/'); return; }

  const backLink = document.getElementById('backLink');
  backLink.href = appPath(`/project.html?id=${projectId}`);
  backLink.onclick = (e) => {
    e.preventDefault();
    leavePage(backLink.href);
  };
  project = await API.get(`/api/projects/${projectId}`);
  document.title = `Annotate — ${project.name}`;

  renderClasses();
  if (isPose()) {
    kpPanel.classList.remove('hidden');
    renderKpPanel();
  }
  if (isSeg()) document.getElementById('segHint').classList.remove('hidden');
  imageList = await API.get(`/api/projects/${projectId}/images`);
  const initialMeta = imageList.find(i => i.id === imageId);
  if (!initialMeta) { window.location.href = appPath(`/project.html?id=${projectId}`); return; }
  await activateImage(initialMeta);
  applyReadOnly();

  document.getElementById('saveBtn').onclick = save;
  document.getElementById('clearBtn').onclick = clearAll;
  document.getElementById('releaseBtn').onclick = releaseClaim;
  document.getElementById('claimThisBtn').onclick = claimThis;
  document.getElementById('prevBtn').onclick = () => navigate(-1);
  document.getElementById('nextBtn').onclick = () => navigate(1);

  brushPanel.classList.toggle('hidden', isSeg());
  brushBtn.onclick = () => setBrush(!brushOn);
  brushRadiusInput.oninput = () => { brushRadius = parseInt(brushRadiusInput.value) || 60; redraw(); };
  classPickerBtn.onclick = () => setClassPicker(classPickerCard.classList.contains('hidden'));
  shortcutBtn.onclick = () => setShortcutCard(shortcutCard.classList.contains('hidden'));
  canvas.addEventListener('mousedown', onMouseDown);
  canvas.addEventListener('mousemove', onMouseMove);
  canvas.addEventListener('mouseup', onMouseUp);
  canvas.addEventListener('wheel', onCanvasWheel, { passive: false });
  canvas.addEventListener('contextmenu', onContextMenu);
  canvas.addEventListener('dblclick', onDblClick);
  canvas.addEventListener('mouseleave', () => {
    const draggedAnnotation = Boolean(draggingKp || draggingVert);
    if (drawing) drawing = false;
    draggingKp = null;
    draggingVert = null;
    draftCursor = null;
    brushCursor = null;
    redraw();
    if (draggedAnnotation) autoSave();
  });
  document.addEventListener('keydown', onKeyDown);
  window.addEventListener('resize', fitCanvas);

  // Brush is the primary annotation interaction. Segment projects keep the
  // polygon workflow because brush suggestions are not available there.
  if (!isSeg()) setBrush(true);
  prefetchAdjacentImages();
}

function applyReadOnly() {
  const isMember = project.role !== null;
  const hasActiveClaim = imageMeta.claimed_by != null && !imageMeta.claim_expired;
  const isLabeled = imageMeta.status === 'labeled' || Number(imageMeta.annotation_count) > 0;
  const canEdit = isMember && imageMeta.claimed_by === currentUser.id && !imageMeta.claim_expired;
  const canClaim = isMember && !hasActiveClaim && !isLabeled;
  readOnly = !canEdit;

  annotationEditor.classList.toggle('hidden', !canEdit);
  readonlySummary.classList.toggle('hidden', canEdit || canClaim);
  annotateControls.classList.toggle('hidden', !canEdit && !canClaim);
  annotateControls.classList.toggle('claim-only', !canEdit && canClaim);

  document.getElementById('saveBtn').classList.toggle('hidden', readOnly);
  document.getElementById('clearBtn').classList.toggle('hidden', readOnly);
  document.getElementById('releaseBtn').classList.toggle('hidden', readOnly);
  document.getElementById('roBanner').classList.add('hidden');
  document.getElementById('claimThisBtn').classList.toggle('hidden', !canClaim);
  if (!canEdit && !canClaim) renderReadonlySummary(isLabeled, isMember, hasActiveClaim);
}

function formatAnnotationDate(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function renderReadonlySummary(isLabeled, isMember, hasActiveClaim) {
  const classCounts = new Map();
  boxes.forEach(box => {
    const cls = project.classes.find(c => c.id === box.class_id);
    const name = cls ? cls.name : `Class ${box.class_id}`;
    classCounts.set(name, (classCounts.get(name) || 0) + 1);
  });
  const classes = [...classCounts.entries()].map(([name, count]) => `${esc(name)} × ${count}`).join(', ');
  const claimInfo = hasActiveClaim
    ? `<div class="annotate-summary-row"><span>Claimed by</span><strong>${esc(imageMeta.claimed_by_name || 'another user')}</strong></div>`
    : '';
  const accessInfo = !isMember
    ? '<p class="text-mute annotate-summary-note">Join this project to claim and annotate images.</p>'
    : (hasActiveClaim ? '<p class="text-mute annotate-summary-note">This image is currently being annotated by another user.</p>' : '');
  readonlySummary.innerHTML = `
    <p class="micro-cap mb-2">Annotation summary</p>
    <div class="annotate-summary-list">
      <div class="annotate-summary-row"><span>Status</span><strong>${isLabeled ? 'Labeled' : 'Unlabeled'}</strong></div>
      <div class="annotate-summary-row"><span>Instances</span><strong>${Number(imageMeta.annotation_count) || boxes.length || 0}</strong></div>
      ${classes ? `<div class="annotate-summary-row"><span>Classes</span><strong>${classes}</strong></div>` : ''}
      ${isLabeled ? `<div class="annotate-summary-row"><span>Labeled by</span><strong>${esc(imageMeta.labeled_by_name || '—')}</strong></div>` : ''}
      ${isLabeled ? `<div class="annotate-summary-row"><span>Labeled at</span><strong>${esc(formatAnnotationDate(imageMeta.labeled_at))}</strong></div>` : ''}
      ${claimInfo}
    </div>
    ${accessInfo}
  `;
}

async function claimThis() {
  try {
    await API.post(`/api/projects/${projectId}/images/${imageId}/claim`);
    window.location.reload();
  } catch (err) { showErr(errMsg, err.detail || 'Claim failed'); }
}

function setBrush(on) {
  brushOn = on;
  if (brushOn) {
    // Entering brush mode ends any in-progress draw/placement.
    drawing = false; polyDraft = null; draftCursor = null;
    if (placing) cancelPlacing();
    brushStatus.classList.remove('hidden');
  } else {
    brushCursor = null;
    brushStatus.classList.add('hidden');
  }
  brushBtn.textContent = `Brush: ${brushOn ? 'On' : 'Off'}`;
  brushBtn.classList.toggle('active', brushOn);
  redraw();
}

function setShortcutCard(open) {
  shortcutCard.classList.toggle('hidden', !open);
  shortcutBtn.setAttribute('aria-expanded', String(open));
}

function setClassPicker(open) {
  classPickerCard.classList.toggle('hidden', !open);
  classPickerBtn.setAttribute('aria-expanded', String(open));
}

function onCanvasWheel(e) {
  if (!brushOn || readOnly || switchingImage || isSeg()) return;
  e.preventDefault();
  const min = Number(brushRadiusInput.min) || 15;
  const max = Number(brushRadiusInput.max) || 200;
  const step = Number(brushRadiusInput.step) || 5;
  const direction = e.deltaY < 0 ? 1 : -1;
  brushRadius = Math.min(max, Math.max(min, brushRadius + direction * step));
  brushRadiusInput.value = String(brushRadius);
  redraw();
}

function onContextMenu(e) {
  // The canvas uses the secondary button as an erase gesture.
  e.preventDefault();
  if (readOnly || switchingImage) return;
  const pos = getMousePos(e);
  if (placing) { cancelPlacing(); return; }
  if (polyDraft) { cancelDraft(); return; }

  const hit = isSeg() ? hitTestPolygon(pos) : hitTestBox(pos);
  if (hit >= 0) removeBoxAt(hit);
}

function trackOperation(promise) {
  let tracked;
  tracked = Promise.resolve(promise).finally(() => pendingOperations.delete(tracked));
  pendingOperations.add(tracked);
  return tracked;
}

async function flushPendingOperations() {
  while (pendingOperations.size) {
    await Promise.all([...pendingOperations]);
  }
  await saveQueue.catch(() => {});
}

function keypointsFromModelCorners(corners) {
  if (!isPose() || project.keypoints.length !== 4 || !Array.isArray(corners) || corners.length !== 4) return null;
  const keypoints = [];
  for (const point of corners) {
    if (!Array.isArray(point) || point.length < 2 || !Number.isFinite(point[0]) || !Number.isFinite(point[1])) return null;
    keypoints.push({
      x: Math.min(1, Math.max(0, point[0])),
      y: Math.min(1, Math.max(0, point[1])),
      v: 2,
    });
  }
  return keypoints;
}

function completePoseBoxKeypoints(box) {
  if (!isPose()) return false;
  const expected = project.keypoints.length;
  const source = Array.isArray(box.keypoints) ? box.keypoints : [];
  const current = source.slice(0, expected);
  if (source.length === expected) return false;

  // An untouched four-corner brush suggestion already contains the complete
  // pose geometry. Preserve any manually placed points; only use the model
  // corners when the keypoint list is still empty.
  box.keypoints = current.length === 0 ? (keypointsFromModelCorners(box.corners) || current) : current;
  while (box.keypoints.length < expected) {
    box.keypoints.push({ x: 0, y: 0, v: 0 });
  }
  return true;
}

async function doBrush(pos) {
  if (readOnly || isSeg()) return;
  const [nx, ny] = toNorm(pos.x, pos.y);
  const rPx = brushRadius / scale;   // display px -> image px
  try {
    const res = await API.post(`/api/images/${imageId}/brush`, { x: nx, y: ny, r: rPx });
    const s = res.suggestion;
    if (!s) {
      okMsg.textContent = `No detection in the brush area (${res.rows} cached row(s)).`;
      okMsg.classList.remove('hidden');
      setTimeout(() => okMsg.classList.add('hidden'), 1500);
      return;
    }
    if (readOnly) return;
    const modelClassId = Number.isInteger(s.class_id) ? s.class_id : -1;
    const modelClassName = typeof s.class_name === 'string' ? s.class_name.toLowerCase() : '';
    const modelClassIndex = Number.isInteger(s.class_index) ? s.class_index : -1;
    let detectedClassIdx = project.classes.findIndex(c => c.id === modelClassId);
    if (detectedClassIdx < 0 && modelClassName) {
      detectedClassIdx = project.classes.findIndex(c => c.name.toLowerCase() === modelClassName);
    }
    if (detectedClassIdx < 0) {
      detectedClassIdx = project.classes.findIndex(c => c.ord === modelClassIndex);
    }
    if (detectedClassIdx >= 0) {
      selectedClassIdx = detectedClassIdx;
      renderClasses();
    }
    const cls = project.classes[detectedClassIdx >= 0 ? detectedClassIdx : selectedClassIdx];
    if (!cls) return;
    const modelKeypoints = keypointsFromModelCorners(s.corners);
    const box = {
      class_id: cls.id,
      x: s.x, y: s.y, w: s.w, h: s.h,
      // Keep the oriented quadrilateral for the canvas preview. Four-keypoint
      // pose projects also persist these corners as visible keypoints.
      corners: Array.isArray(s.corners) ? s.corners : null,
      keypoints: isPose() ? (modelKeypoints || []) : null,
      polygon: null,
    };
    boxes.push(box);
    selectedBoxIdx = boxes.length - 1;
    updateBoxCount();
    if (isPose() && !modelKeypoints) {
      placing = { boxIdx: selectedBoxIdx, nextKp: 0 };
      renderKpPanel();
      // Projects whose keypoint count does not match the four model corners
      // still use manual placement, so hand canvas clicks back to that flow.
      setBrush(false);
    }
    redraw();
    if (!isPose() || modelKeypoints) autoSave();
  } catch (err) {
    const detail = typeof err.detail === 'string' ? err.detail : '';
    if (err.status === 503) {
      showErr(errMsg, `Auto-suggest unavailable: ${detail || 'inference service not running'}`);
    } else {
      showErr(errMsg, detail || 'Brush failed');
    }
  }
}

function renderClasses() {
  const selected = project.classes[selectedClassIdx];
  classPickerBtn.textContent = selected ? `Class: ${selected.name}` : 'Select class';
  classList.innerHTML = project.classes.map((c, i) => `
    <div class="class-item ${i === selectedClassIdx ? 'active' : ''}" data-idx="${i}" role="option" aria-selected="${i === selectedClassIdx}" title="${esc(c.description || '')}">
      <span class="class-swatch" style="background:${CLASS_COLORS[i % CLASS_COLORS.length]}"></span>
      <span>${esc(c.name)}</span>
      <span class="text-mute" style="margin-left:auto;font-size:12px">${i + 1}</span>
    </div>
    ${c.description ? `<p class="text-mute" style="font-size:11px;padding:0 12px 4px;">${esc(c.description)}</p>` : ''}
  `).join('');
  classList.querySelectorAll('.class-item').forEach(el => {
    el.onclick = () => {
      selectedClassIdx = parseInt(el.dataset.idx);
      renderClasses();
      setClassPicker(false);
    };
  });
}

function renderKpPanel() {
  kpList.innerHTML = project.keypoints.map((name, i) => {
    const active = placing && i === placing.nextKp ? ' active' : '';
    const done = placing && i < placing.nextKp ? ' done' : '';
    return `<div class="kp-tile${active}${done}" data-kp="${i}" title="${esc(name)}" aria-label="${esc(name)}">${i}</div>`;
  }).join('');
  if (placing) {
    kpStatus.textContent = `Click: ${project.keypoints[placing.nextKp]} (v=${placingVis}, V to toggle, Esc to cancel)`;
  } else {
    kpStatus.textContent = 'Draw a box to start an instance, or click empty canvas to place keypoints directly (box is derived).';
  }
}

function updateNavInfo(images) {
  const idx = images.findIndex(i => i.id === imageId);
  document.getElementById('navInfo').textContent = `${idx + 1} / ${images.length}`;
}

function boxesFromAnnotations(anns) {
  return anns.map(a => ({
      class_id: a.class_id, x: a.x, y: a.y, w: a.w, h: a.h,
      corners: a.keypoints && a.keypoints.length === 4 && a.keypoints.every(kp => kp && kp.v > 0)
        ? keypointsFromModelCorners(a.keypoints.map(kp => [kp.x, kp.y])) : null,
      keypoints: a.keypoints ? a.keypoints.map(kp => ({ ...kp })) : null,
      polygon: a.polygon ? a.polygon.map(point => [...point]) : null,
  }));
}

function loadImageAsset(meta) {
  const cached = imageAssets.get(meta.id);
  if (cached) return cached.promise;
  const image = new Image();
  const promise = new Promise((resolve, reject) => {
    image.onload = () => resolve(image);
    image.onerror = () => {
      imageAssets.delete(meta.id);
      reject(new Error(`Unable to load image ${meta.id}`));
    };
  });
  imageAssets.set(meta.id, { image, promise });
  image.src = appPath(meta.url);
  return promise;
}

async function loadAnnotationsFor(imageIdToLoad) {
  if (annotationCache.has(imageIdToLoad)) {
    return annotationCache.get(imageIdToLoad);
  }
  const anns = await API.get(`/api/images/${imageIdToLoad}/annotations`);
  annotationCache.set(imageIdToLoad, anns);
  return anns;
}

async function activateImage(meta) {
  const token = ++imageLoadToken;
  const [asset, anns] = await Promise.all([
    loadImageAsset(meta),
    loadAnnotationsFor(meta.id),
  ]);
  if (token !== imageLoadToken) return false;

  imageId = meta.id;
  imageMeta = meta;
  imgElement = asset;
  imgLoaded = true;
  boxes = boxesFromAnnotations(anns);
  selectedBoxIdx = -1;
  drawing = false;
  drawStart = drawCurrent = null;
  placing = null;
  polyDraft = null;
  draftCursor = null;
  draggingKp = null;
  draggingVert = null;
  brushCursor = null;
  document.getElementById('imageName').textContent = imageMeta.filename;
  updateNavInfo(imageList);
  updateBoxCount();
  if (isPose()) renderKpPanel();
  applyReadOnly();
  fitCanvas();
  redraw();
  return true;
}

function prefetchAdjacentImages() {
  if (!imageList) return;
  const idx = imageList.findIndex(i => i.id === imageId);
  for (const neighbor of [imageList[idx - 1], imageList[idx + 1]]) {
    if (!neighbor) continue;
    loadImageAsset(neighbor).catch(() => {});
    loadAnnotationsFor(neighbor.id).catch(() => {});
  }
}

function setSwitchingImage(value) {
  switchingImage = value;
  document.getElementById('prevBtn').disabled = value;
  document.getElementById('nextBtn').disabled = value;
  canvas.setAttribute('aria-busy', String(value));
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

function drawQuadrilateral(corners, color, selected, label, showPoints = true) {
  if (!Array.isArray(corners) || corners.length < 3) return false;
  const pts = corners
    .filter(p => Array.isArray(p) && p.length >= 2 && Number.isFinite(p[0]) && Number.isFinite(p[1]))
    .map(([nx, ny]) => toCanvas(nx, ny));
  if (pts.length < 3) return false;

  ctx.beginPath();
  pts.forEach(([px, py], pi) => { pi ? ctx.lineTo(px, py) : ctx.moveTo(px, py); });
  ctx.closePath();
  ctx.fillStyle = color + '26';  // ~15% fill so the image stays readable
  ctx.fill();
  ctx.strokeStyle = color;
  ctx.lineWidth = selected ? 3 : 2;
  ctx.stroke();
  ctx.font = '12px serif';
  ctx.fillStyle = color;
  ctx.fillText(label || '?', pts[0][0] + 4, pts[0][1] - 6);

  if (!showPoints) return true;

  // Show the four corner points and their order on the preview.
  pts.forEach(([px, py], pi) => {
    ctx.beginPath(); ctx.arc(px, py, selected ? 4 : 3, 0, Math.PI * 2);
    ctx.fillStyle = color; ctx.fill();
    if (selected) {
      ctx.font = '10px serif';
      ctx.fillStyle = color;
      ctx.fillText(String(pi), px + 6, py - 4);
    }
  });
  return true;
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

    const poseCorners = b.keypoints && b.keypoints.length === 4 && b.keypoints.every(kp => kp && kp.v > 0)
      ? b.keypoints.map(kp => [kp.x, kp.y]) : null;
    const hasCorners = drawQuadrilateral(
      poseCorners || b.corners, color, i === selectedBoxIdx, cls ? cls.name : '?', !poseCorners,
    );
    if (!hasCorners) {
      const [x, y, w, h] = boxToCanvas(b);
      ctx.strokeStyle = color;
      ctx.lineWidth = i === selectedBoxIdx ? 3 : 2;
      ctx.strokeRect(x, y, w, h);
      ctx.font = '12px serif';
      ctx.fillStyle = color;
      ctx.fillText(cls ? cls.name : '?', x + 4, y - 4);
    }

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

  if (brushOn && brushCursor) {
    ctx.beginPath();
    ctx.arc(brushCursor.x, brushCursor.y, brushRadius, 0, Math.PI * 2);
    ctx.strokeStyle = 'rgba(255,255,255,0.9)';
    ctx.lineWidth = 1.5;
    ctx.setLineDash([5, 4]);
    ctx.stroke();
    ctx.setLineDash([]);
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

function removeBoxAt(index) {
  if (index < 0 || index >= boxes.length) return;
  boxes.splice(index, 1);
  if (selectedBoxIdx === index) selectedBoxIdx = -1;
  else if (selectedBoxIdx > index) selectedBoxIdx--;
  updateBoxCount();
  redraw();
  autoSave();
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
  if (readOnly || switchingImage) return;
  if (e.button === 2) return;
  const pos = getMousePos(e);

  if (brushOn) { trackOperation(doBrush(pos)); return; }

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
  if (readOnly || switchingImage) return;
  const pos = getMousePos(e);
  if (brushOn) {
    brushCursor = pos;
    redraw();
    return;
  }
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
  if (readOnly || switchingImage) return;
  if (e.button !== 0) {
    drawing = false;
    drawStart = drawCurrent = null;
    return;
  }
  if (draggingKp) { draggingKp = null; autoSave(); return; }
  if (draggingVert) { draggingVert = null; autoSave(); return; }
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
    corners: null,
    keypoints: isPose() ? [] : null,
  });
  selectedBoxIdx = boxes.length - 1;
  updateBoxCount();
  if (isPose()) {
    placing = { boxIdx: selectedBoxIdx, nextKp: 0 };
    renderKpPanel();
  }
  redraw();
  if (!isPose()) autoSave();
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
      boxes.push({ class_id: cls.id, ...kpsBBox(placing.draft), corners: null, keypoints: placing.draft, polygon: null });
      selectedBoxIdx = boxes.length - 1;
      updateBoxCount();
    }
    placing = null;
    placingVis = 2;
    autoSave();
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

function finishPlacingForNavigation() {
  if (!placing) return false;
  if (placing.boxIdx === null) {
    // Keep a keypoints-first draft only when it has at least one visible
    // point; the remaining points can be explicitly unlabeled (v=0).
    if (!placing.draft.some(k => k.v > 0)) {
      cancelPlacing();
      return false;
    }
    const cls = project.classes[selectedClassIdx];
    if (!cls) {
      cancelPlacing();
      return false;
    }
    const keypoints = placing.draft.slice();
    while (keypoints.length < project.keypoints.length) keypoints.push({ x: 0, y: 0, v: 0 });
    boxes.push({ class_id: cls.id, ...kpsBBox(keypoints), corners: null, keypoints, polygon: null });
    selectedBoxIdx = boxes.length - 1;
    updateBoxCount();
  } else {
    const box = boxes[placing.boxIdx];
    if (!box) {
      cancelPlacing();
      return false;
    }
    completePoseBoxKeypoints(box);
  }
  placing = null;
  placingVis = 2;
  renderKpPanel();
  redraw();
  autoSave();
  return true;
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
      boxes.push({ class_id: cls.id, ...polyBBox(polyDraft), corners: null, keypoints: null, polygon: polyDraft });
      selectedBoxIdx = boxes.length - 1;
    }
  }
  polyDraft = null;
  draftCursor = null;
  updateBoxCount();
  redraw();
  autoSave();
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
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
    e.preventDefault();
    navigate(e.key === 'ArrowLeft' ? -1 : 1);
    return;
  }
  if (switchingImage || readOnly) return;
  const n = parseInt(e.key);
  if (n >= 1 && n <= Math.min(project.classes.length, 8)) {
    selectedClassIdx = n - 1;
    renderClasses();
    setClassPicker(false);
    return;
  }
  if ((e.key === 'b' || e.key === 'B') && !isSeg()) {
    setBrush(false);
    return;
  }
  if (e.key === 'Delete' || e.key === 'Backspace') {
    if (placing) { cancelPlacing(); return; }
    if (polyDraft) { cancelDraft(); return; }
    if (selectedBoxIdx >= 0 && selectedBoxIdx < boxes.length) {
      removeBoxAt(selectedBoxIdx);
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
    if (!shortcutCard.classList.contains('hidden')) { setShortcutCard(false); return; }
    if (!classPickerCard.classList.contains('hidden')) { setClassPicker(false); return; }
    if (placing) { cancelPlacing(); return; }
    if (polyDraft) { cancelDraft(); return; }
    selectedBoxIdx = -1; drawing = false; redraw();
  }
}

function updateBoxCount() { boxCount.textContent = String(boxes.length); }

function enqueueSave(auto = false) {
  // Annotation writes replace the full set, so serialize them to prevent a
  // fast sequence of clicks/erasures from letting an older request win.
  const job = saveQueue.catch(() => {}).then(() => persistAnnotations(auto));
  saveQueue = job;
  return job;
}

function autoSave() { return enqueueSave(true); }

async function persistAnnotations(auto = false) {
  if (readOnly) return;
  if (!auto) { hideErr(errMsg); okMsg.classList.add('hidden'); }
  if (placing) {
    if (!auto) showErr(errMsg, 'Finish or cancel the current keypoint placement first (Esc).');
    return;
  }
  if (polyDraft) {
    if (!auto) showErr(errMsg, 'Finish or cancel the current polygon first (Enter to close, Esc to cancel).');
    return;
  }
  try {
    // Make every pose entry valid before sending the atomic replacement. This
    // also covers multiple brush requests completing out of order.
    if (isPose()) boxes.forEach(completePoseBoxKeypoints);
    const payload = boxes.map(({ corners, ...box }) => box);
    await API.put(`/api/images/${imageId}/annotations`, payload);
    annotationCache.set(imageId, JSON.parse(JSON.stringify(payload)));
    okMsg.textContent = auto ? 'Auto-saved.' : `Saved ${boxes.length} instance(s).`;
    okMsg.classList.remove('hidden');
    if (auto) setTimeout(() => okMsg.classList.add('hidden'), 1200);
  } catch (err) { showErr(errMsg, err.detail || 'Save failed'); }
}

function save() { return enqueueSave(false); }

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
    await flushPendingOperations();
    await API.del(`/api/images/${imageId}/annotations`);
    annotationCache.set(imageId, []);
    okMsg.textContent = 'Cleared.';
    okMsg.classList.remove('hidden');
  } catch (err) { showErr(errMsg, err.detail || 'Clear failed'); }
}

async function releaseClaim() {
  try {
    await flushPendingOperations();
    await API.post(`/api/projects/${projectId}/images/${imageId}/release`);
    window.location.href = appPath(`/project.html?id=${projectId}`);
  } catch (err) { showErr(errMsg, err.detail || 'Release failed'); }
}

async function navigate(dir) {
  if (switchingImage) return;
  const images = imageList || await API.get(`/api/projects/${projectId}/images`);
  const idx = images.findIndex(i => i.id === imageId);
  const next = images[idx + dir];
  if (!next) return;
  setSwitchingImage(true);
  try {
    // Finish pending brush work before deciding how to leave an unfinished
    // keypoint draft. This avoids losing a brush-created pose annotation.
    await flushPendingOperations();
    if (placing) finishPlacingForNavigation();
    if (polyDraft) cancelDraft();
    await flushPendingOperations();
    if (!await activateImage(next)) return;
    history.replaceState(null, '', appPath(`/annotate.html?project=${projectId}&image=${next.id}`));
    prefetchAdjacentImages();
  } catch (err) {
    showErr(errMsg, err.detail || 'Unable to load the next image');
  } finally {
    setSwitchingImage(false);
  }
}

async function leavePage(url) {
  if (leavingPage) return;
  leavingPage = true;
  try {
    if (!readOnly) {
      await flushPendingOperations();
      const finishedPlacement = placing ? finishPlacingForNavigation() : false;
      if (polyDraft) cancelDraft();
      // Persist the complete current state even when the last action did not
      // itself trigger an auto-save (for example, an empty-label review).
      if (!finishedPlacement) autoSave();
      await flushPendingOperations();
    }
  } finally {
    window.location.href = url;
  }
}

init();
