# Astro Platesolve

A browser-based preprocessor for afocal phone astrophotos, used as a front end for [nova.astrometry.net](https://nova.astrometry.net) plate solving. Tuned for iPhone 12 Pro shots through a Dobsonian eyepiece — the workflow is to point, snap, then drop the JPEG into this tool to extract a clean star field for the astrometry source extractor.

All processing runs **entirely client-side** using a Web Worker. There is no backend; photos never leave the device. Hosted as static files, deployable for free on Vercel, GitHub Pages, Netlify, or anything that serves HTML.

## Pipeline (port of `astro_preprocess.py`)

1. EXIF-aware load via `createImageBitmap`
2. Optional downsample (1×–4×)
3. Auto-crop to circular eyepiece field (always on — bbox of bright pixels → inscribed circle, 3% shrink)
4. Background subtraction via 2D sliding-histogram median filter (Huang-style, 256-bin)
5. Connected components (4-connectivity, scipy `ndimage.label`-equivalent)
6. Blob filter: size, compactness (peak/area), colour CV
7. Gaussian profile rendering, normalize, stretch, background lift
8. PNG export via `canvas.toBlob`

The defaults match the Python script. Slider tooltips note when to deviate.

## Run locally

It is a static site — just open `index.html` over HTTP (file:// won't work because Web Workers need a real origin):

```bash
cd astro-platesolve
python3 -m http.server 8000
# open http://localhost:8000
```

Any other static server works (`npx serve`, `caddy`, `nginx`).

## Deploy to Vercel via GitHub

1. Push the repo to GitHub.
2. On vercel.com → "Add New Project" → import the GitHub repo.
3. Framework preset: **Other** (no build step needed).
4. Output directory: leave blank (root).
5. Deploy.

Vercel auto-deploys on every push to `main`.

## Mobile usage

Tested on iPhone 12 Pro / iOS Safari. The page is installable as a PWA via Share → Add to Home Screen — it then opens fullscreen with the eyepiece icon.

For 12 MP iPhone shots the default downsample of 2× is the right trade-off: ~10 s on the phone with no risk of running out of memory. Use 1× on desktop for full-resolution output.

## Files

```
index.html              Layout + sliders
style.css               Mobile-first dark theme
src/main.js             UI logic, worker orchestration, PNG export
src/worker.js           Image-processing pipeline (no DOM dependencies)
icon.svg / favicon.svg  App icons
icon-{192,512}.png      Rasterized icons for manifest / iOS
manifest.webmanifest    PWA manifest
samples/                Reference input/output images for testing
```

## Credits

Algorithm adapted from `astro_preprocess.py` by the same author. The original CLI (Python + numpy + scipy) remains a separate project for batch use.
