# AstroPrep

A browser-based preprocessor for afocal phone astrophotos, used as a front end for [nova.astrometry.net](https://nova.astrometry.net) plate solving. Tuned for iPhone 12 Pro shots through a Dobsonian eyepiece — the workflow is to point, snap, then drop the JPEG into this tool to extract a clean star field for the astrometry source extractor.

All processing runs **entirely client-side** in a Web Worker. There is no backend; photos never leave the device. The repository is a static site — push to GitHub, hook up Vercel, and it deploys with zero configuration.

A reference implementation in Python (`astro_preprocess.py`) lives alongside the web app for batch processing on a workstation. Both implementations are kept in sync and produce equivalent results.

---

## Contents

- [What it does](#what-it-does)
- [Algorithm in detail](#algorithm-in-detail)
- [Parameters](#parameters)
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
- **Output:** square PNG, eyepiece field cropped and masked, only confirmed stars rendered as `_astroprep.png`.

The output drops directly into the Nova Astrometry upload form.

## Algorithm in detail

The pipeline is identical between the JS worker (`src/worker.js`) and the Python script (`astro_preprocess.py`). It runs in seven stages:

### 1. Load & EXIF orientation

Decode the input as RGB, honouring the EXIF orientation tag so phone photos taken in portrait don't end up sideways.

- **JS:** `createImageBitmap(file, { imageOrientation: 'from-image' })`
- **Python:** `PIL.ImageOps.exif_transpose`

### 2. Downsample

Resize the image by an integer factor (default 2×). Halving each dimension brings the median filter cost down and is usually visually indistinguishable for plate solving.

### 3. Auto-crop to the eyepiece field (optional, default on)

The eyepiece produces a bright lit field with a dark exterior. When enabled, we:

1. Compute luminance: `0.299·R + 0.587·G + 0.114·B`.
2. Find the bounding box of pixels above a brightness threshold (`0.3 × mean`).
3. Crop to the bounding square of that region.

This handles partial circles naturally by keeping all lit pixels within a square frame. The binary mask of the detected field is kept and used in stage 5.

### 4. Background subtraction

The eyepiece field has a smooth bright background gradient (sky glow, vignetting). Estimate it with a 2D **median filter** of size `bg_kernel` (default 20 px), then subtract it from the luminance.

The JS implementation uses **Huang's sliding-histogram median** (256-bin, per-row reset) for O(W·H·K) cost.

### 5. Eyepiece rim masking (erosion)

The median filter is unreliable near the boundary between the lit field and the dark exterior. We shrink the field mask from stage 3 using **binary erosion** (with a margin related to `bg_kernel`) and zero out the data outside this safe zone. This eliminates the "halo" artifacts around the eyepiece rim.

### 6. Star detection & filtering

Threshold the cleaned image at `threshold` DN (default 10) and run **connected component labeling**. Then evaluate each blob through a filter pipeline:

| Filter | Reject if | Why |
|---|---|---|
| **Size** | size < `min_blob` or > `max_blob` | iPhone ISO noise clusters are tiny; real stars are large diffuse blobs. |
| **Compactness** | peak / size > `max_compact` (default 6.0) | Real stars: 0.2–5.0. Noise spikes: >10. |
| **Colour CV** | std(R,G,B)/mean(R,G,B) > `color_cv` (default 0.5) | Real stars are neutral white. ISO noise is often strongly coloured (red/green/blue speckles). |

### 7. Render

Take the cleaned luminance under the accepted-star mask, apply a small Gaussian blur (`glow_sigma`, default 1.5 px), normalize the brightest pixel to 255, multiply by `stretch` (default 3.0), then add `bg_lift` (default 12 DN) so the background isn't pure black.

## Parameters

| Parameter | Default | What it does |
|---|---|---|
| `crop` | 1 | Detect the eyepiece field and crop to a square. |
| `downsample` | 2× | Process at 1/N resolution. Higher = faster. |
| `bg_kernel` | 20 | Median filter size for background estimation. |
| `threshold` | 10 | DN above local background to enter detection. |
| `min_blob` | 4 | Minimum star size in pixels. Primary noise filter. |
| `max_blob` | 2000 | Maximum star size. |
| `max_compact` | 6.0 | Reject blobs with peak/size above this. |
| `color_cv` | 0.5 | Reject blobs with strong RGB imbalance. |
| `glow_sigma` | 1.5 | Gaussian sigma for star profile rendering. |
| `stretch` | 3.0× | Brightness multiplier after normalisation. |
| `bg_lift` | 12 | Output background grey level. |

## App architecture

```
┌────────────────────┐     ┌─────────────────┐
│  index.html (UI)   │     │  src/main.js    │
│  - sliders         │────▶│  - sliders sync │
│  - canvases        │     │  - file load    │
│  - file picker     │     │  - PNG export   │
└────────────────────┘     └────────┬────────┘
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

The heavy lifting happens in a **Web Worker** to ensure the UI remains responsive during the median filter and star extraction passes.

## Run locally for development

Serve the project folder via any static-file server, for example:

```bash
python3 -m http.server 8000
```

Then open <http://localhost:8000>.

## Deploy to Vercel

Vercel serves static sites at no cost on the hobby tier.

1. Push the repo to GitHub.
2. Import the repo on Vercel at <https://vercel.com/new>.
3. Pick "Other" or "No Framework" — the defaults work fine.
4. Click **Deploy**.

## Python reference script

`astro_preprocess.py` is the original CLI implementation. Run it with:

```bash
pip install pillow numpy scipy
python astro_preprocess.py my_image.jpg
```

## Repository layout

```
AstroPrep/
├── index.html              UI layout: header, file picker, previews, sliders, log
├── style.css               Mobile-first dark theme; range/number-input chip styling
├── src/main.js             Main thread: slider sync, worker orchestration, PNG export
├── src/worker.js           Web Worker: image-processing pipeline
├── astro_preprocess.py     Python reference implementation
├── samples/                Reference images: input / output
├── icon.svg                App icon
├── favicon.svg             Compact favicon
├── manifest.webmanifest    PWA manifest
└── README.md               This file
```

## Tuning tips

- **Too many detections** (>200) → raise `threshold` or `min_blob`.
- **Real stars missing** → lower `threshold` or `min_blob`.
- **Coloured speckle remains in output** → lower `color_cv` (e.g. 0.3).
- **Large bright objects (moon, planet) being rejected** → raise `max_blob`.
- **Field circle partially out of frame** → set `crop` to 0.
- **Ring artifacts at the rim** → raise `bg_kernel` to widen the safe zone erosion.
