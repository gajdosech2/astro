/*
 * Astrophoto Plate-Solve Preprocessor — main thread / UI
 */

const $ = (sel) => document.querySelector(sel);

const DEFAULTS = {
  downsample: 2,
  bgKernel:   15,
  threshold:  10,
  // minBlob: handled separately (auto-derived from downsample by default)
  maxBlob:    2000,
  maxCompact: 3.0,
  colorCV:    0.5,
  glowSigma:  1.5,
  stretch:    3.0,
  bgLift:     12,
};

// Slider definitions: id (also param key), bounds, step, decimal display.
const SLIDERS = [
  { id: 'downsample', min: 1,   max: 4,    step: 1,    digits: 0 },
  { id: 'bgKernel',   min: 3,   max: 51,   step: 2,    digits: 0 },
  { id: 'threshold',  min: 1,   max: 50,   step: 1,    digits: 0 },
  { id: 'minBlob',    min: 1,   max: 100,  step: 1,    digits: 0 },
  { id: 'maxBlob',    min: 50,  max: 5000, step: 50,   digits: 0 },
  { id: 'maxCompact', min: 0.5, max: 10,   step: 0.1,  digits: 1 },
  { id: 'colorCV',    min: 0,   max: 1,    step: 0.05, digits: 2 },
  { id: 'glowSigma',  min: 0,   max: 5,    step: 0.1,  digits: 1 },
  { id: 'stretch',    min: 0.5, max: 10,   step: 0.1,  digits: 1 },
  { id: 'bgLift',     min: 0,   max: 50,   step: 1,    digits: 0 },
];

const state = {
  bitmap:    null,    // ImageBitmap of uploaded photo (with EXIF orientation applied)
  worker:    null,
  outBlob:   null,    // last processed PNG blob, for download
  outName:   'platesolve.png',
};

/* ─────── Sliders: range ⇄ number input sync ──────── */

function fmtVal(id, v) {
  const s = SLIDERS.find((x) => x.id === id);
  return s.digits === 0 ? String(Math.round(v)) : v.toFixed(s.digits);
}

function setSliderValue(id, v, opts = {}) {
  const range = $(`#${id}`);
  const num   = $(`#${id}-num`);
  const s = SLIDERS.find((x) => x.id === id);
  let cv = parseFloat(v);
  if (Number.isNaN(cv)) cv = parseFloat(range.value);
  cv = Math.max(s.min, Math.min(s.max, cv));
  range.value = cv;
  if (!opts.fromNumber) num.value = fmtVal(id, cv);
  // minBlob auto flag: clear unless we set it via "auto"
  if (id === 'minBlob') {
    const chip = num.closest('.slider-chip');
    if (opts.auto) chip.classList.add('auto'); else chip.classList.remove('auto');
  }
  return cv;
}

function setupSliders() {
  for (const s of SLIDERS) {
    const range = $(`#${s.id}`);
    const num   = $(`#${s.id}-num`);

    range.min = s.min; range.max = s.max; range.step = s.step;
    num.min   = s.min; num.max   = s.max; num.step   = s.step;

    range.addEventListener('input', () => {
      setSliderValue(s.id, range.value);
      if (s.id === 'minBlob') $(`#minBlob-num`).closest('.slider-chip').classList.remove('auto');
      if (s.id === 'downsample') refreshAutoMinBlob();
    });
    num.addEventListener('input', () => {
      setSliderValue(s.id, num.value, { fromNumber: true });
      if (s.id === 'minBlob') $(`#minBlob-num`).closest('.slider-chip').classList.remove('auto');
      if (s.id === 'downsample') refreshAutoMinBlob();
    });
    num.addEventListener('blur', () => {
      // On blur, pretty-format the value (e.g. "0.5" → "0.50" for colorCV)
      const v = parseFloat(num.value);
      if (!Number.isNaN(v)) num.value = fmtVal(s.id, Math.max(s.min, Math.min(s.max, v)));
    });
  }
}

function isMinBlobAuto() {
  return $('#minBlob-num').closest('.slider-chip').classList.contains('auto');
}

function refreshAutoMinBlob() {
  if (!isMinBlobAuto()) return;
  const ds = parseInt($('#downsample').value, 10);
  setSliderValue('minBlob', Math.max(2, Math.floor(15 / ds)), { auto: true });
}

function readParams() {
  return {
    downsample: parseInt($('#downsample').value, 10),
    bgKernel:   parseInt($('#bgKernel').value, 10),
    threshold:  parseFloat($('#threshold').value),
    minBlob:    parseFloat($('#minBlob').value),
    maxBlob:    parseFloat($('#maxBlob').value),
    maxCompact: parseFloat($('#maxCompact').value),
    colorCV:    parseFloat($('#colorCV').value),
    glowSigma:  parseFloat($('#glowSigma').value),
    stretch:    parseFloat($('#stretch').value),
    bgLift:     parseFloat($('#bgLift').value),
  };
}

function applyDefaults() {
  for (const [k, v] of Object.entries(DEFAULTS)) setSliderValue(k, v);
  // minBlob: auto, derived from downsample
  setSliderValue('minBlob', Math.max(2, Math.floor(15 / DEFAULTS.downsample)), { auto: true });
}

/* ─────── Image loading ─────────────────────────────── */

async function loadImageFile(file) {
  $('#log').textContent = '';
  state.outBlob = null;
  $('#download').disabled = true;
  state.outName = file.name.replace(/\.[^.]+$/, '') + '_platesolve.png';

  appendLog(`Loading ${file.name} (${(file.size / 1024 / 1024).toFixed(1)} MB)…`);

  let bitmap;
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' });
  } catch {
    bitmap = await createImageBitmap(file);
  }
  state.bitmap = bitmap;
  appendLog(`Loaded: ${bitmap.width}×${bitmap.height}`);

  drawToCanvas($('#preview-original'), bitmap, 1024);
  $('#process').disabled = false;
}

function drawToCanvas(canvas, bitmap, maxDim) {
  const scale = Math.min(1, maxDim / Math.max(bitmap.width, bitmap.height));
  const cw = Math.max(1, Math.round(bitmap.width  * scale));
  const ch = Math.max(1, Math.round(bitmap.height * scale));
  canvas.width = cw;
  canvas.height = ch;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(bitmap, 0, 0, cw, ch);
}

/* ─────── Processing ────────────────────────────────── */

async function process() {
  if (!state.bitmap) return;
  const params = readParams();

  const ds = params.downsample;
  const w = Math.max(1, Math.floor(state.bitmap.width  / ds));
  const h = Math.max(1, Math.floor(state.bitmap.height / ds));

  const off = document.createElement('canvas');
  off.width = w;
  off.height = h;
  const ctx = off.getContext('2d', { willReadFrequently: true });
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(state.bitmap, 0, 0, w, h);

  let imgData;
  try {
    imgData = ctx.getImageData(0, 0, w, h);
  } catch (e) {
    appendLog(`ERROR reading image data: ${e.message}`);
    openLog();
    return;
  }

  appendLog('');
  appendLog('═══════════════════════════════════════════');
  appendLog(`Processing at ${w}×${h} (downsample ${ds}×)`);
  appendLog('═══════════════════════════════════════════');

  $('#process').disabled = true;
  $('#process').textContent = 'Processing…';
  $('#progress-bar').style.width = '0%';
  $('#progress-row').classList.add('active');
  $('#progress-label').textContent = 'starting';

  if (state.worker) state.worker.terminate();
  state.worker = new Worker(new URL('./worker.js', import.meta.url));

  state.worker.onmessage = (e) => {
    const m = e.data;
    if (m.type === 'log') {
      appendLog(m.msg);
    } else if (m.type === 'progress') {
      $('#progress-label').textContent = `${m.stage} — ${m.pct}%`;
      $('#progress-bar').style.width = `${m.pct}%`;
    } else if (m.type === 'done') {
      onProcessDone(m.rgba, m.width, m.height);
    } else if (m.type === 'error') {
      appendLog(`ERROR: ${m.msg}`);
      openLog();
      $('#process').disabled = false;
      $('#process').textContent = 'Process';
      $('#progress-row').classList.remove('active');
    }
  };

  const rgbaCopy = new Uint8ClampedArray(imgData.data);
  state.worker.postMessage(
    { rgba: rgbaCopy, width: w, height: h, params },
    [rgbaCopy.buffer]
  );
}

async function onProcessDone(rgba, w, h) {
  const canvas = document.createElement('canvas');
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext('2d');
  const img = new ImageData(rgba, w, h);
  ctx.putImageData(img, 0, 0);

  const previewCanvas = $('#preview-output');
  const scale = Math.min(1, 1024 / Math.max(w, h));
  previewCanvas.width = Math.round(w * scale);
  previewCanvas.height = Math.round(h * scale);
  previewCanvas.getContext('2d').drawImage(canvas, 0, 0, previewCanvas.width, previewCanvas.height);

  canvas.toBlob((blob) => {
    state.outBlob = blob;
    $('#download').disabled = false;
    $('#process').disabled = false;
    $('#process').textContent = 'Process';
    $('#progress-row').classList.remove('active');
    appendLog(`PNG ready (${(blob.size / 1024).toFixed(0)} KB) — tap Download.`);
  }, 'image/png');
}

function downloadResult() {
  if (!state.outBlob) return;
  const url = URL.createObjectURL(state.outBlob);
  const a = document.createElement('a');
  a.href = url;
  a.download = state.outName;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function appendLog(msg) {
  const el = $('#log');
  el.textContent += msg + '\n';
  el.scrollTop = el.scrollHeight;
}

function openLog() {
  $('#log-details').open = true;
}

/* ─────── Wiring ────────────────────────────────────── */

function init() {
  setupSliders();
  applyDefaults();

  $('#file').addEventListener('change', (e) => {
    const f = e.target.files[0];
    if (f) loadImageFile(f).catch((err) => {
      appendLog(`ERROR loading file: ${err.message}`);
      openLog();
    });
  });

  $('#sample').addEventListener('click', async () => {
    try {
      const res = await fetch('samples/input.jpeg');
      if (!res.ok) throw new Error(`Sample fetch ${res.status}: ${res.statusText}`);
      const blob = await res.blob();
      await loadImageFile(new File([blob], 'sample.jpeg', { type: 'image/jpeg' }));
    } catch (err) {
      appendLog(`ERROR loading sample: ${err.message}`);
      appendLog(`Tip: this app needs a real http(s):// origin — file:// won't load samples or the worker.`);
      openLog();
    }
  });

  $('#process').addEventListener('click', process);
  $('#download').addEventListener('click', downloadResult);
  $('#reset').addEventListener('click', applyDefaults);
  $('#choose').addEventListener('click', () => $('#file').click());
}

init();
