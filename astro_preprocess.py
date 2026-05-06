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
Key insight: real stars are large diffuse blobs, while high-ISO noise spikes 
are tiny and compact. A low threshold + minimum size filter is the primary 
discriminator.

Usage:
    python astro_preprocess.py my_image.jpg
    python astro_preprocess.py my_image.jpg --downsample 1 --min-blob 15
    python astro_preprocess.py my_image.jpg --bg-kernel 25 --threshold 12

The eyepiece field is detected by luminance thresholding and cropped to a 
padded square. This handles partial circles naturally by keeping all lit 
pixels within a square frame. A shape-agnostic halo filter (binary erosion) 
eliminates median-filter artifacts at the eyepiece rim.

Requirements:
    pip install pillow numpy scipy
"""

import argparse
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from scipy.ndimage import median_filter, label, gaussian_filter, binary_erosion


# ── Default parameters (tuned on iPhone 12 Pro / Heritage 100P afocal shots) ─

DEFAULT_BG_KERNEL   = 20    # Median filter size for sky background estimation.
                            # Larger handles uneven backgrounds better.

DEFAULT_THRESHOLD   = 10    # DN above local background to enter detection.
                            # Keep low — real stars are large diffuse blobs and
                            # their per-pixel delta is modest. Noise is handled
                            # by the size and compactness filters instead.

DEFAULT_MIN_BLOB    = 4     # Minimum blob size in pixels.
                            # PRIMARY noise filter: iPhone ISO noise clusters
                            # are almost always <3 px at 2x downsample.

DEFAULT_MAX_BLOB    = 2000  # Maximum blob size in pixels.
                            # Large blobs are almost always bright/distorted stars
                            # not noise, so a generous limit is safe.

DEFAULT_MAX_COMPACT = 6.0   # Maximum allowed peak/size ratio (compactness).
                            # Real stars: 0.2-5.0 (at 2x). Noise spikes: >10.
                            # Threshold of 6.0 gives a clean gap for this camera.

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


def auto_crop_to_field(data: np.ndarray, lum: np.ndarray):
    """
    Detect eyepiece field and crop to a padded square.
    Returns raw cropped data, luminance, and mask.
    """
    # Threshold: outside is much darker than mean
    mask = lum > lum.mean() * 0.3
    
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)

    if not rows.any() or not cols.any():
        print("  [crop]  WARNING: could not detect eyepiece field — using full frame")
        return data, lum, None

    rmin, rmax = np.where(rows)[0][[0, -1]]
    cmin, cmax = np.where(cols)[0][[0, -1]]

    # Center and size for square crop
    rc, cc = (rmin + rmax) // 2, (cmin + cmax) // 2
    size = int(max(rmax - rmin, cmax - cmin))
    hs = size // 2
    
    # Create square canvases
    cropped_data = np.zeros((size, size, 3), dtype=data.dtype)
    cropped_lum  = np.zeros((size, size),    dtype=lum.dtype)
    cropped_mask = np.zeros((size, size),    dtype=bool)
    
    # Source bounds (clamped to image)
    sy0, sy1 = max(0, rc - hs), min(data.shape[0], rc + hs)
    sx0, sx1 = max(0, cc - hs), min(data.shape[1], cc + hs)
    
    # Destination bounds (centered on canvas)
    dy0 = hs - (rc - sy0)
    dx0 = hs - (cc - sx0)
    h_actual, w_actual = sy1 - sy0, sx1 - sx0
    dy1, dx1 = dy0 + h_actual, dx0 + w_actual
    
    # Copy data to square frame
    cropped_data[dy0:dy1, dx0:dx1] = data[sy0:sy1, sx0:sx1]
    cropped_lum[dy0:dy1, dx0:dx1]  = lum[sy0:sy1, sx0:sx1]
    cropped_mask[dy0:dy1, dx0:dx1] = mask[sy0:sy1, sx0:sx1]
    
    print(f"  [crop]  {data.shape[1]}x{data.shape[0]} -> {size}x{size} px "
          f"(threshold-based mask and padded square crop)")
    
    return cropped_data, cropped_lum, cropped_mask


def subtract_background(lum: np.ndarray, kernel: int) -> np.ndarray:
    """
    Estimate and subtract the local sky background via median filtering.
    """
    bg = median_filter(lum, size=kernel, mode='nearest')
    return np.clip(lum - bg, 0, 255)


def mask_eyepiece_field(data: np.ndarray, cleaned: np.ndarray, 
                        field_mask: np.ndarray, bg_kernel: int):
    """
    Shrinks the field mask (erosion) and applies it to both RGB and luminance
    data. This eliminates the "halo" artifacts around the eyepiece rim.
    """
    if field_mask is None:
        return data, cleaned
    
    # Safe zone: where the median filter is reliable
    margin = (bg_kernel // 2) + 2
    eroded_mask = binary_erosion(field_mask, iterations=margin)
    
    # Mask both the raw RGB data and the cleaned luminance
    cleaned_masked = np.where(eroded_mask, cleaned, 0.0)
    data_masked    = np.where(eroded_mask[..., None], data, 0.0)
    
    return data_masked, cleaned_masked


def extract_stars(cleaned:     np.ndarray,
                  rgb:         tuple,
                  threshold:   float,
                  min_blob:    int,
                  max_blob:    int,
                  max_compact: float,
                  color_cv:    float) -> np.ndarray:
    """
    Detect and filter blobs. Returns a boolean mask of accepted star pixels.
    """
    r, g, b = rgb

    binary         = cleaned > threshold
    labeled, n_all = label(binary)

    n_size = n_compact = n_color = n_kept = 0
    mask = np.zeros_like(binary)

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

        mask |= blob
        n_kept += 1

    # Logging
    print(f"  [detect] Blobs above threshold: {n_all}")
    print(f"  [filter] Rejected by size        (<{min_blob} or >{max_blob} px): {n_size}")
    print(f"  [filter] Rejected by compactness (peak/size > {max_compact}):    {n_compact}")
    print(f"  [filter] Rejected by colour      (CV > {color_cv}):              {n_color}")
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
    """
    star_data = np.where(mask, cleaned, 0.0)

    if glow_sigma > 0:
        star_data = gaussian_filter(star_data, sigma=glow_sigma)

    max_val = star_data.max()
    if max_val > 0:
        star_data = np.clip(star_data * (255.0 / max_val) * stretch, 0, 255)

    return np.clip(star_data + bg_lift, 0, 255).astype(np.uint8)


def process(input_path:   str,
            crop:         bool  = True,
            downsample:   int   = 2,
            bg_kernel:    int   = DEFAULT_BG_KERNEL,
            threshold:    float = DEFAULT_THRESHOLD,
            min_blob:     int   = DEFAULT_MIN_BLOB,
            max_blob:     int   = DEFAULT_MAX_BLOB,
            max_compact:  float = DEFAULT_MAX_COMPACT,
            color_cv:     float = DEFAULT_COLOR_CV,
            glow_sigma:   float = DEFAULT_GLOW_SIGMA,
            stretch:      float = DEFAULT_STRETCH,
            bg_lift:      int   = DEFAULT_BG_LIFT) -> str:

    input_path  = Path(input_path)
    output_path = input_path.with_name(input_path.stem + "_astroprep.png")

    print(f"\n{'='*57}")
    print(f"  {input_path.name}")
    print(f"{'='*57}")

    img  = Image.open(str(input_path))
    img  = ImageOps.exif_transpose(img)
    img  = img.convert("RGB")
    print(f"  [load]   {img.size[0]}x{img.size[1]} px")

    # Downsample if requested
    if downsample > 1:
        new_w = img.width  // downsample
        new_h = img.height // downsample
        img = img.resize((new_w, new_h), Image.LANCZOS)
        print(f"  [down]   {downsample}x downsample -> {new_w}x{new_h} px")

    data = np.array(img, dtype=np.float32)
    r, g, b = data[:,:,0], data[:,:,1], data[:,:,2]
    lum = 0.299*r + 0.587*g + 0.114*b

    # Crop to eyepiece field (optional, default on)
    if crop:
        data, lum, field_mask = auto_crop_to_field(data, lum)
    else:
        field_mask = None
        print(f"  [crop]  skipped — crop disabled, processing full frame")

    print(f"  [bg]     Mean luminance: {lum.mean():.1f}")

    # Process
    cleaned = subtract_background(lum, bg_kernel)
    data, cleaned = mask_eyepiece_field(data, cleaned, field_mask, bg_kernel)

    r, g, b = data[:,:,0], data[:,:,1], data[:,:,2]
    mask = extract_stars(cleaned, (r, g, b),
                         threshold, min_blob, max_blob, max_compact, color_cv)
    final   = render_output(cleaned, mask, glow_sigma, stretch, bg_lift)

    _, n_est = label(final > 40)
    print(f"  [output] Estimated astrometry detections: ~{n_est}")
    print(f"  [output] Saved: {output_path.name}")
    print(f"{'='*57}\n")

    Image.fromarray(final, mode="L").convert("RGB").save(str(output_path), format="PNG")
    return str(output_path)


def main():
    parser = argparse.ArgumentParser(
        description="AstroPrep: Preprocess astrophotos for Nova Astrometry plate solving.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Tuning tips:
  Too many detections    ->  raise --threshold or --min-blob
  Real stars missing     ->  lower --threshold or --min-blob
  Colour noise remains   ->  lower --color-cv (e.g. 0.3)
  Uneven background      ->  raise --bg-kernel (e.g. 25)
        """
    )
    parser.add_argument("input",
        help="Input image file (JPEG, PNG, TIFF, ...)")
    parser.add_argument("--crop", type=int, default=1, choices=[0, 1],
        help="1 = threshold the eyepiece field and crop to square "
             "(default). 0 = process the full frame as-is.")
    parser.add_argument("--downsample",   type=int,   default=2,
        help="Downsample factor before processing (default: 2).")
    parser.add_argument("--bg-kernel",    type=int,   default=DEFAULT_BG_KERNEL,
        help=f"Background median filter size (default: {DEFAULT_BG_KERNEL}).")
    parser.add_argument("--threshold",    type=float, default=DEFAULT_THRESHOLD,
        help=f"Detection threshold DN above background (default: {DEFAULT_THRESHOLD})")
    parser.add_argument("--min-blob",     type=int,   default=DEFAULT_MIN_BLOB,
        help=f"Minimum star size in pixels (default: {DEFAULT_MIN_BLOB})")
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

    process(
        args.input,
        crop        = bool(args.crop),
        downsample  = args.downsample,
        bg_kernel   = args.bg_kernel,
        threshold   = args.threshold,
        min_blob    = args.min_blob,
        max_blob    = args.max_blob,
        max_compact = args.max_compact,
        color_cv    = args.color_cv,
        glow_sigma  = args.glow_sigma,
        stretch     = args.stretch,
        bg_lift     = args.bg_lift,
    )


if __name__ == "__main__":
    main()
