# Astro Platesolve

A browser-based preprocessor for afocal phone astrophotos, used as a front end for [nova.astrometry.net](https://nova.astrometry.net) plate solving. Tuned for iPhone 12 Pro shots through a Dobsonian eyepiece — the workflow is to point, snap, then drop the JPEG into this tool to extract a clean star field for the astrometry source extractor.

All processing runs **entirely client-side** in a Web Worker. There is no backend; photos never leave the device. The repository is a static site — push to GitHub, hook up Vercel, and it deploys with zero configuration.

A reference implementation in Python (`astro_preprocess.py`) lives alongside the web app for batch processing on a workstation. Both implementations produce equivalent results.

---

## Contents

- [What it does](#what-it-does)
- [Algorithm in detail](#algorithm-in-detail)
- [Parameters](#parameters)
- [Presets](#presets)
- [App architecture](#app-architecture)
- [Run locally for development](#run-locally-for-development)
- [Deploy to Vercel](#deploy-to-vercel)
- [Python reference script](#python-reference-script)
- [Repository layout](#repository-layout)
- [Tuning tips](#tuning-tips)

---

## What it does

A photo of the sky taken through a phone held against a telescope eyepiece looks like a circular bright field with a handful of dots in it, plus a lot of high-ISO speckle noise. Astrometry's source extractor will gladly pick up the noise as "stars" and fail to plate-solve, or solve to nonsense.

This tool takes the JPEG and produces a clean greyscale PNG containing only real stars rendered as smooth Gaussian blobs on a near-black background:

- **Input:** raw afocal phone photo (any orientation, JPEG/PNG/HEIC).
- **Output:** square PNG, eyepiece field cropped to its inscribed circle, only confirmed stars rendered.

The output drops directly into the Nova Astrometry upload form. On a typical iPhone 12 Pro photo at the default preset, you'll get 30–50 detected stars — plenty for plate solving.

## Algorithm in detail

The pipeline is identical between the JS worker (`src/worker.js`) and the Python script (`astro_preprocess.py`). It runs in seven stages:

### 1. Load & EXIF orientation

Decode the input as RGB, honouring the EXIF orientation tag so phone photos taken in portrait don't end up sideways.

- **JS:** `createImageBitmap(file, { imageOrientation: 'from-image' })` (Safari 14+)
- **Python:** `PIL.ImageOps.exif_transpose`

### 2. Optional downsample

Resize the image by an integer factor (1×–4×). Halving each dimension brings the median filter cost down ~4× and is usually visually indistinguishable for plate solving. The default for the Stacked preset is 2× because the median filter is the slow step on large bright-background images.

When downsampling, `min_blob` is auto-halved (with a floor of 2 px) since blob sizes scale with linear resolution.

### 3. Auto-crop to the eyepiece field (optional, default on)

Controlled by the `crop` parameter (`1` = on, `0` = off). When off, this stage is skipped entirely and the rest of the pipeline runs on the full downsampled frame.

The eyepiece produces a bright circular field with dark exterior. When enabled, we:

1. Compute luminance: `0.299·R + 0.587·G + 0.114·B`.
2. Find the bounding box of pixels above a low brightness threshold (3.0).
3. Build the inscribed circle, shrinking the radius by 3 % to drop the field-stop transition pixels.
4. Zero everything outside the circle.
5. Crop to the bounding square of the circle.

The center coordinates and radius are kept and used in stage 5.

If you turn the crop off (slider value `0`, or `--crop 0` on the CLI), the pipeline still produces a usable result on both sample images — the area outside the eyepiece is mostly black, so it adds very little noise to the median estimate. Use this escape hatch when the field circle is partially out of view, when auto-detection misbehaves on a particular image, or when you want to keep the original framing.

### 4. Background subtraction

The eyepiece field has a smooth bright background gradient (sky glow, vignetting). Estimate it with a 2D **median filter** of size `bg_kernel` (default 15 px, 25 px for stacked exposures), then subtract it from the luminance. The median filter is robust to point sources — a single bright star pixel doesn't shift the median of its 225-pixel neighbourhood.

The JS implementation uses **Huang's sliding-histogram median** (256-bin, per-row reset) for O(W·H·K) cost instead of the O(W·H·K²·log K) of a naive sort-per-pixel approach. On a 1500×2000 image with a 15-pixel kernel this completes in ~0.5 s on desktop, ~3 s on iPhone.

### 5. Boundary-halo suppression (the edge fix)

Stage 3 zeroed the area outside the eyepiece circle. The median filter in stage 4 is therefore unbalanced near the rim: its window straddles bright field pixels and zero exterior pixels, pulling the background estimate *down*. After subtraction, a band of width ≈ `bg_kernel/2` just inside the rim (the radius at which the median window stops straddling the boundary) ends up artificially bright and would produce a halo of false detections.

The fix happens at the **blob level**, after connected-component labeling — not by zeroing pixels in advance. We compute each blob's centroid and peak, and reject a blob if **both**:

1. its centroid sits in the boundary annulus (`d > radius − bg_kernel/2` from the field centre), and
2. its peak is below `5 × threshold` (the worst-case halo amplitude — about half the local field brightness).

Real bright rim stars sit far above `5 × threshold` (typically 80–200 DN on input2's `~50 DN` field) and pass through untouched, even though their centroid is in the annulus. Halo arcs are large, dim, and never approach that peak, so they're rejected. This blob-level approach (rather than pre-zeroing pixels in the annulus) is what preserves the top- and bottom-most rim stars on the Stacked sample — pre-zeroing tended to fragment a star whose tail extended into the annulus and then rejected the leftover via `min_blob`.

### 6. Star detection & filtering

Threshold the cleaned image at `threshold` DN (default 10) and run **4-connected component labeling** (scipy `ndimage.label` equivalent — JS uses two-pass union-find). Then evaluate each blob through a four-stage filter pipeline:

| Filter | Reject if | Why |
|---|---|---|
| **Size** | size < `min_blob` or > `max_blob` | iPhone ISO noise clusters are <10 px; real stars ≥15 px. This is the **primary discriminator**. |
| **Compactness** | peak / size > `max_compact` (default 3.0) | Real stars: 0.2–2.2. Noise spikes: 9–44. Catches bright pinprick artifacts that pass size by chance. |
| **Mean brightness** | (R+G+B)/3 < 1.0 | Sanity check, rarely triggers. |
| **Colour CV** | std(R,G,B)/mean(R,G,B) > `color_cv` (default 0.5) | Real stars are neutral white. ISO noise is often strongly coloured (red/green/blue speckles). |

Each blob that passes all four filters contributes its pixels to the accepted-star mask.

### 7. Render

Take the cleaned luminance under the accepted-star mask, apply a small Gaussian blur (`glow_sigma`, default 1.5 px) to give each star a smooth circular profile, normalize the brightest pixel to 255, multiply by `stretch` (default 3.0) to lift faint stars, then add `bg_lift` (default 12 DN) so the background isn't pure black (helps the source extractor pick a sensible threshold).

Save as a single-channel PNG in an RGB container.

## Parameters

All parameters are exposed in the GUI as sliders **plus** an editable numeric field that shows the current value. Tap or drag the slider on mobile, type a precise number on desktop. If you tweak anything by hand, the active preset highlight dims; **Reset** re-applies whichever preset you most recently selected.

| Parameter | Default | Range | What it does |
|---|---|---|---|
| `crop` | 1 | 0 / 1 | Detect the eyepiece field, mask to a circle and crop to the inscribed square. Turn off (`0`) if the circle is partially out of view or auto-detect misbehaves. |
| `downsample` | 1× | 1–4× | Process at 1/N resolution. Higher = faster, lower memory. |
| `bg_kernel` | 15 | 3–51 | Median filter size for background estimation. Use 25 for bright/uneven backgrounds. |
| `threshold` | 10 | 1–50 | DN above local background to enter detection. Keep low; noise rejection is done by size. |
| `min_blob` | auto | 1–100 | Minimum star size in px. Auto-derived from downsample (15 ÷ N). The **AUTO** badge means it tracks downsample; edit the value to take manual control. |
| `max_blob` | 2000 | 50–5000 | Maximum star size. Generous — bright stars can be large. |
| `max_compact` | 3.0 | 0.5–10 | Reject blobs with peak/size above this. Real stars ≤2.2, noise ≥9. |
| `color_cv` | 0.5 | 0–1 | Reject blobs with strong RGB imbalance. 1.0 disables. |
| `glow_sigma` | 1.5 | 0–5 | Gaussian sigma for star profile rendering. 0 disables. |
| `stretch` | 3.0× | 0.5–10 | Brightness multiplier after normalisation. |
| `bg_lift` | 12 | 0–50 | Output background grey level. |

## Presets

Two named presets, mirroring the Python script. Click a chip to apply.

- **Dark** — short exposure, high ISO, dark and roughly uniform background. `downsample 1×, bg_kernel 15`. Default on load.
- **Stacked** — long / stacked exposure, bright and uneven background (sky glow fills much of the field). `downsample 2×, bg_kernel 25`.

The presets only differ in `downsample` and `bg_kernel`; all other defaults — including `crop = 1` and the blob-level halo filter — are shared. Both presets feed the same `auto_crop_to_field` pass when crop is on, and both can be flipped to `crop = 0` to process the full frame. The reason the boundary halo was historically only obvious in the Stacked case is that a bright background creates a stronger contrast against the zeroed exterior, exposing the median-filter halo more clearly. After applying a preset, you can fine-tune any slider; the preset highlight dims to indicate "custom mode". Hit **Reset** to snap back to the last preset.

## App architecture

```
┌────────────────────┐     ┌─────────────────┐
│  index.html (UI)   │     │  src/main.js    │
│  - sliders         │────▶│  - sliders sync │
│  - canvases        │     │  - preset apply │
│  - file picker     │     │  - file load    │
└────────────────────┘     │  - PNG export   │
                           └────────┬────────┘
                                    │  postMessage
                                    │  RGBA + params
                                    ▼
                           ┌─────────────────┐
                           │  src/worker.js  │
                           │  (Web Worker)   │
                           │  - crop         │
                           │  - median       │
                           │  - edge erode   │
                           │  - CCL          │
                           │  - filter blobs │
                           │  - gaussian     │
                           │  - stretch+lift │
                           └─────────────────┘
```

The worker is also CommonJS-loadable (`module.exports = { run }` at the bottom), so the same algorithm code can be exercised from Node for headless testing without bringing up a browser.

**Why a Web Worker?** Median-filter cost on a 12 MP image is several seconds even with the sliding-histogram trick. Running it on the main thread would freeze the UI on iOS Safari. Posting RGBA pixel buffers to a worker keeps the page responsive, and the progress bar gets per-row updates from the median pass.

**Why no build step?** It's three files of vanilla JS. A bundler would be more friction than benefit. It also makes the project trivially deployable to anything that serves static files (Vercel, GitHub Pages, Netlify, S3, a Raspberry Pi).

## Run locally for development

The page must be served over a real `http://` (or `https://`) origin. Opening `index.html` directly via `file://` will not work — Web Workers and `fetch()` (the "Use sample" button) both refuse to operate from `file://`.

The simplest way is Python's built-in HTTP server, which ships with most Linux/macOS systems and is one command on Windows once Python is installed:

```bash
cd astro-platesolve
python3 -m http.server 8000
```

Then open <http://localhost:8000> in your browser. Hot-reloading is not needed — refresh the page after each edit.

If you don't have Python:

```bash
# Node alternative
npx serve

# Or any other static-file server (caddy, nginx, etc.)
```

For mobile testing, find your computer's LAN IP (`ip addr` / `ifconfig`) and visit `http://<your-ip>:8000` from the phone on the same Wi-Fi. The app installs as a PWA via Safari → Share → **Add to Home Screen**, after which it opens fullscreen with the eyepiece icon.

### Headless testing

The `src/worker.js` file is dual-mode. To exercise the pipeline from Node:

```js
const { run } = require('./src/worker.js');
const result = run(rgbaUint8ClampedArray, width, height, {
  bgKernel: 15, threshold: 10, minBlob: 15, maxBlob: 2000,
  maxCompact: 3.0, colorCV: 0.5, glowSigma: 1.5, stretch: 3.0, bgLift: 12,
});
// result.rgba is a Uint8ClampedArray; result.width, result.height the dimensions
```

Pair with `sharp` for JPEG decode and PNG encode and you have a CLI port of the browser pipeline in ~30 lines.

## Deploy to Vercel

Vercel serves static sites at no cost on the hobby tier. The flow:

### 1. Push the repo to GitHub

```bash
cd astro-platesolve
git remote add origin https://github.com/<your-user>/<your-repo>.git
git push -u origin main
```

(If the repo on GitHub already exists and is empty, the above is the only step. If it has commits, `git pull --rebase origin main` first.)

### 2. Import the repo on Vercel

1. Go to <https://vercel.com/new> and sign in with GitHub if you haven't.
2. Click **Add New… → Project**.
3. Pick the GitHub repo from the list. If you don't see it, click "Adjust GitHub App Permissions" and grant access.
4. On the configuration screen:
   - **Framework Preset:** Other (or "No Framework" — both work).
   - **Build Command:** leave blank.
   - **Output Directory:** leave blank (root of the repo).
   - **Install Command:** leave blank.
   - **Root Directory:** `./` (the default).
5. Click **Deploy**.

Vercel will publish the repo verbatim. The first deploy takes about 30 seconds. Subsequent pushes to `main` redeploy automatically.

### 3. Add a custom domain (optional)

In the Vercel project settings → **Domains**, add your domain and follow the DNS instructions. The free `*.vercel.app` URL also works fine.

### 4. iPhone install

Open the deployed URL in Safari → Share → **Add to Home Screen**. The PWA manifest gives the app the eyepiece icon, fullscreen launch, and the app-style status bar.

## Python reference script

`astro_preprocess.py` is the original CLI implementation. It is **not** used by the web app, but it serves as:

- a reference for the algorithm, with the same numeric defaults and pipeline stages
- a faster batch tool when processing many images on a workstation (numpy + scipy is faster than the JS port at high resolution)
- a sanity check when changing the JS implementation: both should produce equivalent star catalogues

The Python version has been kept in sync with the JS fixes:

- circular crop optional via `--crop 0` (default on)
- boundary halo suppression after background subtraction
- both `dark` and `stacked` presets crop

Run it with:

```bash
pip install pillow numpy scipy
python astro_preprocess.py my_image.jpg                    # Dark preset (default)
python astro_preprocess.py my_image.jpg --preset stacked   # Stacked preset
python astro_preprocess.py my_image.jpg --crop 0           # disable circular crop
python astro_preprocess.py my_image.jpg --bg-kernel 25     # override individual params
```

Output is written next to the input as `<name>_platesolve.png`.

## Repository layout

```
astro-platesolve/
├── index.html              UI layout: header, file picker, previews, sliders, log
├── style.css               Mobile-first dark theme; range/number-input chip styling
├── src/main.js             Main thread: slider sync, preset application, worker orchestration, PNG export
├── src/worker.js           Web Worker: full image-processing pipeline (also Node-loadable for tests)
├── astro_preprocess.py     Python reference implementation of the same algorithm
├── samples/                Reference images: input / output for both presets
│   ├── input.jpeg          Short-exposure / Dark preset case
│   ├── output.png          Expected output for input.jpeg
│   ├── input2.jpeg         Stacked-exposure / Stacked preset case (bright background)
│   └── output2.png         Reference for input2 (legacy output from the original pipeline; current pipeline preserves rim stars it was missing)
├── icon.svg                App icon — eyepiece field with stars
├── favicon.svg             Compact favicon
├── icon-{180,192,512}.png  Rasterized icons for iOS Add-to-Home and the PWA manifest
├── manifest.webmanifest    PWA manifest (standalone display, theme colours, icon set)
├── README.md               This file
└── .gitignore              Ignores .DS_Store, editor temp files, node_modules, .vercel
```

## Tuning tips

- **Bright/cloudy sky, slow processing** → switch to Stacked preset.
- **Too many detections** (>200) → raise `threshold` or `min_blob`.
- **Real stars missing** → lower `threshold` or `min_blob`. Confirm `min_blob` AUTO badge is appropriate for your downsample.
- **Coloured speckle remains in output** → lower `color_cv` (e.g. 0.3).
- **Large bright objects (moon, planet) being rejected** → raise `max_blob`.
- **Field circle partially out of frame, or auto-crop misbehaves** → set `crop` to 0. The output keeps the original framing and the rest of the pipeline still produces a usable star catalogue.
- **Ring artifacts at the rim with crop on** → already handled by the blob-level halo filter; if you still see them, raise `bg_kernel` (the halo annulus widens with kernel size, so the filter excludes more of the boundary).
