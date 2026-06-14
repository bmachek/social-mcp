"""Helpers for the local-folder posting flow.

The MCP server hardlinks files from `images/` into `public/<token>/<filename>`
so an external nginx can serve them under a random, unguessable path. After
publishing the file is moved to `posted/YYYY-MM-DD/<filename>` and the token
directory is unlinked.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import pathlib
import secrets
import shutil
from urllib.parse import quote

log = logging.getLogger("instagram-mcp")

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


class LocalFileError(RuntimeError):
    """Raised for resolution / staging problems before any API call."""


def images_dir(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "images"


def is_video(path: pathlib.Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def public_dir(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "public"


def posted_dir(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "posted"


def _resolve_in_images(data_dir: pathlib.Path, filename: str, allowed_exts: set[str]) -> pathlib.Path:
    if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
        raise LocalFileError(f"invalid filename: {filename!r}")
    src = (images_dir(data_dir) / filename).resolve()
    if not str(src).startswith(str(images_dir(data_dir).resolve()) + os.sep):
        raise LocalFileError(f"path escapes images dir: {filename!r}")
    if not src.is_file():
        raise LocalFileError(f"no such file in images/: {filename!r}")
    if src.suffix.lower() not in allowed_exts:
        raise LocalFileError(
            f"unsupported file extension {src.suffix!r} for {filename!r}; "
            f"allowed: {sorted(allowed_exts)}"
        )
    return src


def resolve_image(data_dir: pathlib.Path, filename: str) -> pathlib.Path:
    """Resolve an image filename against images/ and refuse anything that escapes it."""
    return _resolve_in_images(data_dir, filename, IMAGE_EXTENSIONS)


def resolve_video(data_dir: pathlib.Path, filename: str) -> pathlib.Path:
    """Resolve a video filename against images/ (the inbox holds both)."""
    return _resolve_in_images(data_dir, filename, VIDEO_EXTENSIONS)


def resolve_media(data_dir: pathlib.Path, filename: str) -> pathlib.Path:
    """Resolve any inbox media file (image or video)."""
    return _resolve_in_images(data_dir, filename, MEDIA_EXTENSIONS)


def _list_by_exts(data_dir: pathlib.Path, exts: set[str]) -> list[dict[str, object]]:
    p = images_dir(data_dir)
    if not p.is_dir():
        return []
    out: list[dict[str, object]] = []
    for child in sorted(p.iterdir()):
        if not child.is_file():
            continue
        if child.name.startswith("."):
            continue
        if child.suffix.lower() not in exts:
            continue
        stat = child.stat()
        out.append({
            "name": child.name,
            "size_bytes": stat.st_size,
            "kind": "video" if child.suffix.lower() in VIDEO_EXTENSIONS else "image",
        })
    return out


def list_images(data_dir: pathlib.Path) -> list[dict[str, object]]:
    return _list_by_exts(data_dir, IMAGE_EXTENSIONS)


def list_videos(data_dir: pathlib.Path) -> list[dict[str, object]]:
    return _list_by_exts(data_dir, VIDEO_EXTENSIONS)


def list_media(data_dir: pathlib.Path) -> list[dict[str, object]]:
    """Both images and videos in the inbox, sorted by filename."""
    return _list_by_exts(data_dir, MEDIA_EXTENSIONS)


def stage_for_serving(
    data_dir: pathlib.Path, src: pathlib.Path, base_url: str
) -> tuple[str, pathlib.Path, str]:
    """Hardlink src into public/<token>/<src.name>. Return (token, link, url)."""
    token = secrets.token_urlsafe(24)
    token_dir = public_dir(data_dir) / token
    token_dir.mkdir(parents=True, exist_ok=False)
    link_path = token_dir / src.name
    try:
        os.link(src, link_path)
    except OSError:
        # Fallback for filesystems without hardlink support — copy instead.
        shutil.copy2(src, link_path)
    public_url = f"{base_url.rstrip('/')}/{token}/{quote(src.name, safe='')}"
    return token, link_path, public_url


def unstage(data_dir: pathlib.Path, token: str) -> None:
    token_dir = public_dir(data_dir) / token
    if token_dir.is_dir():
        shutil.rmtree(token_dir, ignore_errors=True)


def archive(data_dir: pathlib.Path, src: pathlib.Path) -> pathlib.Path:
    """Move src into posted/YYYY-MM-DD/, suffixing on collision."""
    today = dt.date.today().isoformat()
    dest_dir = posted_dir(data_dir) / today
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        stem, suffix = src.stem, src.suffix
        for n in range(1, 1000):
            candidate = dest_dir / f"{stem}_{n}{suffix}"
            if not candidate.exists():
                dest = candidate
                break
    shutil.move(str(src), str(dest))
    return dest
