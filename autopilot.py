"""Autopilot: turn an inbox of photos into a 2-week IG+FB posting plan.

Three-stage flow:

1. ``triage(data_dir, …)`` — read inbox, drop near-duplicates via perceptual
   hash, cluster photos by EXIF time + GPS proximity into proposed carousels,
   and propose a posting slot for each item over the next ``days_ahead`` days.
   Returns a JSON-shaped plan with ``caption=None`` placeholders.

2. (Claude looks at each plan item's photos and writes a caption.)

3. ``commit(plan, …)`` — schedule one APScheduler job per plan item via the
   existing scheduler module. Cross-posting is a single combined job target
   (``cross_photo`` / ``cross_carousel`` / ``cross_reel``) that hits both
   platforms and archives the source file once.

Perceptual hash uses imagehash.phash with Hamming distance ≤ ``dedup_hamming``
treated as "same scene, different shutter press" — i.e. burst dupes. Different
angles of the same scene stay separate and surface as a clustered carousel.

Slot allocator places posts at the configured times-of-day (default 12:00 and
19:30 Europe/Berlin), spread evenly across the window. Posts in the past are
never scheduled — the earliest slot is tomorrow's first time-of-day.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import pathlib
import re
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import imagehash
from PIL import Image

import exif_reader
import local_files

log = logging.getLogger("instagram-mcp")
TZ = ZoneInfo("Europe/Berlin")

# Default best-time-of-day slots for a Europe/Berlin audience. These are
# rules-of-thumb (lunch + evening); replace with per-user data once the
# Insights loop has accumulated enough history.
DEFAULT_SLOTS = ("12:00", "19:30")

# imagehash.phash returns a 64-bit hash; Hamming ≤ this = "duplicate burst".
DEFAULT_DEDUP_HAMMING = 5

# Clustering windows for grouping a burst into a single carousel.
DEFAULT_CLUSTER_TIME_MINUTES = 60
DEFAULT_CLUSTER_DISTANCE_METERS = 500

# An IG carousel is hard-capped at 10 slides by Meta.
MAX_CAROUSEL = 10

# Maximum-future window the scheduler will accept — APScheduler tolerates
# anything but Meta's tokens may not, so we cap at 30 days.
MAX_DAYS_AHEAD = 30


# ---------------------------------------------------------------------------
# Internal types
# ---------------------------------------------------------------------------


@dataclass
class _Photo:
    name: str
    path: pathlib.Path
    size_bytes: int
    phash: int | None
    captured_at: dt.datetime | None
    lat: float | None
    lon: float | None
    camera: str | None


@dataclass
class _Video:
    name: str
    path: pathlib.Path
    size_bytes: int


# ---------------------------------------------------------------------------
# EXIF / hash helpers
# ---------------------------------------------------------------------------


_EXIF_DT_RE = re.compile(
    r"(\d{4})[:\-/](\d{1,2})[:\-/](\d{1,2})[ T](\d{1,2}):(\d{2}):(\d{2})"
)


def _parse_exif_datetime(s: Any) -> dt.datetime | None:
    """Parse an EXIF DateTimeOriginal string into an aware Europe/Berlin datetime.

    EXIF timestamps are naive; we treat them as local time at the shooting
    location, which for this user is Europe/Berlin in the overwhelming case.
    """
    if not isinstance(s, str):
        return None
    m = _EXIF_DT_RE.match(s.strip())
    if not m:
        return None
    try:
        y, mo, d, hh, mm, ss = (int(g) for g in m.groups())
        return dt.datetime(y, mo, d, hh, mm, ss, tzinfo=TZ)
    except ValueError:
        return None


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    )
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _read_photo(path: pathlib.Path) -> _Photo | None:
    try:
        meta = exif_reader.read_metadata(path)
    except Exception as e:
        log.warning("autopilot: skipping %s — EXIF read failed: %s", path.name, e)
        return None

    h: int | None = None
    try:
        with Image.open(path) as im:
            h = int(str(imagehash.phash(im, hash_size=8)), 16)
    except Exception as e:
        log.warning("autopilot: %s — phash failed: %s", path.name, e)

    lat = meta.get("gps_latitude")
    lon = meta.get("gps_longitude")
    return _Photo(
        name=path.name,
        path=path,
        size_bytes=path.stat().st_size,
        phash=h,
        captured_at=_parse_exif_datetime(meta.get("datetime_original")),
        lat=lat if isinstance(lat, (int, float)) else None,
        lon=lon if isinstance(lon, (int, float)) else None,
        camera=meta.get("camera_model") if isinstance(meta.get("camera_model"), str) else None,
    )


# ---------------------------------------------------------------------------
# Dedup + cluster
# ---------------------------------------------------------------------------


def _dedupe(
    photos: list[_Photo], threshold: int
) -> tuple[list[_Photo], list[dict[str, Any]]]:
    """Collapse near-duplicate bursts. Keep the largest-file copy per cluster."""
    kept: list[_Photo] = []
    dropped: list[dict[str, Any]] = []
    for p in photos:
        if p.phash is None:
            kept.append(p)
            continue
        match: _Photo | None = None
        for k in kept:
            if k.phash is None:
                continue
            if _hamming(p.phash, k.phash) <= threshold:
                match = k
                break
        if match is None:
            kept.append(p)
            continue
        # Keep the bigger file (typically the higher-quality / less-edited copy).
        if p.size_bytes > match.size_bytes:
            kept.remove(match)
            kept.append(p)
            dropped.append({
                "name": match.name,
                "duplicate_of": p.name,
                "reason": "smaller file size",
            })
        else:
            dropped.append({
                "name": p.name,
                "duplicate_of": match.name,
                "reason": "smaller file size",
            })
    return kept, dropped


def _cluster(
    photos: list[_Photo],
    time_window_minutes: int,
    distance_meters: int,
) -> list[list[_Photo]]:
    """Group photos by EXIF time + GPS into burst-clusters; one pass over a
    time-sorted list."""
    timed = sorted(
        (p for p in photos if p.captured_at is not None),
        key=lambda p: p.captured_at,  # type: ignore[arg-type, return-value]
    )
    untimed = [p for p in photos if p.captured_at is None]

    clusters: list[list[_Photo]] = []
    cur: list[_Photo] = []
    window = time_window_minutes * 60
    for p in timed:
        if not cur:
            cur = [p]
            continue
        last = cur[-1]
        gap = (p.captured_at - last.captured_at).total_seconds()  # type: ignore[union-attr]
        breaks = gap > window
        if not breaks and (
            p.lat is not None and p.lon is not None
            and last.lat is not None and last.lon is not None
        ):
            if _haversine_m(p.lat, p.lon, last.lat, last.lon) > distance_meters:
                breaks = True
        if breaks:
            clusters.append(cur)
            cur = [p]
        else:
            cur.append(p)
    if cur:
        clusters.append(cur)
    # Split oversize clusters into chunks of MAX_CAROUSEL.
    final: list[list[_Photo]] = []
    for cl in clusters:
        if len(cl) <= MAX_CAROUSEL:
            final.append(cl)
        else:
            for i in range(0, len(cl), MAX_CAROUSEL):
                final.append(cl[i:i + MAX_CAROUSEL])
    for p in untimed:
        final.append([p])
    return final


# ---------------------------------------------------------------------------
# Slot allocator
# ---------------------------------------------------------------------------


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def _propose_slots(
    n: int,
    days_ahead: int,
    slots_per_day: list[str],
    now: dt.datetime,
) -> list[dt.datetime]:
    """Spread n slots over the next ``days_ahead`` days at the configured times.

    Starts from tomorrow's first slot so we never schedule into the same day a
    plan is generated (avoids surprise immediate posts). When n exceeds the
    days×slots capacity, falls back to packing day-by-day starting tomorrow.
    """
    if n <= 0 or not slots_per_day:
        return []
    capacity = days_ahead * len(slots_per_day)
    base = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    out: list[dt.datetime] = []

    def slot_at(slot_idx: int) -> dt.datetime:
        day_offset, slot_in_day = divmod(slot_idx, len(slots_per_day))
        h, m = _parse_hhmm(slots_per_day[slot_in_day])
        return (base + dt.timedelta(days=day_offset)).replace(hour=h, minute=m)

    if n > capacity:
        for i in range(n):
            out.append(slot_at(i))
        return out

    # Spread evenly across the full window.
    stride = capacity / n
    for i in range(n):
        out.append(slot_at(int(round(i * stride))))
    # Guard against rounding collisions in the rare case stride < 1.
    seen: set[dt.datetime] = set()
    deduped: list[dt.datetime] = []
    next_idx = 0
    for s in out:
        while s in seen:
            next_idx += 1
            s = slot_at(next_idx)
        seen.add(s)
        deduped.append(s)
    return deduped


# ---------------------------------------------------------------------------
# Top-level: triage
# ---------------------------------------------------------------------------


def triage(
    data_dir: pathlib.Path,
    days_ahead: int = 14,
    max_posts: int = 14,
    slots_per_day: list[str] | None = None,
    dedup_hamming: int = DEFAULT_DEDUP_HAMMING,
    cluster_time_minutes: int = DEFAULT_CLUSTER_TIME_MINUTES,
    cluster_distance_meters: int = DEFAULT_CLUSTER_DISTANCE_METERS,
    cross_post_to_facebook: bool = True,
    include_story: bool = True,
) -> dict[str, Any]:
    """Build a posting plan from the current inbox.

    Returns a dict with ``ok`` / ``summary`` / ``plan`` / ``next_step``. Each
    plan item carries ``caption: null`` for Claude to fill in before calling
    :func:`commit`.
    """
    if days_ahead <= 0 or days_ahead > MAX_DAYS_AHEAD:
        return {"ok": False, "error": f"days_ahead must be in 1..{MAX_DAYS_AHEAD}"}
    if max_posts <= 0:
        return {"ok": False, "error": "max_posts must be >= 1"}

    slots = list(slots_per_day) if slots_per_day else list(DEFAULT_SLOTS)
    for s in slots:
        try:
            _parse_hhmm(s)
        except ValueError:
            return {"ok": False, "error": f"invalid slot time {s!r}; expected HH:MM"}

    inbox = local_files.images_dir(data_dir)
    image_paths: list[pathlib.Path] = []
    video_paths: list[pathlib.Path] = []
    if inbox.is_dir():
        for child in sorted(inbox.iterdir()):
            if not child.is_file() or child.name.startswith("."):
                continue
            ext = child.suffix.lower()
            if ext in local_files.IMAGE_EXTENSIONS:
                image_paths.append(child)
            elif ext in local_files.VIDEO_EXTENSIONS:
                video_paths.append(child)

    photos: list[_Photo] = []
    skipped: list[str] = []
    for p in image_paths:
        ph = _read_photo(p)
        if ph is None:
            skipped.append(p.name)
        else:
            photos.append(ph)

    deduped, dropped = _dedupe(photos, threshold=dedup_hamming)
    clusters = _cluster(
        deduped,
        time_window_minutes=cluster_time_minutes,
        distance_meters=cluster_distance_meters,
    )

    # Photo items (single + carousel), then video items as Reels at the end.
    items_raw: list[dict[str, Any]] = []
    for cl in clusters:
        if len(cl) == 1:
            p = cl[0]
            hint_summary = "1 photo"
            if p.captured_at:
                hint_summary += f" from {p.captured_at.date().isoformat()}"
            items_raw.append({
                "kind": "photo",
                "filenames": [p.name],
                "hints": {
                    "summary": hint_summary,
                    "datetime_original": p.captured_at.isoformat() if p.captured_at else None,
                    "gps": [p.lat, p.lon] if p.lat is not None and p.lon is not None else None,
                    "camera": p.camera,
                },
            })
        else:
            cap = cl[0].captured_at
            last = cl[-1].captured_at
            span_min: int | None = None
            if cap and last:
                span_min = int((last - cap).total_seconds() // 60)
            summary = f"{len(cl)} photos"
            if cap:
                summary += f" from {cap.date().isoformat()}"
            if span_min is not None:
                summary += f" (span {span_min} min)"
            gps_seen = [(p.lat, p.lon) for p in cl if p.lat is not None and p.lon is not None]
            items_raw.append({
                "kind": "carousel",
                "filenames": [p.name for p in cl],
                "hints": {
                    "summary": summary,
                    "datetime_original": cap.isoformat() if cap else None,
                    "gps": list(gps_seen[0]) if gps_seen else None,
                    "camera": cl[0].camera,
                    "span_minutes": span_min,
                },
            })

    for v in video_paths:
        items_raw.append({
            "kind": "reel",
            "filenames": [v.name],
            "hints": {
                "summary": "1 video for Reel",
                "size_bytes": v.stat().st_size,
            },
        })

    # Truncate to max_posts and propose slots.
    items_raw = items_raw[:max_posts]
    now = dt.datetime.now(tz=TZ)
    proposed_slots = _propose_slots(
        n=len(items_raw),
        days_ahead=days_ahead,
        slots_per_day=slots,
        now=now,
    )

    plan: list[dict[str, Any]] = []
    for idx, item in enumerate(items_raw):
        plan.append({
            "id": idx + 1,
            "kind": item["kind"],
            "filenames": item["filenames"],
            "slot_iso": proposed_slots[idx].isoformat() if idx < len(proposed_slots) else None,
            "hints": item["hints"],
            "caption": None,
            "alt_text": None if item["kind"] != "photo" else None,
            "share_to_feed": True if item["kind"] == "reel" else None,
            "also_story": include_story,
            "cross_post_to_facebook": cross_post_to_facebook,
        })

    summary = {
        "inbox_images": len(image_paths),
        "inbox_videos": len(video_paths),
        "photos_after_dedup": len(deduped),
        "duplicates_dropped": dropped,
        "skipped_unreadable": skipped,
        "clusters": len(clusters),
        "plan_items": len(plan),
        "days_ahead": days_ahead,
        "slots_per_day": slots,
        "now_iso": now.isoformat(),
    }

    next_step = (
        "Look at each plan item (use show_image or the image:// resource on the "
        "filenames), write a caption for it, then call autopilot_commit(plan) "
        "with the same list. Items keep going to IG; cross_post_to_facebook=True "
        "also schedules an FB Page cross-post in the same job."
    )
    return {
        "ok": True,
        "summary": summary,
        "plan": plan,
        "next_step": next_step,
    }


# ---------------------------------------------------------------------------
# Top-level: commit
# ---------------------------------------------------------------------------


_VALID_KINDS = {"photo", "carousel", "reel"}


def commit(
    data_dir: pathlib.Path,
    plan: list[dict[str, Any]],
    scheduler_get,  # callable -> APScheduler instance
    run_async_job,  # module-level entrypoint for scheduled jobs
) -> dict[str, Any]:
    """Schedule every item in ``plan``. Validates files + captions first.

    ``scheduler_get`` and ``run_async_job`` are passed in to avoid a circular
    import between autopilot ↔ scheduler.
    """
    if not isinstance(plan, list) or not plan:
        return {"ok": False, "error": "plan must be a non-empty list"}

    # Validate everything up front so partial failures don't leave half a queue.
    errors: list[str] = []
    parsed: list[tuple[dict[str, Any], dt.datetime]] = []
    now = dt.datetime.now(tz=TZ)
    for i, item in enumerate(plan):
        tag = f"item {i + 1}"
        if not isinstance(item, dict):
            errors.append(f"{tag}: not a dict")
            continue
        kind = item.get("kind")
        if kind not in _VALID_KINDS:
            errors.append(f"{tag}: kind must be one of {_VALID_KINDS}, got {kind!r}")
            continue
        filenames = item.get("filenames")
        if not isinstance(filenames, list) or not filenames:
            errors.append(f"{tag}: filenames must be a non-empty list")
            continue
        if kind == "carousel" and not (2 <= len(filenames) <= MAX_CAROUSEL):
            errors.append(f"{tag}: carousel needs 2..{MAX_CAROUSEL} filenames")
            continue
        if kind in ("photo", "reel") and len(filenames) != 1:
            errors.append(f"{tag}: {kind} needs exactly 1 filename")
            continue
        for fn in filenames:
            try:
                if kind == "reel":
                    local_files.resolve_video(data_dir, fn)
                else:
                    local_files.resolve_image(data_dir, fn)
            except local_files.LocalFileError as e:
                errors.append(f"{tag}: {e}")
                break
        else:
            slot_iso = item.get("slot_iso")
            if not isinstance(slot_iso, str):
                errors.append(f"{tag}: slot_iso missing or not a string")
                continue
            try:
                slot = dt.datetime.fromisoformat(slot_iso)
            except ValueError as e:
                errors.append(f"{tag}: invalid slot_iso {slot_iso!r}: {e}")
                continue
            if slot.tzinfo is None:
                slot = slot.replace(tzinfo=TZ)
            if slot <= now:
                errors.append(
                    f"{tag}: slot_iso {slot.isoformat()} is not in the future"
                )
                continue
            caption = item.get("caption")
            if not isinstance(caption, str) or not caption.strip():
                errors.append(f"{tag}: caption must be a non-empty string")
                continue
            parsed.append((item, slot))

    if errors:
        return {"ok": False, "errors": errors, "scheduled": 0}

    scheduled: list[dict[str, Any]] = []
    sched = scheduler_get()
    for item, slot in parsed:
        kind = item["kind"]
        cross = bool(item.get("cross_post_to_facebook", True))
        target = _resolve_target(kind, cross)
        run_kwargs = _build_run_kwargs(kind, cross, item)
        job = sched.add_job(
            run_async_job,
            trigger="date",
            run_date=slot,
            kwargs={"target": target, "kwargs": run_kwargs},
            replace_existing=False,
        )
        scheduled.append({
            "id": item.get("id"),
            "job_id": job.id,
            "target": target,
            "scheduled_for": slot.isoformat(),
            "filenames": item["filenames"],
        })

    return {"ok": True, "scheduled_count": len(scheduled), "scheduled": scheduled}


def _resolve_target(kind: str, cross: bool) -> str:
    if kind == "photo":
        return "cross_photo" if cross else "instagram"
    if kind == "carousel":
        return "cross_carousel" if cross else "instagram_carousel"
    if kind == "reel":
        return "cross_reel" if cross else "instagram_reel"
    raise ValueError(f"unknown kind: {kind}")


def _build_run_kwargs(kind: str, cross: bool, item: dict[str, Any]) -> dict[str, Any]:
    caption = item["caption"]
    also_story = bool(item.get("also_story", True))
    if kind == "photo":
        if cross:
            return {
                "filename": item["filenames"][0],
                "caption": caption,
                "alt_text": item.get("alt_text") or "",
                "also_story_ig": also_story,
                "also_story_fb": also_story,
            }
        return {
            "filename": item["filenames"][0],
            "caption": caption,
            "alt_text": item.get("alt_text") or "",
            "also_story": also_story,
        }
    if kind == "carousel":
        return {"filenames": list(item["filenames"]), "caption": caption}
    if kind == "reel":
        share = bool(item.get("share_to_feed", True))
        if cross:
            return {
                "filename": item["filenames"][0],
                "caption": caption,
                "share_to_feed": share,
            }
        return {
            "filename": item["filenames"][0],
            "caption": caption,
            "share_to_feed": share,
        }
    raise ValueError(f"unknown kind: {kind}")
