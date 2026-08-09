"""SQLite-backed job queue with a thread pool. Survives restarts:
startup re-queues anything left 'running' by a crash, then drains the backlog.
Delivery is AT LEAST ONCE — a crashed attempt is retried, so handlers must be
idempotent (contract: ops/AT-LEAST-ONCE.md). A failed attempt parks the row
behind a backoff (next_attempt_at) and the queue's own sweeper thread re-offers
it once the wait elapses; retry() is the operator's override for both a parked
row and a dead one, and queue_health() is what /healthz and the ops heartbeat
read so a parked or wedged queue is never invisible."""

import json
import logging
import threading
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
    inquiry_notify,
    notion_sync,
    plutus_recommend,
    presets,
    video,
)

log = logging.getLogger("mise.jobs")

_pool: ThreadPoolExecutor | None = None
_sweeper: threading.Thread | None = None
_sweeper_stop = threading.Event()
MAX_ATTEMPTS = 3
# Delay before the Nth retry, indexed by the attempt that just failed. Bounded and
# modest on purpose — this is a solo-operator queue, not a distributed system. It
# only has to outlive a vendor blip (a Notion 502, a Gmail hiccup); anything longer
# is better surfaced as a failed job in the admin than retried silently for hours.
# The sweeper thread is the clock that picks a parked job back up, so the honest
# gap is "backoff, plus at most one JOB_SWEEP_TICK_SECONDS, minus up to a second
# to SQLite's whole-second datetime()" — call it 59-120s, then 299-360s at the
# defaults. That is why the queue does NOT ride the hourly recurring scheduler:
# an hourly clock would round both steps to "within the hour" and the numbers
# here would be a lie.
RETRY_BACKOFF_SECONDS = (60, 300)
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


def zip_entries(gallery_id: int, assets) -> list:
    """(source_path, archive_name) pairs for a gallery ZIP, de-duplicating the
    archive names build_zip says callers own. Shared by the full-gallery job,
    the favorites/section job, and the small inline builds in public/downloads."""
    src_dir = _gallery_dirs(gallery_id)["original"]
    names: set[str] = set()
    entries = []
    for a in assets:
        name = a["filename"]
        if name in names:
            name = f"{Path(name).stem}_{a['id']}{Path(name).suffix}"
        names.add(name)
        entries.append((src_dir / a["stored"], name))
    return entries


def _h_zip(p: dict) -> None:
    """Full-gallery ZIP of originals — STORE (media doesn't deflate), atomic rename."""
    gid, rev = p["gallery_id"], p["rev"]
    final = zip_path(gid, rev)
    if final.exists():
        return
    assets = db.all_("SELECT * FROM assets WHERE gallery_id=? AND status='ready'", (gid,))
    build_zip(final, zip_entries(gid, assets))
    for old in config.ZIP_DIR.glob(f"g{gid}-r*.zip"):
        if old != final:
            old.unlink(missing_ok=True)


def _h_zip_subset(p: dict) -> None:
    """Favorites / section ZIP that was too big to build in the request path
    (public/downloads._queue_or_build). Same STORED copy as the full-gallery
    build, over the EXPLICIT asset-id list the route hashed the filename from —
    re-querying could pick up a fav added since, and the file would no longer
    match its own content key. `name`/`prune` are server-minted (gN-fav-<hash>
    .zip, gN-sM-<hash>.zip), never client input."""
    gid, ids = p["gallery_id"], p["asset_ids"]
    final = config.ZIP_DIR / p["name"]
    if final.exists() or not ids:
        return
    ph = ",".join("?" * len(ids))  # only ever "?,?,..." placeholders
    rows = db.all_(
        f"SELECT * FROM assets WHERE gallery_id=? AND status='ready' AND id IN ({ph})",
        (gid, *ids),
    )
    order = {aid: n for n, aid in enumerate(ids)}
    build_zip(final, zip_entries(gid, sorted(rows, key=lambda a: order[a["id"]])))
    for old in config.ZIP_DIR.glob(p["prune"]):
        if old != final:
            old.unlink(missing_ok=True)


HANDLERS = {
    "image_derivatives": _h_image,
    "social_crops": _h_crops,
    "video_transcode": _h_video,
    "video_renditions": _h_renditions,
    "zip_build": _h_zip,
    "zip_subset": _h_zip_subset,
    "notion_sync_invoice": lambda p: notion_sync.sync_invoice(p["invoice_id"]),
    "notion_sync_gallery": lambda p: notion_sync.sync_gallery(p["gallery_id"]),
    "notion_sync_inquiry": lambda p: notion_sync.sync_inquiry(p["inquiry_id"]),
    "inquiry_owner_email": lambda p: inquiry_notify.deliver_owner_email(p["inquiry_id"]),
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


def _retry_delay(attempts: int) -> str:
    """SQLite datetime modifier for the wait after `attempts` failed tries."""
    seconds = RETRY_BACKOFF_SECONDS[min(attempts, len(RETRY_BACKOFF_SECONDS)) - 1]
    return f"+{seconds} seconds"


def _due_job_ids() -> list[int]:
    """Queued jobs runnable right now — fresh work plus retries past their backoff."""
    rows = db.all_(
        "SELECT id FROM jobs WHERE status='queued' "
        "AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now')) ORDER BY id"
    )
    return [r["id"] for r in rows]


def _claim(job_id: int) -> "db.sqlite3.Row | None":
    con = db.connect()
    try:
        # The single queued->running gate: it also enforces the retry backoff, so
        # no dispatch path (pool, sweep, restart backlog) can jump a job's wait.
        cur = con.execute(
            "UPDATE jobs SET status='running', attempts=attempts+1, "
            "updated_at=datetime('now') WHERE id=? AND status='queued' "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now'))",
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
    # Parsed INSIDE the try: a corrupt payload is a job failure like any other.
    # Parsed outside, it raised past the recorder and left the row 'running'
    # forever — no retry, no sweep, no log, only a restart.
    payload: dict = {}
    try:
        payload = json.loads(job["payload"])
        HANDLERS[job["kind"]](payload)
        db.run(
            "UPDATE jobs SET status='done', error=NULL, next_attempt_at=NULL, "
            "updated_at=datetime('now') WHERE id=?",
            (job_id,),
        )
        log.info("job %s %s done", job_id, job["kind"])
    except Exception as e:
        status = "queued" if job["attempts"] < MAX_ATTEMPTS else "failed"
        # Park the retry behind a backoff instead of resubmitting it here: an
        # immediate resubmit burned every attempt inside a second, so a vendor
        # blip looked identical to a permanent failure. The sweeper thread picks
        # it back up once the wait elapses, and retry() can force it sooner.
        # datetime('now', NULL) is NULL, so a terminal failure clears the column
        # in the same statement.
        retry_in = _retry_delay(job["attempts"]) if status == "queued" else None
        db.run(
            "UPDATE jobs SET status=?, error=?, updated_at=datetime('now'), "
            "next_attempt_at=datetime('now', ?) WHERE id=?",
            (status, str(e)[:500], retry_in, job_id),
        )
        log.exception(
            "job %s %s attempt %s -> %s%s",
            job_id,
            job["kind"],
            job["attempts"],
            status,
            f" (retry {retry_in})" if retry_in else "",
        )
        # Only primary ingest owns the canonical asset's readiness. Optional
        # derivatives (social crops/renditions) report their own job failure
        # without removing an already delivered asset from every reader.
        if status == "failed" and job["kind"] in PRIMARY_ASSET_JOB_KINDS and "asset_id" in payload:
            db.run("UPDATE assets SET status='failed' WHERE id=?", (payload["asset_id"],))


def retry(job_id: int) -> bool:
    """Force a job to run NOW — the admin Jobs button, and the operator's only
    hands-on control over a stuck queue.

    It has to match TWO states, not just the terminal one. A failed attempt now
    parks the row as queued-with-a-future-next_attempt_at, and _claim refuses a
    parked row on purpose; if retry() only looked for status='failed', pressing
    retry during a backoff window would 404 and Kevin would have no way to jump
    the wait. Clearing next_attempt_at (and the attempt count) is exactly what
    unparks it, so the same statement serves both cases.

    A plain queued row with no next_attempt_at is deliberately NOT matched: it
    has never failed, the pool or the next sweep already owns it, and there is
    no button for it in the admin.
    """
    con = db.connect()
    try:
        cur = con.execute(
            "UPDATE jobs SET status='queued', attempts=0, error=NULL, "
            "next_attempt_at=NULL, updated_at=datetime('now') "
            "WHERE id=? AND (status='failed' OR "
            "(status='queued' AND next_attempt_at IS NOT NULL))",
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


def queue_health() -> dict:
    """The four numbers that tell you whether the queue is actually moving.

    `failed` is the old signal (dead, awaiting a human). `waiting_retry` is the
    limbo the backoff introduced — queued, but deliberately not runnable yet.
    `stuck` is the one that means something is wrong: a retry whose
    next_attempt_at passed over JOB_STUCK_AFTER_SECONDS ago and still has not
    been claimed, so the sweeper thread or the pool is not doing its job (or the
    timestamp itself is bad). `running_stale` is the other wrong: a row _claim
    flipped to 'running' (which is what sets updated_at) more than
    JOB_STUCK_AFTER_SECONDS ago and never moved off it — a worker that died
    mid-handler, or one still grinding a genuinely long transcode. Nothing else
    reports it: sweep() only re-offers queued rows, so a wedged 'running' row
    waits for a restart. Read by /healthz and by the ops heartbeat — a parked or
    wedged queue must never be invisible.

    Deliberately NOT counted as stuck: a never-attempted queued job, however
    old. Two workers chewing through a batch of video transcodes leave fresh
    work queued for an hour as a matter of course, and alerting on that would
    cry wolf on a healthy queue — `jobs_pending` is the number for depth. Same
    reason the running ceiling is generous: it is not a per-job SLA, it is the
    line past which "still working" stops being the likely explanation.
    """
    window = f"-{config.JOB_STUCK_AFTER_SECONDS} seconds"
    row = db.one(
        "SELECT "
        "SUM(status='failed') AS failed, "
        "SUM(status='queued' AND next_attempt_at IS NOT NULL) AS waiting_retry, "
        "SUM(status='queued' AND next_attempt_at IS NOT NULL "
        "    AND next_attempt_at <= datetime('now', ?)) AS stuck, "
        "SUM(status='running' AND updated_at <= datetime('now', ?)) AS running_stale "
        "FROM jobs",
        (window, window),
    )
    return {k: int(row[k] or 0) for k in ("failed", "waiting_retry", "stuck", "running_stale")}


def sweep() -> None:
    """Re-offer every due queued job to the pool — the queue's only self-heal.

    Two ways a durable row strands with the process still up: dispatch found no
    pool (see dispatch()), or a failed attempt parked it behind its backoff.
    Neither is re-drained by anything else short of a restart. Re-submitting a
    job the pool already holds is harmless — _claim is the one queued->running
    gate, so the duplicate just no-ops.
    """
    if not _pool:
        return
    due = _due_job_ids()
    if due:
        dispatch(due)
        log.info("job sweep re-dispatched %d queued jobs", len(due))


def _sweep_loop() -> None:
    """The queue's clock. Reads the tick every pass so it can be tuned live."""
    while not _sweeper_stop.wait(config.JOB_SWEEP_TICK_SECONDS):
        try:
            sweep()
        except Exception:
            log.exception("job sweep failed")


def _start_sweeper() -> None:
    global _sweeper
    if _sweeper and _sweeper.is_alive():
        return
    _sweeper_stop.clear()
    _sweeper = threading.Thread(target=_sweep_loop, name="mise-job-sweep", daemon=True)
    _sweeper.start()


def _stop_sweeper() -> None:
    global _sweeper
    _sweeper_stop.set()
    if _sweeper:
        _sweeper.join(timeout=2)
        _sweeper = None


def pending_count() -> int:
    row = db.one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','running')")
    return row["n"] if row else 0


def start() -> None:
    global _pool
    db.run("UPDATE jobs SET status='queued' WHERE status='running'")
    _pool = ThreadPoolExecutor(max_workers=config.JOB_WORKERS, thread_name_prefix="mise-job")
    backlog = _due_job_ids()
    for job_id in backlog:
        _pool.submit(_execute, job_id)
    if backlog:
        log.info("re-queued %d jobs from previous run", len(backlog))
    # Anything not due yet (a retry parked mid-crash) is the sweeper's job.
    _start_sweeper()
    log.info(
        "job queue up (%s workers, sweep every %ss)",
        config.JOB_WORKERS,
        config.JOB_SWEEP_TICK_SECONDS,
    )


def stop() -> None:
    global _pool
    _stop_sweeper()
    if _pool:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None
