/*
 * Astrophoto Plate-Solve Preprocessor — Web Worker
 * Port of astro_preprocess.py
 *
 * Receives RGBA pixel data + parameters, returns a single-channel
 * processed image rendered into RGB for download/display.
 */

const isWorker = typeof self !== 'undefined' && typeof self.postMessage === 'function';
const log = (msg) => { if (isWorker) self.postMessage({ type: 'log', msg }); };
const progress = (stage, pct) => { if (isWorker) self.postMessage({ type: 'progress', stage, pct }); };

if (isWorker) {
  self.onmessage = async (e) => {
    const { rgba, width, height, params } = e.data;
    try {
      const result = run(rgba, width, height, params);
      self.postMessage(
        { type: 'done', rgba: result.rgba, width: result.width, height: result.height },
        [result.rgba.buffer]
      );
    } catch (err) {
      self.postMessage({ type: 'error', msg: err.message + '\n' + err.stack });
    }
  };
}

// Node CommonJS hook for headless tests (harmless in browser)
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { run };
}

function run(rgba, w, h, p) {
  log(`[load]   ${w}×${h} px input`);

  // Split into R, G, B float32 arrays. Drop alpha.
  let r = new Float32Array(w * h);
  let g = new Float32Array(w * h);
  let b = new Float32Array(w * h);
  for (let i = 0, j = 0; i < rgba.length; i += 4, j++) {
    r[j] = rgba[i];
    g[j] = rgba[i + 1];
    b[j] = rgba[i + 2];
  }

  // ── Crop to circular eyepiece field (optional, default on) ───────────
  // When disabled, fieldRadius stays 0 and the downstream blob-level halo
  // filter becomes a no-op — the median filter sees no synthetic exterior
  // discontinuity, so no halo is generated either.
  let fieldRadius = 0;
  let fieldCx     = w / 2;
  let fieldCy     = h / 2;
  if (p.crop) {
    progress('crop', 0);
    const cropped = autoCropToField(r, g, b, w, h);
    r = cropped.r; g = cropped.g; b = cropped.b;
    w = cropped.w; h = cropped.h;
    fieldRadius = cropped.radius;
    fieldCx     = cropped.cx;
    fieldCy     = cropped.cy;
    log(`[crop]   ${cropped.origW}×${cropped.origH} → ${w}×${h} px (radius ${fieldRadius}px)`);
  } else {
    log(`[crop]   skipped — circular crop disabled, processing full frame`);
  }

  // ── Compute luminance ────────────────────────────────────────────────
  const lum = new Float32Array(w * h);
  let lumSum = 0;
  for (let i = 0; i < w * h; i++) {
    lum[i] = 0.299 * r[i] + 0.587 * g[i] + 0.114 * b[i];
    lumSum += lum[i];
  }
  const lumMean = lumSum / (w * h);
  log(`[bg]     Mean luminance: ${lumMean.toFixed(1)} (>30 suggests bright sky, raise bg-kernel)`);

  // ── Background subtraction (median filter) ───────────────────────────
  progress('median', 0);
  log(`[bg]     Median filter, kernel=${p.bgKernel}…`);
  const bg = medianFilter2D(lum, w, h, (p.bgKernel - 1) >> 1);
  const cleaned = new Float32Array(w * h);
  for (let i = 0; i < w * h; i++) {
    cleaned[i] = Math.max(0, Math.min(255, lum[i] - bg[i]));
  }

  // Make sure any pixel that might have leaked outside the field circle
  // during the median filter is zeroed (the median window straddling the
  // exterior would have produced a non-zero cleaned value otherwise).
  // The actual halo *inside* the rim is rejected at the blob level in
  // extractStars — see "boundary halo" filter there.
  if (fieldRadius > 0) {
    const rField2 = fieldRadius * fieldRadius;
    for (let y = 0; y < h; y++) {
      const dy = y - fieldCy;
      const dy2 = dy * dy;
      for (let x = 0; x < w; x++) {
        const dx = x - fieldCx;
        if (dx * dx + dy2 > rField2) cleaned[y * w + x] = 0;
      }
    }
  }

  // ── Detect & filter blobs ────────────────────────────────────────────
  progress('detect', 0);
  const mask = extractStars(cleaned, r, g, b, w, h, p, fieldCx, fieldCy, fieldRadius);

  // ── Render output ────────────────────────────────────────────────────
  progress('render', 0);
  const out = renderOutput(cleaned, mask, w, h, p);

  // Estimate astrometry detections (blobs > 40 in output)
  const binary = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) binary[i] = out[i] > 40 ? 1 : 0;
  const { count: estDetections } = labelComponents(binary, w, h);
  log(`[output] Estimated astrometry detections: ~${estDetections}`);

  // Pack into RGBA for transfer
  const outRgba = new Uint8ClampedArray(w * h * 4);
  for (let i = 0, j = 0; i < out.length; i++, j += 4) {
    outRgba[j] = out[i];
    outRgba[j + 1] = out[i];
    outRgba[j + 2] = out[i];
    outRgba[j + 3] = 255;
  }
  return { rgba: outRgba, width: w, height: h };
}

/* ─────────────────────────────────────────────────────────────────────
 * Auto-crop to circular eyepiece field
 *   1. Threshold luminance > 3.0 to find the bright field
 *   2. Find bbox of bright pixels
 *   3. Build inscribed circle, shrink by 3% to kill boundary artifacts
 *   4. Zero outside circle, crop to bbox of circle
 * ──────────────────────────────────────────────────────────────────── */
function autoCropToField(r, g, b, w, h) {
  const fieldThreshold = 3.0;
  const shrink = 0.97;

  let rmin = h, rmax = -1, cmin = w, cmax = -1;
  for (let y = 0; y < h; y++) {
    let rowHas = false;
    const rowOff = y * w;
    for (let x = 0; x < w; x++) {
      const i = rowOff + x;
      const lum = 0.299 * r[i] + 0.587 * g[i] + 0.114 * b[i];
      if (lum > fieldThreshold) {
        rowHas = true;
        if (x < cmin) cmin = x;
        if (x > cmax) cmax = x;
      }
    }
    if (rowHas) {
      if (y < rmin) rmin = y;
      if (y > rmax) rmax = y;
    }
  }

  if (rmax < 0) {
    // No bright pixels — return as-is
    log('[crop]   WARNING: no bright field detected, skipping crop');
    return { r, g, b, w, h, origW: w, origH: h, radius: 0, cx: w / 2, cy: h / 2 };
  }

  const rc = (rmin + rmax) >> 1;
  const cc = (cmin + cmax) >> 1;
  const radius = Math.floor(Math.min(rmax - rmin, cmax - cmin) / 2 * shrink);

  const r0 = Math.max(0, rc - radius);
  const r1 = Math.min(h, rc + radius);
  const c0 = Math.max(0, cc - radius);
  const c1 = Math.min(w, cc + radius);
  const newH = r1 - r0;
  const newW = c1 - c0;

  const nr = new Float32Array(newW * newH);
  const ng = new Float32Array(newW * newH);
  const nb = new Float32Array(newW * newH);

  const r2 = radius * radius;
  for (let y = 0; y < newH; y++) {
    for (let x = 0; x < newW; x++) {
      const srcY = r0 + y;
      const srcX = c0 + x;
      const dy = srcY - rc;
      const dx = srcX - cc;
      const inside = dx * dx + dy * dy <= r2;
      const dst = y * newW + x;
      if (inside) {
        const src = srcY * w + srcX;
        nr[dst] = r[src];
        ng[dst] = g[src];
        nb[dst] = b[src];
      }
    }
  }

  // Centre in cropped coordinates (r0, c0 may differ from rc-radius if
  // the inscribed circle extends past the original image edges).
  const newCy = rc - r0;
  const newCx = cc - c0;
  return { r: nr, g: ng, b: nb, w: newW, h: newH, origW: w, origH: h, radius, cx: newCx, cy: newCy };
}

/* ─────────────────────────────────────────────────────────────────────
 * 2D median filter (sliding histogram — Huang-style, per-row)
 * For each row, initialize a 256-bin histogram for the K×K window at
 * column 0, then slide right one column at a time, removing the
 * left column and adding the right column. Median position is tracked
 * incrementally.
 * ──────────────────────────────────────────────────────────────────── */
function medianFilter2D(src, w, h, radius) {
  const k = radius * 2 + 1;
  const target = (k * k) >> 1; // index of median element (0-based)

  // Quantize input to uint8 for histogram bins
  const data = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) {
    const v = src[i] | 0;
    data[i] = v < 0 ? 0 : v > 255 ? 255 : v;
  }

  const out = new Float32Array(w * h);
  const hist = new Uint32Array(256);

  // Reflect helper (clamp at edges — we don't need full reflect for sky bg)
  const clampX = (x) => x < 0 ? 0 : x >= w ? w - 1 : x;
  const clampY = (y) => y < 0 ? 0 : y >= h ? h - 1 : y;

  let lastProgress = 0;

  for (let y = 0; y < h; y++) {
    // Reset histogram
    hist.fill(0);

    // Initialize histogram with the K×K window centered at (0, y)
    for (let dy = -radius; dy <= radius; dy++) {
      const yy = clampY(y + dy);
      const yyOff = yy * w;
      for (let dx = -radius; dx <= radius; dx++) {
        hist[data[yyOff + clampX(dx)]]++;
      }
    }

    // Find initial median
    let median = 0;
    let lt = 0; // count strictly less than median
    let cum = 0;
    for (let i = 0; i < 256; i++) {
      cum += hist[i];
      if (cum > target) {
        median = i;
        lt = cum - hist[i];
        break;
      }
    }

    out[y * w + 0] = median;

    // Slide right
    for (let x = 1; x < w; x++) {
      const oldX = clampX(x - radius - 1);
      const newX = clampX(x + radius);

      for (let dy = -radius; dy <= radius; dy++) {
        const yy = clampY(y + dy);
        const yyOff = yy * w;
        const oldV = data[yyOff + oldX];
        const newV = data[yyOff + newX];

        hist[oldV]--;
        if (oldV < median) lt--;

        hist[newV]++;
        if (newV < median) lt++;
      }

      // Adjust median: we want lt <= target < lt + hist[median]
      while (lt > target) {
        median--;
        lt -= hist[median];
      }
      while (lt + hist[median] <= target) {
        lt += hist[median];
        median++;
      }

      out[y * w + x] = median;
    }

    // Progress every ~1% of rows
    const pct = Math.floor((y + 1) / h * 100);
    if (pct >= lastProgress + 2) {
      lastProgress = pct;
      progress('median', pct);
    }
  }

  return out;
}

/* ─────────────────────────────────────────────────────────────────────
 * Connected components labeling (4-connectivity, scipy.ndimage.label
 * default behaviour)
 * ──────────────────────────────────────────────────────────────────── */
function labelComponents(binary, w, h) {
  const labels = new Int32Array(w * h);
  // parent[0] reserved for background. Grow as needed.
  let cap = 1024;
  let parent = new Int32Array(cap);
  let nextLabel = 1;

  const ensure = (n) => {
    if (n < cap) return;
    while (n >= cap) cap *= 2;
    const np = new Int32Array(cap);
    np.set(parent);
    parent = np;
  };

  const find = (x) => {
    while (parent[x] !== x) {
      parent[x] = parent[parent[x]];
      x = parent[x];
    }
    return x;
  };

  const union = (a, b) => {
    const ra = find(a);
    const rb = find(b);
    if (ra === rb) return;
    if (ra < rb) parent[rb] = ra;
    else parent[ra] = rb;
  };

  for (let y = 0; y < h; y++) {
    const off = y * w;
    for (let x = 0; x < w; x++) {
      const idx = off + x;
      if (!binary[idx]) continue;

      const left = x > 0 ? labels[idx - 1] : 0;
      const top = y > 0 ? labels[idx - w] : 0;

      if (left && top) {
        labels[idx] = left < top ? left : top;
        if (left !== top) union(left, top);
      } else if (left) {
        labels[idx] = left;
      } else if (top) {
        labels[idx] = top;
      } else {
        ensure(nextLabel);
        parent[nextLabel] = nextLabel;
        labels[idx] = nextLabel;
        nextLabel++;
      }
    }
  }

  // Compact roots into 1..count
  const remap = new Int32Array(nextLabel);
  let count = 0;
  for (let i = 1; i < nextLabel; i++) {
    if (parent[i] === i) {
      count++;
      remap[i] = count;
    }
  }
  for (let i = 1; i < nextLabel; i++) {
    if (parent[i] !== i) remap[i] = remap[find(i)];
  }
  for (let i = 0; i < w * h; i++) {
    if (labels[i]) labels[i] = remap[labels[i]];
  }
  return { labels, count };
}

/* ─────────────────────────────────────────────────────────────────────
 * Detect star blobs and apply size / compactness / colour filters.
 * Returns a Uint8Array mask (1 = accepted star pixel).
 * ──────────────────────────────────────────────────────────────────── */
function extractStars(cleaned, r, g, b, w, h, p, fieldCx, fieldCy, fieldRadius) {
  const N = w * h;
  const binary = new Uint8Array(N);
  for (let i = 0; i < N; i++) binary[i] = cleaned[i] > p.threshold ? 1 : 0;

  const { labels, count } = labelComponents(binary, w, h);
  log(`[detect] Blobs above threshold: ${count}`);

  const sizes = new Int32Array(count + 1);
  const peaks = new Float32Array(count + 1);
  const sumR  = new Float32Array(count + 1);
  const sumG  = new Float32Array(count + 1);
  const sumB  = new Float32Array(count + 1);
  const sumX  = new Float32Array(count + 1);
  const sumY  = new Float32Array(count + 1);

  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = y * w + x;
      const lbl = labels[i];
      if (!lbl) continue;
      sizes[lbl]++;
      if (cleaned[i] > peaks[lbl]) peaks[lbl] = cleaned[i];
      sumR[lbl] += r[i];
      sumG[lbl] += g[i];
      sumB[lbl] += b[i];
      sumX[lbl] += x;
      sumY[lbl] += y;
    }
  }

  // Boundary halo (only meaningful when crop is on, i.e. fieldRadius > 0):
  // blobs whose centroid lies in the median-bias annulus and whose peak is
  // too low to be a real star are halo. The halo bias can reach ~50% of
  // local field brightness when the median window is half outside the
  // circle. Real rim stars in our samples sit at peaks ≥ 80 DN, so a
  // 5×-threshold cutoff catches the worst halo while leaving real bright
  // stars untouched.
  const haloMargin = Math.ceil(p.bgKernel / 2);
  const safeRadius = fieldRadius > 0 ? Math.max(1, fieldRadius - haloMargin) : 0;
  const safeR2     = safeRadius * safeRadius;
  const haloMax    = 5 * p.threshold;

  // Accept/reject per blob
  const accept = new Uint8Array(count + 1);
  let nSize = 0, nCompact = 0, nColor = 0, nHalo = 0, nKept = 0;
  for (let lbl = 1; lbl <= count; lbl++) {
    const sz = sizes[lbl];
    if (sz < p.minBlob || sz > p.maxBlob) { nSize++; continue; }

    const compactness = peaks[lbl] / sz;
    if (compactness > p.maxCompact) { nCompact++; continue; }

    const mr = sumR[lbl] / sz;
    const mg = sumG[lbl] / sz;
    const mb = sumB[lbl] / sz;
    const meanRgb = (mr + mg + mb) / 3;
    if (meanRgb < 1.0) { nColor++; continue; }
    const variance = ((mr - meanRgb) ** 2 + (mg - meanRgb) ** 2 + (mb - meanRgb) ** 2) / 3;
    const cv = Math.sqrt(variance) / meanRgb;
    if (cv > p.colorCV) { nColor++; continue; }

    if (fieldRadius > 0) {
      const dx = sumX[lbl] / sz - fieldCx;
      const dy = sumY[lbl] / sz - fieldCy;
      if (dx * dx + dy * dy > safeR2 && peaks[lbl] < haloMax) {
        nHalo++;
        continue;
      }
    }

    accept[lbl] = 1;
    nKept++;
  }

  log(`[filter] Rejected by size        (<${p.minBlob} or >${p.maxBlob} px): ${nSize}`);
  log(`[filter] Rejected by compactness (peak/size > ${p.maxCompact}):    ${nCompact}`);
  log(`[filter] Rejected by colour      (CV > ${p.colorCV}):              ${nColor}`);
  log(`[filter] Rejected by halo        (rim, peak < ${haloMax} DN):      ${nHalo}`);
  log(`[result] Stars accepted: ${nKept}`);
  if (nKept < 10) log(`[warn]   Only ${nKept} stars — try lowering threshold or min-blob.`);
  if (nKept > 200) log(`[warn]   ${nKept} stars — may still be noisy, try raising threshold or min-blob.`);

  // Build pixel mask from accepted labels
  const mask = new Uint8Array(N);
  for (let i = 0; i < N; i++) {
    const lbl = labels[i];
    if (lbl && accept[lbl]) mask[i] = 1;
  }
  return mask;
}

/* ─────────────────────────────────────────────────────────────────────
 * Render output: mask × cleaned, gaussian glow, normalize, stretch, lift
 * ──────────────────────────────────────────────────────────────────── */
function renderOutput(cleaned, mask, w, h, p) {
  const N = w * h;
  let star = new Float32Array(N);
  for (let i = 0; i < N; i++) star[i] = mask[i] ? cleaned[i] : 0;

  if (p.glowSigma > 0) star = gaussianFilter1Dx2(star, w, h, p.glowSigma);

  // Normalize to 255 then apply stretch
  let maxV = 0;
  for (let i = 0; i < N; i++) if (star[i] > maxV) maxV = star[i];

  const out = new Float32Array(N);
  if (maxV > 0) {
    const scale = (255 / maxV) * p.stretch;
    for (let i = 0; i < N; i++) {
      const v = star[i] * scale + p.bgLift;
      out[i] = v < 0 ? 0 : v > 255 ? 255 : v;
    }
  } else {
    for (let i = 0; i < N; i++) out[i] = p.bgLift;
  }

  return out;
}

/* Separable Gaussian, clamp boundary, truncate=4*sigma to match scipy. */
function gaussianFilter1Dx2(src, w, h, sigma) {
  if (sigma <= 0) return src;
  const radius = Math.max(1, Math.ceil(4 * sigma));
  const size = 2 * radius + 1;
  const kernel = new Float32Array(size);
  const denom = 2 * sigma * sigma;
  let ksum = 0;
  for (let i = 0; i < size; i++) {
    const x = i - radius;
    kernel[i] = Math.exp(-x * x / denom);
    ksum += kernel[i];
  }
  for (let i = 0; i < size; i++) kernel[i] /= ksum;

  const tmp = new Float32Array(w * h);
  // Horizontal
  for (let y = 0; y < h; y++) {
    const off = y * w;
    for (let x = 0; x < w; x++) {
      let v = 0;
      for (let k = -radius; k <= radius; k++) {
        let xx = x + k;
        if (xx < 0) xx = 0;
        else if (xx >= w) xx = w - 1;
        v += src[off + xx] * kernel[k + radius];
      }
      tmp[off + x] = v;
    }
  }
  const out = new Float32Array(w * h);
  // Vertical
  for (let x = 0; x < w; x++) {
    for (let y = 0; y < h; y++) {
      let v = 0;
      for (let k = -radius; k <= radius; k++) {
        let yy = y + k;
        if (yy < 0) yy = 0;
        else if (yy >= h) yy = h - 1;
        v += tmp[yy * w + x] * kernel[k + radius];
      }
      out[y * w + x] = v;
    }
  }
  return out;
}
