"""
Astrophoto Plate-Solve Preprocessor
=====================================
Cleans up astrophotos for submission to Nova Astrometry (nova.astrometry.net).

Reference Python implementation. The companion in-browser version
(`index.html`, `src/worker.js`) ports the same algorithm to JavaScript and
runs entirely on the client. Both implementations produce equivalent
results for plate solving; this script remains useful for batch processing
on a workstation.

Tuned for afocal phone shots (iPhone 12 Pro / similar) through a Dobsonian.
Key insight: real stars are large diffuse blobs (>=15 px at threshold 10),
while high-ISO noise spikes are tiny and compact (<10 px, compactness > 9).
A low threshold + minimum size filter is the primary discriminator.

Usage:
    python astro_preprocess.py my_image.jpg
    python astro_preprocess.py my_image.jpg --preset stacked
    python astro_preprocess.py my_image.jpg --bg-kernel 25 --min-blob 8

The eyepiece field is cropped to its inscribed square by default. Pass
`--crop 0` (or `crop=False` to `process()`) to skip cropping and process
the full frame as-is — useful when the field circle is partially out of
view or auto-detection misbehaves on a particular image. When cropping is
enabled, a blob-level halo filter rejects low-amplitude false detections
in the median-filter halo annulus just inside the rim while preserving
real bright rim stars.

Requirements:
    pip install pillow numpy scipy
"""

import argparse
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from scipy.ndimage import median_filter, label, gaussian_filter


# ── Default parameters (tuned on iPhone 12 Pro / Heritage 100P afocal shots) ─

DEFAULT_BG_KERNEL   = 15    # Median filter size for sky background estimation.
                            # Increase to 25 for bright/uneven backgrounds
                            # (sky glow, light pollution, clouds).

DEFAULT_THRESHOLD   = 10    # DN above local background to enter detection.
                            # Keep low — real stars are large diffuse blobs and
                            # their per-pixel delta is modest. Noise is handled
                            # by the size and compactness filters instead.

DEFAULT_MIN_BLOB    = 15    # Minimum blob size in pixels.
                            # PRIMARY noise filter: iPhone ISO noise clusters
                            # are almost always <10 px. Real stars are >=20 px.
                            # If using --downsample 2, this is auto-halved
                            # unless you set it explicitly.

DEFAULT_MAX_BLOB    = 2000  # Maximum blob size in pixels.
                            # Large blobs are almost always bright/distorted stars
                            # not noise, so a generous limit is safe.

DEFAULT_MAX_COMPACT = 3.0   # Maximum allowed peak/size ratio (compactness).
                            # Real stars: 0.2-2.2. Noise spikes: 9-44.
                            # Threshold of 3.0 gives a clean gap for this camera.

DEFAULT_COLOR_CV    = 0.5   # Maximum colour coefficient of variation (0-1).
                            # Rejects blobs where one RGB channel dominates.
                            # Real stars are neutral white. ISO noise is often
                            # strongly coloured (red/green/blue speckles).
                            # Lower = stricter. Set to 1.0 to disable.

DEFAULT_GLOW_SIGMA  = 1.5   # Gaussian sigma for star profile rendering.
                            # Produces smooth round blobs that astrometry.net's
                            # centroider expects. Does not affect detection.

DEFAULT_STRETCH     = 3.0   # Brightness multiplier after normalisation.

DEFAULT_BG_LIFT     = 12    # Background grey level in output image (0 = pure black).
                            # A slight lift helps the source extractor set its
                            # detection threshold cleanly.

# ─────────────────────────────────────────────────────────────────────────────

# ── Named presets ─────────────────────────────────────────────────────────────
# Use with --preset <name>. Individual flags override preset values.
# Note: cropping is on by default for both presets but can be overridden
# with --crop 0. The two presets only differ in downsample factor and
# median kernel size.

PRESETS = {
    "dark": {
        # Short exposure (~1/3 s), high ISO. Dark background, mostly uniform.
        # The smaller bg-kernel is enough; downsample is unnecessary.
        "downsample":  1,
        "bg_kernel":   15,
        "threshold":   10,
        "min_blob":    15,
        "max_blob":    2000,
        "max_compact": 3.0,
        "color_cv":    0.5,
    },
    "stacked": {
        # Stacked / longer exposure. Bright background (sky glow fills the
        # field of view). A larger bg kernel handles the uneven background;
        # downsampling 2x makes the median filter tractable.
        "downsample":  2,
        "bg_kernel":   25,
        "threshold":   10,
        "min_blob":    None,   # auto-halved to 8 by downsample logic
        "max_blob":    2000,
        "max_compact": 3.0,
        "color_cv":    0.5,
    },
}


def auto_crop_to_field(data: np.ndarray, field_threshold: float = 3.0,
                        shrink: float = 0.97):
    """
    Detect the eyepiece circular field, apply a circular mask zeroing everything
    outside it, then crop to the circle bounding box.

    Called by process() only when crop=True. The radius is shrunk by 3% to
    eliminate boundary artifacts from the eyepiece field stop.

    Returns (cropped_data, (cy, cx, radius)) where the centre coordinates are
    in the cropped frame and used downstream for boundary erosion.
    """
    lum = 0.299*data[:,:,0] + 0.587*data[:,:,1] + 0.114*data[:,:,2]
    field_mask = lum > field_threshold

    rows = np.any(field_mask, axis=1)
    cols = np.any(field_mask, axis=0)

    if not rows.any() or not cols.any():
        print("  [crop]  WARNING: could not detect eyepiece field — using full frame")
        h, w = data.shape[:2]
        return data, (h // 2, w // 2, min(h, w) // 2)

    rmin, rmax = np.where(rows)[0][[0, -1]]
    cmin, cmax = np.where(cols)[0][[0, -1]]

    # Circle centre and radius, shrunk to kill eyepiece field-stop transition
    rc     = (rmin + rmax) // 2
    cc     = (cmin + cmax) // 2
    radius = int(min(rmax - rmin, cmax - cmin) // 2 * shrink)

    # Build circular mask and zero outside it
    h, w   = data.shape[:2]
    yy, xx = np.ogrid[:h, :w]
    circle = (yy - rc)**2 + (xx - cc)**2 <= radius**2
    masked = data.copy()
    masked[~circle] = 0

    # Crop to bounding square of the circle
    r0, r1 = max(0, rc - radius), min(h, rc + radius)
    c0, c1 = max(0, cc - radius), min(w, cc + radius)
    cropped = masked[r0:r1, c0:c1]

    # Centre in cropped coordinates (handles rare clamped cases at image edge)
    new_cy = rc - r0
    new_cx = cc - c0

    print(f"  [crop]  {data.shape[1]}x{data.shape[0]} -> {cropped.shape[1]}x{cropped.shape[0]} px "
          f"(circular mask r={radius}px, shrink={shrink:.0%})")
    return cropped, (new_cy, new_cx, radius)


def subtract_background(lum: np.ndarray, kernel: int) -> np.ndarray:
    """
    Estimate and subtract the local sky background via median filtering.

    `mode='nearest'` (clamp) matches the JS port's boundary handling. The
    default 'reflect' would mirror bright field content across the image
    edge, while 'nearest' replicates the actual edge row (which is mostly
    zeroed exterior of the eyepiece circle). The downstream halo filter
    is calibrated to the clamp behaviour, so use 'nearest' for both.
    """
    bg = median_filter(lum, size=kernel, mode='nearest')
    return np.clip(lum - bg, 0, 255)


def enforce_field_mask(cleaned: np.ndarray, cy: int, cx: int,
                        radius: int) -> np.ndarray:
    """
    Make sure pixels outside the field circle stay at zero. The median
    filter window straddling the exterior would otherwise leave non-zero
    values just outside the rim. The actual halo *inside* the rim is
    rejected at the blob level by extract_stars().
    """
    if radius <= 0:
        return cleaned
    h, w   = cleaned.shape
    yy, xx = np.ogrid[:h, :w]
    field_mask = (yy - cy)**2 + (xx - cx)**2 <= radius**2
    out = np.where(field_mask, cleaned, 0.0)
    return out


def extract_stars(cleaned:     np.ndarray,
                  rgb:         tuple,
                  threshold:   float,
                  min_blob:    int,
                  max_blob:    int,
                  max_compact: float,
                  color_cv:    float,
                  field_cy:    int,
                  field_cx:    int,
                  field_radius: int,
                  bg_kernel:   int) -> np.ndarray:
    """
    Detect and filter blobs. Returns a boolean mask of accepted star pixels.

    Filter pipeline (in order):
      1. Threshold    — only pixels meaningfully above local background
      2. Size         — primary noise rejection (noise <10 px, stars >=15 px)
      3. Compactness  — rejects unusually bright tiny spikes (peak / size)
      4. Colour       — rejects strongly coloured ISO noise speckles
      5. Halo         — rejects low-amplitude blobs in the median-bias annulus
                        just inside the field rim (real bright rim stars survive
                        because their peak sits far above the halo level).
    """
    r, g, b = rgb

    binary         = cleaned > threshold
    labeled, n_all = label(binary)

    # Blob centroid is needed for the halo filter
    halo_margin = (bg_kernel + 1) // 2
    safe_radius = max(1, field_radius - halo_margin) if field_radius > 0 else 0
    safe_r2     = safe_radius * safe_radius
    halo_max    = 5 * threshold   # see worker.js — bias can reach ~50% of bright field

    n_size = n_compact = n_color = n_halo = n_kept = 0
    mask = np.zeros_like(binary)
    h, w = cleaned.shape

    for blob_id in range(1, n_all + 1):
        blob = labeled == blob_id
        sz   = int(blob.sum())

        # 1. Size filter
        if not (min_blob <= sz <= max_blob):
            n_size += 1
            continue

        # 2. Compactness filter
        peak        = float(cleaned[blob].max())
        compactness = peak / sz
        if compactness > max_compact:
            n_compact += 1
            continue

        # 3. Colour filter
        mr       = float(r[blob].mean())
        mg       = float(g[blob].mean())
        mb_      = float(b[blob].mean())
        mean_rgb = (mr + mg + mb_) / 3.0
        if mean_rgb < 1.0:
            n_color += 1
            continue
        cv = float(np.std([mr, mg, mb_]) / mean_rgb)
        if cv > color_cv:
            n_color += 1
            continue

        # 4. Boundary-halo filter (only when we have a detected field circle)
        if field_radius > 0:
            ys, xs = np.where(blob)
            cy_blob = float(ys.mean())
            cx_blob = float(xs.mean())
            d2 = (cy_blob - field_cy)**2 + (cx_blob - field_cx)**2
            if d2 > safe_r2 and peak < halo_max:
                n_halo += 1
                continue

        mask |= blob
        n_kept += 1

    # Logging
    print(f"  [detect] Blobs above threshold: {n_all}")
    print(f"  [filter] Rejected by size        (<{min_blob} or >{max_blob} px): {n_size}")
    print(f"  [filter] Rejected by compactness (peak/size > {max_compact}):    {n_compact}")
    print(f"  [filter] Rejected by colour      (CV > {color_cv}):              {n_color}")
    print(f"  [filter] Rejected by halo        (rim, peak < {halo_max:g} DN):       {n_halo}")
    print(f"  [result] Stars accepted: {n_kept}")

    if n_kept < 10:
        print(f"  [warn]   Only {n_kept} stars found — try lowering --threshold or --min-blob.")
    if n_kept > 200:
        print(f"  [warn]   {n_kept} stars found — may still be noisy, try raising --threshold or --min-blob.")

    return mask


def render_output(cleaned:    np.ndarray,
                  mask:       np.ndarray,
                  glow_sigma: float,
                  stretch:    float,
                  bg_lift:    int) -> np.ndarray:
    """
    Render confirmed stars with smooth Gaussian profiles.
    Glow is applied only after masking so it never bleeds into noise regions.
    """
    star_data = np.where(mask, cleaned, 0.0)

    if glow_sigma > 0:
        star_data = gaussian_filter(star_data, sigma=glow_sigma)

    max_val = star_data.max()
    if max_val > 0:
        star_data = np.clip(star_data * (255.0 / max_val) * stretch, 0, 255)

    return np.clip(star_data + bg_lift, 0, 255).astype(np.uint8)


def process(input_path:   str,
            crop:         bool  = True,    # circular crop to eyepiece field
            downsample:   int   = 1,
            bg_kernel:    int   = DEFAULT_BG_KERNEL,
            threshold:    float = DEFAULT_THRESHOLD,
            min_blob:     int   = None,   # None = auto based on downsample
            max_blob:     int   = DEFAULT_MAX_BLOB,
            max_compact:  float = DEFAULT_MAX_COMPACT,
            color_cv:     float = DEFAULT_COLOR_CV,
            glow_sigma:   float = DEFAULT_GLOW_SIGMA,
            stretch:      float = DEFAULT_STRETCH,
            bg_lift:      int   = DEFAULT_BG_LIFT) -> str:

    input_path  = Path(input_path)
    output_path = input_path.with_name(input_path.stem + "_platesolve.png")

    print(f"\n{'='*57}")
    print(f"  {input_path.name}")
    print(f"{'='*57}")

    img  = Image.open(str(input_path))
    img  = ImageOps.exif_transpose(img)   # honour phone orientation tag
    img  = img.convert("RGB")
    print(f"  [load]   {img.size[0]}x{img.size[1]} px")

    # Downsample if requested
    if downsample > 1:
        new_w = img.width  // downsample
        new_h = img.height // downsample
        img = img.resize((new_w, new_h), Image.LANCZOS)
        print(f"  [down]   {downsample}x downsample -> {new_w}x{new_h} px")

    data = np.array(img, dtype=np.float32)

    # Auto min_blob: scale with downsample factor
    effective_min_blob = min_blob if min_blob is not None else max(2, DEFAULT_MIN_BLOB // downsample)
    if min_blob is None and downsample > 1:
        print(f"  [info]   min_blob auto-set to {effective_min_blob} (={DEFAULT_MIN_BLOB}/{downsample})")

    # Crop to eyepiece field (optional, default on)
    if crop:
        data, (cy, cx, radius) = auto_crop_to_field(data)
    else:
        h, w = data.shape[:2]
        cy, cx, radius = h // 2, w // 2, 0   # radius=0 disables the halo filter
        print(f"  [crop]  skipped — circular crop disabled, processing full frame")

    r, g, b = data[:,:,0], data[:,:,1], data[:,:,2]
    lum = 0.299*r + 0.587*g + 0.114*b
    print(f"  [bg]     Mean luminance: {lum.mean():.1f} (>30 suggests bright sky, use --bg-kernel 25)")

    # Process
    cleaned = subtract_background(lum, bg_kernel)
    cleaned = enforce_field_mask(cleaned, cy, cx, radius)
    mask    = extract_stars(cleaned, (r, g, b),
                            threshold, effective_min_blob, max_blob, max_compact, color_cv,
                            cy, cx, radius, bg_kernel)
    final   = render_output(cleaned, mask, glow_sigma, stretch, bg_lift)

    _, n_est = label(final > 40)
    print(f"  [output] Estimated astrometry detections: ~{n_est}")
    print(f"  [output] Saved: {output_path.name}")
    print(f"{'='*57}\n")

    Image.fromarray(final, mode="L").convert("RGB").save(str(output_path), format="PNG")
    return str(output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Preprocess astrophotos for Nova Astrometry plate solving.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Tuning tips:
  Bright/cloudy sky, slow processing  ->  --preset stacked
  Too many detections                 ->  raise --threshold or --min-blob
  Real stars missing                  ->  lower --threshold or --min-blob
  Colour noise remains                ->  lower --color-cv (e.g. 0.3)
        """
    )
    parser.add_argument("input",
        help="Input image file (JPEG, PNG, TIFF, ...)")
    parser.add_argument("--preset", default=None,
        help="Parameter preset: 'dark' (short exposure, high ISO) or "
             "'stacked' (longer/stacked exposure, bright background). "
             "Individual flags override preset values.")
    parser.add_argument("--crop", type=int, default=1, choices=[0, 1],
        help="1 = detect the eyepiece field, mask to a circle and crop "
             "(default). 0 = process the full frame as-is.")
    parser.add_argument("--downsample",   type=int,   default=1,
        help="Downsample factor before processing, e.g. 2 (default: 1). "
             "Use for bright backgrounds or large images that are slow to process. "
             "min-blob is auto-halved when downsampling.")
    parser.add_argument("--bg-kernel",    type=int,   default=DEFAULT_BG_KERNEL,
        help=f"Background median filter size (default: {DEFAULT_BG_KERNEL}). "
             f"Use 25 for bright/uneven backgrounds.")
    parser.add_argument("--threshold",    type=float, default=DEFAULT_THRESHOLD,
        help=f"Detection threshold DN above background (default: {DEFAULT_THRESHOLD})")
    parser.add_argument("--min-blob",     type=int,   default=None,
        help=f"Minimum star size in pixels (default: {DEFAULT_MIN_BLOB}, "
             f"auto-halved when --downsample 2)")
    parser.add_argument("--max-blob",     type=int,   default=DEFAULT_MAX_BLOB,
        help=f"Maximum star size in pixels (default: {DEFAULT_MAX_BLOB})")
    parser.add_argument("--max-compact",  type=float, default=DEFAULT_MAX_COMPACT,
        help=f"Max compactness peak/size (default: {DEFAULT_MAX_COMPACT})")
    parser.add_argument("--color-cv",     type=float, default=DEFAULT_COLOR_CV,
        help=f"Max colour variation 0-1 (default: {DEFAULT_COLOR_CV})")
    parser.add_argument("--glow-sigma",   type=float, default=DEFAULT_GLOW_SIGMA,
        help=f"Gaussian profile sigma (default: {DEFAULT_GLOW_SIGMA})")
    parser.add_argument("--stretch",      type=float, default=DEFAULT_STRETCH,
        help=f"Brightness stretch multiplier (default: {DEFAULT_STRETCH})")
    parser.add_argument("--bg-lift",      type=int,   default=DEFAULT_BG_LIFT,
        help=f"Output background grey level (default: {DEFAULT_BG_LIFT})")

    args = parser.parse_args()

    # Apply preset first, then let any explicit flags override
    preset_vals = {}
    if args.preset:
        if args.preset not in PRESETS:
            parser.error(f"Unknown preset '{args.preset}'. Choose from: {', '.join(PRESETS)}")
        preset_vals = PRESETS[args.preset].copy()
        print(f"Using preset: {args.preset}")

    def pval(key, default):
        """Return explicit arg if given, else preset value, else default."""
        explicit = getattr(args, key.replace("-", "_"), None)
        if explicit is not None and explicit != default:
            return explicit
        return preset_vals.get(key, default)

    process(
        args.input,
        crop        = bool(args.crop),
        downsample  = pval("downsample",  args.downsample),
        bg_kernel   = pval("bg_kernel",   args.bg_kernel),
        threshold   = pval("threshold",   args.threshold),
        min_blob    = pval("min_blob",    args.min_blob),
        max_blob    = pval("max_blob",    args.max_blob),
        max_compact = pval("max_compact", args.max_compact),
        color_cv    = pval("color_cv",    args.color_cv),
        glow_sigma  = args.glow_sigma,
        stretch     = args.stretch,
        bg_lift     = args.bg_lift,
    )


if __name__ == "__main__":
    main()
