#!/usr/bin/env python3
"""Write AVIF/WebP siblings next to JPEG derivatives that predate them.

New uploads get their siblings from the ingest job. Galleries already on disk
do not — they keep serving JPEG, correctly and forever, because negotiation
falls back when a sibling is missing. This script is how you opt an existing
library in, at a time you choose, without re-running ingest.

Green-light tooling — run on the production host (as the mise user) after pull:

  cd /opt/mise && sudo -u mise .venv/bin/python scripts/backfill-modern-derivatives.py --dry-run
  cd /opt/mise && sudo -u mise .venv/bin/python scripts/backfill-modern-derivatives.py

Idempotent: a derivative that already has every enabled sibling is skipped, so
re-running costs a stat per file. Re-encodes from the JPEG derivative, not the
original — this is a delivery-format change, not a re-process, and reading
2048px JPEGs keeps it cheap enough to run on a live host. It reads no database
rows and writes none; it touches only files in MEDIA_DIR. Ctrl-C is safe (each
file is finished or absent).

Does not touch money, schema migrations, contracts, or the original masters.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from app import config, imaging  # noqa: E402


def jpeg_derivatives(media_dir: Path):
    """Every JPEG under a gallery's web/ or thumb/ directory.

    original/ is deliberately excluded: masters are what the client paid for and
    are never substituted at serve time, so a sibling there would be dead bytes.
    """
    for gallery_dir in sorted(p for p in media_dir.iterdir() if p.is_dir()):
        for variant in ("web", "thumb"):
            variant_dir = gallery_dir / variant
            if not variant_dir.is_dir():
                continue
            yield from sorted(variant_dir.glob("*.jpg"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report what would be written")
    ap.add_argument("--limit", type=int, default=0, help="stop after N derivatives (0 = all)")
    args = ap.parse_args()

    formats = config.MODERN_IMAGE_FORMATS
    if not formats:
        print("MISE_MODERN_IMAGE_FORMATS is empty — nothing to write.")
        return 0
    media_dir = config.MEDIA_DIR
    if not media_dir.is_dir():
        print(f"No media directory at {media_dir}")
        return 1

    print(f"media={media_dir}  formats={','.join(formats)}  dry_run={args.dry_run}")
    scanned = done = skipped = failed = 0
    before = after = 0

    for jpeg in jpeg_derivatives(media_dir):
        if args.limit and done >= args.limit:
            break
        scanned += 1
        missing = [e for e in formats if not jpeg.with_suffix(f".{e}").is_file()]
        if not missing:
            skipped += 1
            continue
        if args.dry_run:
            done += 1
            print(f"  would write {','.join(missing)} for {jpeg.relative_to(media_dir)}")
            continue
        try:
            with Image.open(jpeg) as img:
                img.load()
                written = imaging.write_modern_siblings(img, jpeg, formats=missing)
        except Exception as exc:  # noqa: BLE001 — one bad file must not stop the run
            failed += 1
            print(f"  FAILED {jpeg.relative_to(media_dir)}: {exc}")
            continue
        if not written:
            failed += 1
            continue
        done += 1
        before += jpeg.stat().st_size
        after += min(jpeg.with_suffix(f".{e}").stat().st_size for e in written)
        if done % 100 == 0:
            print(f"  … {done} written")

    print(
        f"scanned={scanned} written={done} already_complete={skipped} failed={failed}"
        + (
            f"  best-case bytes {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB"
            f" ({100 * (1 - after / before):.0f}% smaller)"
            if before and after
            else ""
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
