"""EXIF extraction and JPEG re-encoding for the image MCP resources."""

from __future__ import annotations

import io
import pathlib
from typing import Any

from PIL import ExifTags, Image, ImageOps


def _gps_to_decimal(coords: tuple[Any, Any, Any], ref: str | None) -> float | None:
    try:
        d, m, s = (float(c) for c in coords)
    except (TypeError, ValueError):
        return None
    val = d + m / 60.0 + s / 3600.0
    if ref in ("S", "W"):
        val = -val
    return val


def _stringify(v: Any) -> Any:
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8", errors="replace").strip("\x00 ")
        except Exception:
            return v.hex()
    if isinstance(v, (list, tuple)):
        return [_stringify(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _stringify(val) for k, val in v.items()}
    try:
        import fractions
        if isinstance(v, fractions.Fraction):
            return float(v)
    except Exception:
        pass
    if hasattr(v, "numerator") and hasattr(v, "denominator"):
        try:
            return float(v.numerator) / float(v.denominator) if v.denominator else None
        except Exception:
            return str(v)
    return v


def read_metadata(path: pathlib.Path) -> dict[str, Any]:
    """Return camera/lens/GPS/dimensions parsed from EXIF, plus file basics."""
    with Image.open(path) as im:
        im.load()
        width, height = im.size
        fmt = im.format
        exif = im.getexif() or {}
        # Resolve numeric tag IDs to human names; pull GPS into its own sub-dict.
        flat: dict[str, Any] = {}
        for tag_id, raw in exif.items():
            name = ExifTags.TAGS.get(tag_id, f"Tag{tag_id}")
            if name == "GPSInfo":
                continue
            flat[name] = _stringify(raw)

        # IFD0 only carries Make/Model/Orientation/etc. — the shooting params
        # (FNumber, ExposureTime, ISO, FocalLength, LensModel) live in the
        # EXIF SubIFD and have to be pulled in explicitly.
        if hasattr(exif, "get_ifd"):
            try:
                exif_ifd = exif.get_ifd(ExifTags.IFD.Exif) or {}
            except Exception:
                exif_ifd = {}
            for tag_id, raw in exif_ifd.items():
                name = ExifTags.TAGS.get(tag_id, f"Tag{tag_id}")
                if name == "GPSInfo":
                    continue
                flat.setdefault(name, _stringify(raw))

        gps_raw = exif.get_ifd(ExifTags.IFD.GPSInfo) if hasattr(exif, "get_ifd") else {}
        gps: dict[str, Any] = {}
        for tag_id, raw in (gps_raw or {}).items():
            name = ExifTags.GPSTAGS.get(tag_id, f"GPS{tag_id}")
            gps[name] = _stringify(raw)

        lat = _gps_to_decimal(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef")) if gps.get("GPSLatitude") else None
        lon = _gps_to_decimal(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef")) if gps.get("GPSLongitude") else None

    stat = path.stat()
    return {
        "filename": path.name,
        "size_bytes": stat.st_size,
        "format": fmt,
        "width": width,
        "height": height,
        "camera_make": flat.get("Make"),
        "camera_model": flat.get("Model"),
        "lens_model": flat.get("LensModel"),
        "datetime_original": flat.get("DateTimeOriginal") or flat.get("DateTime"),
        "exposure_time": flat.get("ExposureTime"),
        "f_number": flat.get("FNumber"),
        "iso": flat.get("ISOSpeedRatings") or flat.get("PhotographicSensitivity"),
        "focal_length_mm": flat.get("FocalLength"),
        "focal_length_35mm": flat.get("FocalLengthIn35mmFilm"),
        "orientation": flat.get("Orientation"),
        "software": flat.get("Software"),
        "gps_latitude": lat,
        "gps_longitude": lon,
        "gps_altitude": gps.get("GPSAltitude"),
        "gps_raw": gps or None,
        "exif_raw": flat,
    }


def encode_preview_jpeg(path: pathlib.Path, max_side: int = 2048, quality: int = 88) -> bytes:
    """Open any supported image, auto-rotate via EXIF, downscale, re-encode as JPEG.

    Used to serve the binary MCP resource with a stable image/jpeg mime type.
    """
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        w, h = im.size
        m = max(w, h)
        if m > max_side:
            scale = max_side / m
            im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
