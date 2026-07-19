"""SQLite-backed job queue with a thread pool. Survives restarts:
startup re-queues anything left 'running' by a crash, then drains the backlog."""

import json
import logging
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import (
    argus_analyze,
    argus_writeback,
    brand_kits,
    config,
    db,
    imaging,
    notion_sync,
    plutus_recommend,
    presets,
    video,
)

log = logging.getLogger("mise.jobs")

_pool: ThreadPoolExecutor | None = None
MAX_ATTEMPTS = 3
PRIMARY_ASSET_JOB_KINDS = frozenset({"image_derivatives", "video_transcode"})


# ── handlers ───────────────────────────────────────────────────────────────


def _gallery_dirs(gallery_id: int) -> dict[str, Path]:
    base = config.MEDIA_DIR / str(gallery_id)
    return {k: base / k for k in ("original", "web", "thumb")}


def _h_image(p: dict) -> None:
    asset = db.one("SELECT * FROM assets WHERE id=?", (p["asset_id"],))
    if not asset:
        return
    dirs = _gallery_dirs(asset["gallery_id"])
    src = dirs["original"] / asset["stored"]
    base = Path(asset["stored"]).stem
    w, h = imaging.make_derivatives(
        str(src),
        str(dirs["web"] / f"{base}.jpg"),
        str(dirs["thumb"] / f"{base}.jpg"),
        config.WEB_MAX_PX,
        config.THUMB_MAX_PX,
        config.JPEG_QUALITY,
    )
    db.run("UPDATE assets SET status='ready', width=?, height=? WHERE id=?", (w, h, asset["id"]))


def _h_video(p: dict) -> None:
    asset = db.one("SELECT * FROM assets WHERE id=?", (p["asset_id"],))
    if not asset:
        return
    dirs = _gallery_dirs(asset["gallery_id"])
    src = dirs["original"] / asset["stored"]
    base = Path(asset["stored"]).stem
    web_mp4 = dirs["web"] / f"{base}.mp4"
    poster = dirs["web"] / f"{base}.jpg"
    info = video.transcode(
        str(src), str(web_mp4), str(poster), config.VIDEO_MAX_W, config.VIDEO_CRF
    )
    imaging.make_derivatives(
        str(poster),
        str(dirs["web"] / f"{base}_poster.jpg"),
        str(dirs["thumb"] / f"{base}.jpg"),
        config.WEB_MAX_PX,
        config.THUMB_MAX_PX,
        config.JPEG_QUALITY,
    )
    db.run(
        "UPDATE assets SET status='ready', width=?, height=?, duration=? WHERE id=?",
        (info["width"], info["height"], info["duration"], asset["id"]),
    )


def renditions_dir(gallery_id: int) -> Path:
    return config.MEDIA_DIR / str(gallery_id) / "renditions"


def _h_renditions(p: dict) -> None:
    """Social-cut renditions (9:16 / 1:1) for a ready video — renders every
    pending asset_renditions row for the asset from the camera original (full
    quality source; the web proxy is already downscaled). Per-row try/except:
    one failed encode marks that row failed and moves on rather than aborting
    the sibling preset — failures stay visible as admin chips, and the build
    button re-queues failed rows."""
    asset = db.one(
        "SELECT * FROM assets WHERE id=? AND kind='video' AND status='ready'", (p["asset_id"],)
    )
    if not asset:
        return
    out_dir = renditions_dir(asset["gallery_id"])
    out_dir.mkdir(parents=True, exist_ok=True)
    src = _gallery_dirs(asset["gallery_id"])["original"] / asset["stored"]
    rows = db.all_(
        "SELECT * FROM asset_renditions WHERE asset_id=? AND status='pending'", (asset["id"],)
    )
    for r in rows:
        out = out_dir / r["stored"]
        try:
            info = video.rendition(str(src), str(out), r["preset"], config.VIDEO_CRF)
        except Exception:
            log.exception(
                "rendition %s (%s) failed for asset %s", r["id"], r["preset"], asset["id"]
            )
            db.run("UPDATE asset_renditions SET status='failed' WHERE id=?", (r["id"],))
            continue
        db.run(
            "UPDATE asset_renditions SET status='ready', width=?, height=?, bytes=? WHERE id=?",
            (info["width"], info["height"], out.stat().st_size, r["id"]),
        )


def crops_dir(gallery_id: int) -> Path:
    return config.MEDIA_DIR / str(gallery_id) / "crops"


def _h_crops(p: dict) -> None:
    """Social crops (1:1/4:5/9:16) for a favorited photo — idempotent by file existence."""
    asset = db.one(
        "SELECT * FROM assets WHERE id=? AND kind='photo' AND status='ready'", (p["asset_id"],)
    )
    if not asset:
        return
    out = crops_dir(asset["gallery_id"])
    stem = Path(asset["stored"]).stem
    active = presets.active()
    if all((out / f"{stem}_{ps['slug']}.jpg").is_file() for ps in active):
        return
    out.mkdir(parents=True, exist_ok=True)
    src = _gallery_dirs(asset["gallery_id"])["original"] / asset["stored"]
    gal = db.one("SELECT client_id FROM galleries WHERE id=?", (asset["gallery_id"],))
    overlay = brand_kits.overlay_for_client(gal["client_id"] if gal else None)
    imaging.make_crops(str(src), out, stem, config.JPEG_QUALITY, active, overlay=overlay)


def zip_path(gallery_id: int, rev: int) -> Path:
    return config.ZIP_DIR / f"g{gallery_id}-r{rev}.zip"


def build_zip(out: Path, entries) -> None:
    """Atomically write a STORED zip (media is already compressed) at `out` from an
    iterable of (source_path, archive_name) pairs. Goes via a .part temp + rename so
    a reader never sees a half-built archive. Callers own arcname de-duplication."""
    tmp = out.with_suffix(".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
        for src, arc in entries:
            zf.write(src, arcname=arc)
    tmp.rename(out)


def _h_zip(p: dict) -> None:
    """Full-gallery ZIP of originals — STORE (media doesn't deflate), atomic rename."""
    gid, rev = p["gallery_id"], p["rev"]
    final = zip_path(gid, rev)
    if final.exists():
        return
    assets = db.all_("SELECT * FROM assets WHERE gallery_id=? AND status='ready'", (gid,))
    src_dir = _gallery_dirs(gid)["original"]
    names: set[str] = set()
    entries = []
    for a in assets:
        name = a["filename"]
        if name in names:
            name = f"{Path(name).stem}_{a['id']}{Path(name).suffix}"
        names.add(name)
        entries.append((src_dir / a["stored"], name))
    build_zip(final, entries)
    for old in config.ZIP_DIR.glob(f"g{gid}-r*.zip"):
        if old != final:
            old.unlink(missing_ok=True)


HANDLERS = {
    "image_derivatives": _h_image,
    "social_crops": _h_crops,
    "video_transcode": _h_video,
    "video_renditions": _h_renditions,
    "zip_build": _h_zip,
    "notion_sync_invoice": lambda p: notion_sync.sync_invoice(p["invoice_id"]),
    "notion_sync_gallery": lambda p: notion_sync.sync_gallery(p["gallery_id"]),
    "notion_sync_inquiry": lambda p: notion_sync.sync_inquiry(p["inquiry_id"]),
    "argus_analyze_gallery": lambda p: argus_analyze.run_for_gallery(
        p["gallery_id"], skip_dedup=bool(p.get("skip_dedup"))
    ),
    "argus_writeback_gallery": lambda p: argus_writeback.run_for_gallery(
        p["gallery_id"], int(p["run_id"])
    ),
    "plutus_recommend_gallery": lambda p: plutus_recommend.run_for_gallery(p["gallery_id"]),
}


# ── queue machinery ────────────────────────────────────────────────────────


def stage(con, kind: str, payload: dict) -> int:
    """Insert a queued job in the caller's transaction without dispatching it."""
    return con.execute(
        "INSERT INTO jobs (kind, payload) VALUES (?,?)", (kind, json.dumps(payload))
    ).lastrowid


def dispatch(job_ids: list[int]) -> None:
    """Offer committed jobs to the live pool; durable rows survive no pool/restart."""
    pool = _pool
    if not pool:
        return
    for offset, job_id in enumerate(job_ids):
        try:
            pool.submit(_execute, job_id)
        except RuntimeError:
            log.warning("job pool unavailable; %d queued jobs remain", len(job_ids) - offset)
            break


def enqueue(kind: str, payload: dict) -> int:
    with db.tx() as con:
        job_id = stage(con, kind, payload)
    dispatch([job_id])
    return job_id


def _claim(job_id: int) -> "db.sqlite3.Row | None":
    con = db.connect()
    try:
        cur = con.execute(
            "UPDATE jobs SET status='running', attempts=attempts+1, "
            "updated_at=datetime('now') WHERE id=? AND status='queued'",
            (job_id,),
        )
        con.commit()
        if cur.rowcount != 1:
            return None
        return con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        con.close()


def _execute(job_id: int) -> None:
    job = _claim(job_id)
    if not job:
        return
    payload = json.loads(job["payload"])
    try:
        HANDLERS[job["kind"]](payload)
        db.run(
            "UPDATE jobs SET status='done', error=NULL, updated_at=datetime('now') WHERE id=?",
            (job_id,),
        )
        log.info("job %s %s done", job_id, job["kind"])
    except Exception as e:
        status = "queued" if job["attempts"] < MAX_ATTEMPTS else "failed"
        db.run(
            "UPDATE jobs SET status=?, error=?, updated_at=datetime('now') WHERE id=?",
            (status, str(e)[:500], job_id),
        )
        log.exception("job %s %s attempt %s -> %s", job_id, job["kind"], job["attempts"], status)
        # Only primary ingest owns the canonical asset's readiness. Optional
        # derivatives (social crops/renditions) report their own job failure
        # without removing an already delivered asset from every reader.
        if status == "failed" and job["kind"] in PRIMARY_ASSET_JOB_KINDS and "asset_id" in payload:
            db.run("UPDATE assets SET status='failed' WHERE id=?", (payload["asset_id"],))
        if status == "queued" and _pool:
            _pool.submit(_execute, job_id)


def retry(job_id: int) -> bool:
    con = db.connect()
    try:
        cur = con.execute(
            "UPDATE jobs SET status='queued', attempts=0, error=NULL, "
            "updated_at=datetime('now') WHERE id=? AND status='failed'",
            (job_id,),
        )
        con.commit()
    finally:
        con.close()
    if cur.rowcount != 1:
        return False
    log.info("job %s retried by admin", job_id)
    if _pool:
        _pool.submit(_execute, job_id)
    return True


def pending_count() -> int:
    row = db.one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','running')")
    return row["n"] if row else 0


def start() -> None:
    global _pool
    db.run("UPDATE jobs SET status='queued' WHERE status='running'")
    _pool = ThreadPoolExecutor(max_workers=config.JOB_WORKERS, thread_name_prefix="mise-job")
    backlog = db.all_("SELECT id FROM jobs WHERE status='queued' ORDER BY id")
    for row in backlog:
        _pool.submit(_execute, row["id"])
    if backlog:
        log.info("re-queued %d jobs from previous run", len(backlog))


def stop() -> None:
    global _pool
    if _pool:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None
