#!/usr/bin/env python3
# © Copyright 2025-2026, Query.Farm LLC - https://query.farm
# SPDX-License-Identifier: Apache-2.0

r"""Regenerate this site's brand assets from one master logo.

The master is committed as ``assets/logo-master.png``: the shield mark on
transparency, at the highest resolution we have.  Everything else is derived,
so a new master is a one-command reroll:

    uv run --with pillow python scripts/regenerate_logo_assets.py

Pass ``--master PATH`` to cut the assets from a different source, which also
replaces the committed master.

The master lives at the repo root rather than in ``public``, which Astro copies
verbatim into the deployed site — the source artwork is a build input, not
something to serve to every visitor.

The favicons are letterboxed rather than scaled to fill.  The mark is a
landscape shield and a favicon is a square, so the alternative is cropping the
banner off the bottom of the only place it is small enough to matter.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from PIL import Image

_REPO = Path(__file__).resolve().parent.parent
_MASTER = _REPO / "assets" / "logo-master.png"
_PUBLIC = _REPO / "public"

# The hero renders the mark at 128-160 CSS px tall and it doubles as the
# og:image. 600 is the width the sibling ports serve for the same job, so the
# fleet ships one size rather than five.
_HERO_WIDTH = 600


def _scaled_to_width(logo: Image.Image, width: int) -> Image.Image:
    """Resample *logo* to *width*, preserving aspect ratio."""
    height = round(logo.height * width / logo.width)
    return logo.resize((width, height), Image.LANCZOS)


def _letterboxed(logo: Image.Image, size: int) -> Image.Image:
    """Centre *logo* in a transparent square canvas of *size*, padding not cropping.

    Args:
        logo: The transparent mark.
        size: Side length of the square result.

    Returns:
        A square RGBA image.

    """
    # Inset slightly: a mark that touches the icon's edge reads as clipped.
    fitted = _scaled_to_width(logo, round(size * 0.94))
    if fitted.height > size:
        fitted = fitted.resize((round(fitted.width * size / fitted.height), size), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(fitted, ((size - fitted.width) // 2, (size - fitted.height) // 2))
    return canvas


def _cropped_mark(master: Image.Image) -> Image.Image:
    """Return *master* as RGBA, cropped to the mark's bounding box.

    Args:
        master: The logo, on transparency.

    Returns:
        An RGBA image with no dead margin, so every derived size is tight and
        predictable.

    Raises:
        SystemExit: If the master is fully opaque, which means it is the mark on
            a background rather than on transparency — scaling that would bake
            a white slab into every asset.

    """
    image = master.convert("RGBA")
    if image.getchannel("A").getextrema() == (255, 255):
        raise SystemExit(f"{_MASTER} has no transparency: it is the mark on a background, not a keyed master")
    bbox = image.getbbox()
    return image.crop(bbox) if bbox else image


def main() -> None:
    """Cut every derived asset from the master and report what was written."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--master",
        type=Path,
        default=None,
        help="Source logo on transparency. Replaces the committed master when given.",
    )
    args = parser.parse_args()

    if args.master is not None:
        _MASTER.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.master, _MASTER)
    if not _MASTER.exists():
        parser.error(f"no master at {_MASTER}; pass --master PATH")

    logo = _cropped_mark(Image.open(_MASTER))
    print(f"master {Image.open(_MASTER).size} -> mark {logo.size}")

    _scaled_to_width(logo, _HERO_WIDTH).save(_PUBLIC / "logo-hero.png")
    _letterboxed(logo, 32).save(_PUBLIC / "favicon-32x32.png")
    _letterboxed(logo, 180).save(_PUBLIC / "apple-touch-icon.png")
    _letterboxed(logo, 48).save(_PUBLIC / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])

    for name in ("logo-hero.png", "favicon-32x32.png", "apple-touch-icon.png", "favicon.ico"):
        path = _PUBLIC / name
        print(f"  {name:22} {Image.open(path).size!s:12} {path.stat().st_size // 1024:>4} KiB")


if __name__ == "__main__":
    main()
