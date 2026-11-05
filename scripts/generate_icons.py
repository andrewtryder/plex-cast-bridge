#!/usr/bin/env python3
"""
Generate clean, transparent icons from the uploaded image.
Removes the baked-in preview checkerboard pattern and produces anti-aliased,
high-resolution PNG and ICO assets for README, web apps, and favicons.
"""

from pathlib import Path
from PIL import Image

SOURCE_IMAGE = Path("/Users/atr/.gemini/antigravity/brain/81959878-9266-42c3-8bb9-9a18caf80295/.user_uploaded/media_1793831992788_0c17491d.jpg")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = PROJECT_ROOT / "assets"
STATIC_DIR = PROJECT_ROOT / "gcp-cloud-run" / "src" / "cloud_gateway" / "static"
ARTIFACT_DIR = Path("/Users/atr/.gemini/antigravity/brain/81959878-9266-42c3-8bb9-9a18caf80295")


def main():
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Loading source image from {SOURCE_IMAGE}...")
    img = Image.open(SOURCE_IMAGE).convert("RGBA")

    # 4x Supersampled superellipse mask matching the exact squircle geometry
    # Squircle spans [128, 895] in 1024x1024, center=511.5, radius=383.0, exponent=5.15
    scale = 4
    w, h = 1024 * scale, 1024 * scale
    xc, yc = 511.5 * scale, 511.5 * scale
    a, b = 383.0 * scale, 383.0 * scale
    n = 5.15

    print("Synthesizing 4x supersampled antialiased squircle mask...")
    buf = bytearray(w * h)
    for y in range(h):
        dy = abs(y - yc) / b
        dyn = dy ** n
        if dyn > 1.0:
            continue
        max_dx = a * ((1.0 - dyn) ** (1.0 / n))
        x_min = max(0, int(xc - max_dx))
        x_max = min(w - 1, int(xc + max_dx))
        row_offset = y * w
        buf[row_offset + x_min : row_offset + x_max + 1] = b"\xff" * (x_max - x_min + 1)

    mask = Image.frombytes("L", (w, h), bytes(buf)).resize((1024, 1024), Image.Resampling.LANCZOS)
    img.putalpha(mask)

    # Crop to the 768x768 squircle boundaries
    squircle_768 = img.crop((128, 128, 896, 896))

    # Resize to standard 1024x1024 master icon (crisp Lanczos)
    icon_1024 = squircle_768.resize((1024, 1024), Image.Resampling.LANCZOS)
    icon_512 = squircle_768.resize((512, 512), Image.Resampling.LANCZOS)
    icon_256 = squircle_768.resize((256, 256), Image.Resampling.LANCZOS)
    icon_128 = squircle_768.resize((128, 128), Image.Resampling.LANCZOS)
    icon_64 = squircle_768.resize((64, 64), Image.Resampling.LANCZOS)
    icon_32 = squircle_768.resize((32, 32), Image.Resampling.LANCZOS)

    # Save to assets/
    print(f"Exporting assets to {ASSETS_DIR}...")
    icon_1024.save(ASSETS_DIR / "icon-1024.png", "PNG", optimize=True)
    icon_512.save(ASSETS_DIR / "icon.png", "PNG", optimize=True)
    icon_512.save(ASSETS_DIR / "icon-512.png", "PNG", optimize=True)
    icon_256.save(ASSETS_DIR / "icon-256.png", "PNG", optimize=True)
    icon_128.save(ASSETS_DIR / "icon-128.png", "PNG", optimize=True)
    icon_64.save(ASSETS_DIR / "icon-64.png", "PNG", optimize=True)
    icon_32.save(ASSETS_DIR / "favicon.png", "PNG", optimize=True)

    # Multi-resolution ICO
    icon_512.save(
        ASSETS_DIR / "favicon.ico",
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64)],
    )

    # Copy to static web directory
    icon_512.save(STATIC_DIR / "icon.png", "PNG", optimize=True)
    icon_512.save(
        STATIC_DIR / "favicon.ico",
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48)],
    )

    # Copy to artifact directory for markdown display
    icon_512.save(ARTIFACT_DIR / "icon.png", "PNG", optimize=True)

    print("Successfully generated all icon and favicon assets:")
    for path in [
        ASSETS_DIR / "icon.png",
        ASSETS_DIR / "icon-1024.png",
        ASSETS_DIR / "favicon.ico",
        STATIC_DIR / "icon.png",
        ARTIFACT_DIR / "icon.png",
    ]:
        print(f"  -> {path}")


if __name__ == "__main__":
    main()
