"""FastMCP server exposing Meta Graph API publishing tools.

Tools:
- post_instagram_photo(image_url, caption, alt_text="")
- post_facebook_photo(image_url, caption, published=True)
- post_instagram_local_photo(filename, caption, alt_text="", also_story=True)
- post_facebook_local_photo(filename, caption, also_story=True, group_ids=None)
- list_pending_images()
- get_image_metadata(filename)
- show_image(filename, max_side=1536)
- check_token_validity()

Resources:
- image://list                JSON listing of pending images
- image://{filename}          JPEG-encoded preview of an image (for Claude to read)

Run directly:    python server.py
Or containerised: see Dockerfile / docker-compose.yml
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
import sys
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image

import exif_reader
import local_files
import scheduler
from local_files import LocalFileError
from meta_client import MetaAPIError, MetaClient


load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("instagram-mcp")


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise SystemExit(
            f"Missing required env var {name}. "
            "Copy .env.example to .env and fill it in."
        )
    return val


def _parse_id_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


META_ACCESS_TOKEN = _require_env("META_ACCESS_TOKEN")
IG_USER_ID = _require_env("IG_USER_ID")
FB_PAGE_ID = _require_env("FB_PAGE_ID")
GRAPH_VERSION = os.getenv("GRAPH_API_VERSION", "v25.0")
META_APP_ID = os.getenv("META_APP_ID")
META_APP_SECRET = os.getenv("META_APP_SECRET")
CONTAINER_TIMEOUT = float(os.getenv("IG_CONTAINER_TIMEOUT_SECONDS", "90"))
CONTAINER_POLL = float(os.getenv("IG_CONTAINER_POLL_SECONDS", "3"))
DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", "/data"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
FB_GROUP_IDS_DEFAULT = _parse_id_list(os.getenv("FB_GROUP_IDS"))
SCHEDULER_DB = pathlib.Path(os.getenv("SCHEDULER_DB", str(DATA_DIR / "scheduler" / "scheduled.db")))
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "3224"))

mcp = FastMCP("instagram-mcp", host=MCP_HOST, port=MCP_PORT)


def _new_client() -> MetaClient:
    return MetaClient(
        access_token=META_ACCESS_TOKEN,
        ig_user_id=IG_USER_ID,
        fb_page_id=FB_PAGE_ID,
        graph_version=GRAPH_VERSION,
        app_id=META_APP_ID,
        app_secret=META_APP_SECRET,
    )


def _err(prefix: str, e: Exception) -> dict[str, Any]:
    if isinstance(e, MetaAPIError):
        return {"ok": False, "error": prefix, "meta_api_error": e.to_dict()}
    return {"ok": False, "error": prefix, "detail": str(e)}


# ---------------------------------------------------------------------------
# Tools — Meta posting
# ---------------------------------------------------------------------------


@mcp.tool()
async def post_instagram_photo(
    image_url: str,
    caption: str = "",
    alt_text: str = "",
) -> dict[str, Any]:
    """Publish a single photo to the configured Instagram Professional account.

    Performs the full two-step Content Publishing flow against a publicly
    reachable HTTPS image URL. Returns the published media id, or a structured
    Meta API error.
    """
    log.info("post_instagram_photo url=%s caption_len=%d", image_url, len(caption))
    async with _new_client() as client:
        try:
            url_info = await client.validate_image_url(image_url)
        except Exception as e:
            return _err("image_url validation failed", e)

        try:
            limit = await client.get_publishing_limit()
            usage = (limit.get("data") or [{}])[0].get("quota_usage")
            if isinstance(usage, int) and usage >= 50:
                return {
                    "ok": False,
                    "error": "rate_limit",
                    "detail": "Instagram 24h publishing quota exhausted (50 posts)",
                    "quota_usage": usage,
                }
        except MetaAPIError as e:
            log.warning("publishing_limit check failed (non-fatal): %s", e)
            usage = None

        try:
            container_id = await client.create_ig_container(
                image_url=image_url,
                caption=caption or None,
                alt_text=alt_text or None,
            )
        except Exception as e:
            return _err("create_container failed", e)

        try:
            status = await client.wait_for_container(
                container_id, timeout_seconds=CONTAINER_TIMEOUT, poll_interval=CONTAINER_POLL,
            )
        except Exception as e:
            return _err(f"container {container_id} did not reach FINISHED", e)

        try:
            published = await client.publish_ig_container(container_id)
        except Exception as e:
            return _err(f"publish failed for container {container_id}", e)

        return {
            "ok": True,
            "media_id": published.get("id"),
            "container_id": container_id,
            "container_final_status": status.status_code,
            "image_url_validation": url_info,
            "publishing_quota_usage": usage,
        }


@mcp.tool()
async def post_facebook_photo(
    image_url: str,
    caption: str = "",
    published: bool = True,
) -> dict[str, Any]:
    """Post a single photo to the configured Facebook Page via a public URL."""
    log.info("post_facebook_photo url=%s published=%s", image_url, published)
    async with _new_client() as client:
        try:
            await client.validate_image_url(image_url)
        except Exception as e:
            return _err("image_url validation failed", e)

        try:
            result = await client.post_fb_page_photo(
                image_url=image_url, caption=caption or None, published=published,
            )
        except Exception as e:
            return _err("facebook page photo post failed", e)

        return {
            "ok": True,
            "photo_id": result.get("id"),
            "post_id": result.get("post_id"),
            "raw": result,
        }


@mcp.tool()
async def check_token_validity() -> dict[str, Any]:
    """Introspect the configured META_ACCESS_TOKEN via /debug_token.

    Also tries to derive a Page Access Token for FB_PAGE_ID via /me/accounts
    and introspects that too — so you can see at a glance whether the user
    token has pages_show_list, whether the page is reachable as that user,
    and which scopes the derived page token actually carries.
    """
    def _to_iso(ts: Any) -> str | None:
        if not isinstance(ts, int) or ts <= 0:
            return None
        return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()

    def _summarise(raw: dict[str, Any], now: int) -> dict[str, Any]:
        data = raw.get("data", {}) if isinstance(raw, dict) else {}
        exp = data.get("expires_at")
        dexp = data.get("data_access_expires_at")
        return {
            "is_valid": data.get("is_valid"),
            "type": data.get("type"),
            "app_id": data.get("app_id"),
            "application": data.get("application"),
            "user_id": data.get("user_id"),
            "profile_id": data.get("profile_id"),
            "scopes": data.get("scopes"),
            "granular_scopes": data.get("granular_scopes"),
            "expires_at_unix": exp,
            "expires_at_iso": _to_iso(exp),
            "expires_in_seconds": (exp - now) if isinstance(exp, int) and exp > 0 else None,
            "data_access_expires_at_unix": dexp,
            "data_access_expires_at_iso": _to_iso(dexp),
        }

    now = int(dt.datetime.now(tz=dt.timezone.utc).timestamp())
    async with _new_client() as client:
        try:
            user_info = await client.debug_token()
        except Exception as e:
            return _err("debug_token (user token) failed", e)

        page_token: str | None = None
        page_token_error: dict[str, Any] | None = None
        page_info_summary: dict[str, Any] | None = None
        try:
            page_token = await client.get_page_access_token()
        except MetaAPIError as e:
            page_token_error = e.to_dict()
        except Exception as e:
            page_token_error = {"message": str(e)}

        if page_token:
            try:
                page_info = await client.debug_token(token_to_check=page_token)
                page_info_summary = _summarise(page_info, now)
            except Exception as e:
                page_token_error = (
                    e.to_dict() if isinstance(e, MetaAPIError) else {"message": str(e)}
                )

        return {
            "ok": True,
            "user_token": _summarise(user_info, now),
            "page_token": page_info_summary,
            "page_token_derivable": page_token is not None,
            "page_token_error": page_token_error,
            "graph_version": GRAPH_VERSION,
            "fb_page_id": FB_PAGE_ID,
            "ig_user_id": IG_USER_ID,
            "fb_groups_configured": FB_GROUP_IDS_DEFAULT,
        }


# ---------------------------------------------------------------------------
# Tools — local file inbox
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_pending_images() -> dict[str, Any]:
    """List image files currently sitting in the local inbox (`data/images/`)."""
    try:
        items = local_files.list_images(DATA_DIR)
    except Exception as e:
        return _err("listing images failed", e)
    return {
        "ok": True,
        "count": len(items),
        "images_dir": str(local_files.images_dir(DATA_DIR)),
        "images": items,
    }


@mcp.tool()
async def get_image_metadata(filename: str) -> dict[str, Any]:
    """Return EXIF + dimensions for an image in the inbox.

    Use this to learn the camera, lens, capture time and GPS coordinates of a
    photo before writing a caption. The raw image bytes are available as the
    MCP resource `image://{filename}` if you also want to look at the picture.
    """
    try:
        src = local_files.resolve_image(DATA_DIR, filename)
    except LocalFileError as e:
        return {"ok": False, "error": str(e)}
    try:
        meta = exif_reader.read_metadata(src)
    except Exception as e:
        return _err("EXIF read failed", e)
    return {"ok": True, **meta}


async def _post_ig_story(client: MetaClient, image_url: str) -> dict[str, Any]:
    try:
        container_id = await client.create_ig_story_container(image_url)
    except Exception as e:
        return _err("ig story container create failed", e)
    try:
        status = await client.wait_for_container(
            container_id, timeout_seconds=CONTAINER_TIMEOUT, poll_interval=CONTAINER_POLL,
        )
    except Exception as e:
        return _err(f"ig story container {container_id} did not finish", e)
    try:
        pub = await client.publish_ig_container(container_id)
    except Exception as e:
        return _err(f"ig story publish failed for container {container_id}", e)
    return {
        "ok": True,
        "media_id": pub.get("id"),
        "container_id": container_id,
        "container_final_status": status.status_code,
    }


async def _post_fb_story_from_file(client: MetaClient, file_path: pathlib.Path) -> dict[str, Any]:
    try:
        unpublished = await client.post_fb_page_photo_file(file_path=file_path, published=False)
    except Exception as e:
        return _err("fb story unpublished upload failed", e)
    photo_id = unpublished.get("id")
    if not photo_id:
        return {"ok": False, "error": "fb story upload missing photo id", "raw": unpublished}
    try:
        story = await client.post_fb_page_story_from_photo(str(photo_id))
    except Exception as e:
        return _err("fb story promote failed", e)
    return {"ok": True, "photo_id": photo_id, "story_id": story.get("id"), "raw": story}


async def _post_to_fb_groups(
    client: MetaClient,
    file_path: pathlib.Path,
    caption: str | None,
    group_ids: list[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for gid in group_ids:
        try:
            res = await client.post_fb_group_photo_file(
                group_id=gid, file_path=file_path, caption=caption,
            )
            out.append({"group_id": gid, "ok": True, "post_id": res.get("post_id") or res.get("id"), "raw": res})
        except MetaAPIError as e:
            out.append({"group_id": gid, "ok": False, "meta_api_error": e.to_dict()})
        except Exception as e:
            out.append({"group_id": gid, "ok": False, "detail": str(e)})
    return out


@mcp.tool()
async def post_instagram_local_photo(
    filename: str,
    caption: str = "",
    alt_text: str = "",
    also_story: bool = True,
) -> dict[str, Any]:
    """Publish a photo from the inbox to Instagram, optionally as a Story too.

    Stages the file behind a one-shot public URL (nginx sidecar), publishes it
    as a feed post, and — if `also_story` is True (default) — additionally
    publishes the same image as an IG Story. On success the source file moves
    to `data/posted/YYYY-MM-DD/`.
    """
    if not PUBLIC_BASE_URL:
        return {"ok": False, "error": "PUBLIC_BASE_URL not configured — set it in .env"}
    try:
        src = local_files.resolve_image(DATA_DIR, filename)
    except LocalFileError as e:
        return {"ok": False, "error": str(e)}

    try:
        token, _link, public_url = local_files.stage_for_serving(DATA_DIR, src, PUBLIC_BASE_URL)
    except Exception as e:
        return _err("staging file for serving failed", e)

    log.info("post_instagram_local_photo file=%s url=%s story=%s", filename, public_url, also_story)
    feed_result: dict[str, Any]
    story_result: dict[str, Any] | None = None
    try:
        feed_result = await post_instagram_photo(public_url, caption=caption, alt_text=alt_text)
        if feed_result.get("ok") and also_story:
            async with _new_client() as client:
                story_result = await _post_ig_story(client, public_url)
    finally:
        local_files.unstage(DATA_DIR, token)

    if feed_result.get("ok"):
        try:
            archived = local_files.archive(DATA_DIR, src)
            feed_result["archived_to"] = str(archived)
        except Exception as e:
            log.warning("archive failed for %s: %s", filename, e)
            feed_result["archive_warning"] = str(e)
        feed_result["source_filename"] = filename
        feed_result["served_url"] = public_url
        if story_result is not None:
            feed_result["story"] = story_result
    return feed_result


@mcp.tool()
async def post_facebook_local_photo(
    filename: str,
    caption: str = "",
    also_story: bool = True,
    group_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Upload a photo from the inbox to the Facebook Page, groups, and Story.

    Posts to: (1) the configured Page, (2) every group in `group_ids` — or
    FB_GROUP_IDS from .env if you pass None, or no groups if you pass `[]`,
    (3) the Page's 24h Story when `also_story=True` (default).

    Returns the Page post id plus a per-group result list and the story result.
    Group/story failures do not abort the Page post — each surfaces its own
    error so you can see exactly which targets succeeded.
    """
    try:
        src = local_files.resolve_image(DATA_DIR, filename)
    except LocalFileError as e:
        return {"ok": False, "error": str(e)}

    targets = FB_GROUP_IDS_DEFAULT if group_ids is None else list(group_ids)
    log.info(
        "post_facebook_local_photo file=%s groups=%s story=%s",
        filename, targets, also_story,
    )

    out: dict[str, Any] = {"ok": False, "source_filename": filename}
    async with _new_client() as client:
        try:
            page_raw = await client.post_fb_page_photo_file(
                file_path=src, caption=caption or None, published=True,
            )
        except Exception as e:
            return _err("facebook page photo upload failed", e)
        out.update({
            "ok": True,
            "photo_id": page_raw.get("id"),
            "post_id": page_raw.get("post_id"),
            "page_raw": page_raw,
        })

        if targets:
            out["groups"] = await _post_to_fb_groups(client, src, caption or None, targets)

        if also_story:
            out["story"] = await _post_fb_story_from_file(client, src)

    try:
        archived = local_files.archive(DATA_DIR, src)
        out["archived_to"] = str(archived)
    except Exception as e:
        log.warning("archive failed for %s: %s", filename, e)
        out["archive_warning"] = str(e)
    return out


# ---------------------------------------------------------------------------
# Tools — carousels (multi-photo posts)
# ---------------------------------------------------------------------------


def _stage_many(srcs: list[pathlib.Path]) -> list[tuple[str, str]]:
    """Stage a list of files for serving. Returns [(token, public_url), ...]."""
    out: list[tuple[str, str]] = []
    for src in srcs:
        token, _link, url = local_files.stage_for_serving(DATA_DIR, src, PUBLIC_BASE_URL)
        out.append((token, url))
    return out


def _unstage_many(tokens: list[str]) -> None:
    for tok in tokens:
        try:
            local_files.unstage(DATA_DIR, tok)
        except Exception:
            pass


@mcp.tool()
async def post_instagram_local_carousel(
    filenames: list[str],
    caption: str = "",
) -> dict[str, Any]:
    """Publish a multi-photo IG carousel from inbox files (2-10 images).

    Builds a child container per slide, then a CAROUSEL parent container, then
    publishes. On success each source file is archived to data/posted/.
    """
    if not PUBLIC_BASE_URL:
        return {"ok": False, "error": "PUBLIC_BASE_URL not configured — set it in .env"}
    if not 2 <= len(filenames) <= 10:
        return {"ok": False, "error": "IG carousel needs between 2 and 10 photos"}

    srcs: list[pathlib.Path] = []
    for fn in filenames:
        try:
            srcs.append(local_files.resolve_image(DATA_DIR, fn))
        except LocalFileError as e:
            return {"ok": False, "error": str(e)}

    try:
        staged = _stage_many(srcs)
    except Exception as e:
        return _err("staging files for serving failed", e)

    tokens = [t for t, _ in staged]
    urls = [u for _, u in staged]

    try:
        async with _new_client() as client:
            child_ids: list[str] = []
            for url in urls:
                child_ids.append(await client.create_ig_carousel_item(url))
            parent_id = await client.create_ig_carousel_container(child_ids, caption=caption or None)
            status = await client.wait_for_container(
                parent_id, timeout_seconds=CONTAINER_TIMEOUT, poll_interval=CONTAINER_POLL,
            )
            published = await client.publish_ig_container(parent_id)
    except Exception as e:
        _unstage_many(tokens)
        return _err("ig carousel publish failed", e)
    finally:
        _unstage_many(tokens)

    out: dict[str, Any] = {
        "ok": True,
        "media_id": published.get("id"),
        "parent_container_id": parent_id,
        "child_container_ids": child_ids,
        "container_final_status": status.status_code,
        "source_filenames": filenames,
    }
    archives: list[str] = []
    warnings: list[str] = []
    for src in srcs:
        try:
            archives.append(str(local_files.archive(DATA_DIR, src)))
        except Exception as e:
            warnings.append(f"{src.name}: {e}")
    out["archived_to"] = archives
    if warnings:
        out["archive_warnings"] = warnings
    return out


@mcp.tool()
async def post_facebook_local_carousel(
    filenames: list[str],
    caption: str = "",
) -> dict[str, Any]:
    """Publish a multi-photo FB Page feed post from inbox files (2-10 images).

    Each photo is uploaded as unpublished to /photos to get its id, then a
    single feed post is created with attached_media[] referencing them.
    """
    if not 2 <= len(filenames) <= 10:
        return {"ok": False, "error": "FB carousel needs between 2 and 10 photos"}

    srcs: list[pathlib.Path] = []
    for fn in filenames:
        try:
            srcs.append(local_files.resolve_image(DATA_DIR, fn))
        except LocalFileError as e:
            return {"ok": False, "error": str(e)}

    async with _new_client() as client:
        photo_ids: list[str] = []
        try:
            for src in srcs:
                raw = await client.post_fb_page_photo_file(file_path=src, published=False)
                pid = raw.get("id")
                if not pid:
                    return {"ok": False, "error": "fb unpublished upload missing id", "raw": raw}
                photo_ids.append(str(pid))
        except Exception as e:
            return _err("fb carousel photo upload failed", e)

        try:
            feed = await client.post_fb_page_multi_photo_feed(photo_ids, message=caption or None)
        except Exception as e:
            return _err("fb carousel feed post failed", e)

    out: dict[str, Any] = {
        "ok": True,
        "post_id": feed.get("id"),
        "photo_ids": photo_ids,
        "source_filenames": filenames,
        "raw": feed,
    }
    archives: list[str] = []
    warnings: list[str] = []
    for src in srcs:
        try:
            archives.append(str(local_files.archive(DATA_DIR, src)))
        except Exception as e:
            warnings.append(f"{src.name}: {e}")
    out["archived_to"] = archives
    if warnings:
        out["archive_warnings"] = warnings
    return out


# ---------------------------------------------------------------------------
# Tools — post history (so Claude can match new photos against prior posts)
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_recent_instagram_posts(
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """Return recent IG media with caption, media_url, permalink and timestamp.

    Use this to give Claude context on past posts so it can pick a thematically
    matching photo from the inbox for a new post. Pass `after` (cursor from
    `paging.cursors.after` in a prior result) to page further back.
    """
    async with _new_client() as client:
        try:
            payload = await client.list_ig_recent_media(limit=limit, after=after)
        except Exception as e:
            return _err("list ig media failed", e)
    return {
        "ok": True,
        "count": len(payload.get("data") or []),
        "posts": payload.get("data") or [],
        "paging": payload.get("paging") or {},
    }


@mcp.tool()
async def list_recent_facebook_posts(
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """Return recent FB Page posts with message, full_picture, permalink, attachments.

    Same use case as list_recent_instagram_posts: feed Claude the prior posts
    so it can suggest a fitting new photo from the inbox.
    """
    async with _new_client() as client:
        try:
            payload = await client.list_fb_page_recent_posts(limit=limit, after=after)
        except Exception as e:
            return _err("list fb posts failed", e)
    return {
        "ok": True,
        "count": len(payload.get("data") or []),
        "posts": payload.get("data") or [],
        "paging": payload.get("paging") or {},
    }


# ---------------------------------------------------------------------------
# Tools — scheduling
# ---------------------------------------------------------------------------


def _schedule(target: str, when: str, filenames: list[str], run_kwargs: dict[str, Any]) -> dict[str, Any]:
    for fn in filenames:
        try:
            local_files.resolve_image(DATA_DIR, fn)
        except LocalFileError as e:
            return {"ok": False, "error": str(e)}
    try:
        run_at = scheduler.parse_when(when)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    job = scheduler.get().add_job(
        scheduler.run_async_job,
        trigger="date",
        run_date=run_at,
        kwargs={"target": target, "kwargs": run_kwargs},
        replace_existing=False,
    )
    return {
        "ok": True,
        "job_id": job.id,
        "target": target,
        "scheduled_for": run_at.isoformat(),
        "run_kwargs": run_kwargs,
    }


@mcp.tool()
async def schedule_instagram_local_photo(
    filename: str,
    when: str,
    caption: str = "",
    alt_text: str = "",
    also_story: bool = True,
) -> dict[str, Any]:
    """Queue an IG feed post for later.

    `when` accepts 'in 30m', 'in 2h', 'in 1d', or an ISO timestamp like
    '2026-06-14T18:00' / '2026-06-14 18:00' — all interpreted in Europe/Berlin
    if the timestamp has no timezone. Returns the job_id you can pass to
    `cancel_scheduled_post`.
    """
    return _schedule(
        "instagram", when, [filename],
        {"filename": filename, "caption": caption, "alt_text": alt_text, "also_story": also_story},
    )


@mcp.tool()
async def schedule_facebook_local_photo(
    filename: str,
    when: str,
    caption: str = "",
    also_story: bool = True,
    group_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Queue a Facebook Page (+ optional groups + story) post for later. See
    schedule_instagram_local_photo for `when` formats.
    """
    return _schedule(
        "facebook", when, [filename],
        {"filename": filename, "caption": caption, "also_story": also_story, "group_ids": group_ids},
    )


@mcp.tool()
async def schedule_instagram_local_carousel(
    filenames: list[str],
    when: str,
    caption: str = "",
) -> dict[str, Any]:
    """Queue an IG carousel (2-10 photos) for later. `when` accepts 'in 2h' or ISO timestamps."""
    if not 2 <= len(filenames) <= 10:
        return {"ok": False, "error": "IG carousel needs between 2 and 10 photos"}
    return _schedule(
        "instagram_carousel", when, filenames,
        {"filenames": filenames, "caption": caption},
    )


@mcp.tool()
async def schedule_facebook_local_carousel(
    filenames: list[str],
    when: str,
    caption: str = "",
) -> dict[str, Any]:
    """Queue a FB multi-photo Page post (2-10 photos) for later."""
    if not 2 <= len(filenames) <= 10:
        return {"ok": False, "error": "FB carousel needs between 2 and 10 photos"}
    return _schedule(
        "facebook_carousel", when, filenames,
        {"filenames": filenames, "caption": caption},
    )


@mcp.tool()
async def list_scheduled_posts() -> dict[str, Any]:
    """Return all jobs currently in the scheduler queue."""
    try:
        jobs = scheduler.get().get_jobs()
    except Exception as e:
        return _err("scheduler unavailable", e)
    out: list[dict[str, Any]] = []
    for j in jobs:
        nrt = j.next_run_time
        out.append({
            "job_id": j.id,
            "next_run_time": nrt.isoformat() if nrt else None,
            "target": (j.kwargs or {}).get("target"),
            "kwargs": (j.kwargs or {}).get("kwargs"),
        })
    return {"ok": True, "count": len(out), "jobs": out}


@mcp.tool()
async def cancel_scheduled_post(job_id: str) -> dict[str, Any]:
    """Cancel a previously-scheduled post by job_id."""
    try:
        scheduler.get().remove_job(job_id)
    except Exception as e:
        return _err(f"could not remove job {job_id}", e)
    return {"ok": True, "removed": job_id}


@mcp.tool()
def show_image(filename: str, max_side: int = 1536) -> Image:
    """Return an inbox image as inline content so Claude sees it this turn.

    Unlike the `image://{filename}` resource (which the client must explicitly
    read), this tool injects the JPEG directly into the next model turn.
    Downscaled to `max_side` px on the long side to keep token cost bounded.
    """
    src = local_files.resolve_image(DATA_DIR, filename)
    data = exif_reader.encode_preview_jpeg(src, max_side=max_side)
    return Image(data=data, format="jpeg")


# ---------------------------------------------------------------------------
# Resources — let Claude read the actual images
# ---------------------------------------------------------------------------


@mcp.resource("image://list", mime_type="application/json")
def images_list_resource() -> str:
    """JSON list of pending images. Mirrors `list_pending_images` but as a resource."""
    items = local_files.list_images(DATA_DIR)
    return json.dumps(
        {
            "count": len(items),
            "images_dir": str(local_files.images_dir(DATA_DIR)),
            "images": items,
        },
        indent=2,
    )


@mcp.resource("image://{filename}", mime_type="image/jpeg")
def image_resource(filename: str) -> bytes:
    """Return a JPEG-encoded preview of an inbox image so Claude can see it.

    Source format (PNG/WebP/HEIC/etc.) is re-encoded to JPEG and downscaled to
    max 2048px on the long side, since the resource template has a single
    fixed mime type. Use `get_image_metadata` for full EXIF on the original.
    """
    src = local_files.resolve_image(DATA_DIR, filename)
    return exif_reader.encode_preview_jpeg(src)


if __name__ == "__main__":
    if MCP_TRANSPORT == "stdio":
        log.info("starting instagram-mcp transport=stdio (graph %s)", GRAPH_VERSION)
    else:
        log.info(
            "starting instagram-mcp transport=%s on %s:%d (graph %s)",
            MCP_TRANSPORT, MCP_HOST, MCP_PORT, GRAPH_VERSION,
        )
    if FB_GROUP_IDS_DEFAULT:
        log.info("FB_GROUP_IDS default: %s", FB_GROUP_IDS_DEFAULT)
    scheduler.init(SCHEDULER_DB)
    mcp.run(transport=MCP_TRANSPORT)
