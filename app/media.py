"""Download reference files and use Nadou's normal small/chunked upload flow."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import requests

from .browser import BrowserError, BrowserRejected


MAX_MEDIA_BYTES = 200 * 1024 * 1024
CHUNK_THRESHOLD = 5 * 1024 * 1024
CHUNK_BYTES = 10 * 1024 * 1024
MIME_EXTENSIONS = {"audio/mpeg": ".mp3", "audio/wav": ".wav", "image/jpeg": ".jpg",
                   "image/png": ".png", "image/webp": ".webp", "video/mp4": ".mp4",
                   "video/quicktime": ".mov"}


class MediaError(BrowserError):
    pass


def _download(source: str, kind: str, proxy_url: str = "") -> tuple[bytes, str, str]:
    if kind not in {"images", "videos", "audios"}:
        raise MediaError("unsupported reference media kind")
    if source.startswith("data:"):
        header, separator, payload = source.partition(",")
        if not separator or ";base64" not in header:
            raise MediaError("media data URL must be base64 encoded")
        mime = header[5:].split(";", 1)[0].lower()
        if len(payload) > (MAX_MEDIA_BYTES * 4 // 3 + 8):
            raise MediaError("reference media exceeds 200 MB")
        try:
            data = base64.b64decode(payload, validate=True)
        except ValueError as exc:
            raise MediaError("media data URL contains invalid base64") from exc
        filename = kind + MIME_EXTENSIONS.get(mime, mimetypes.guess_extension(mime) or ".bin")
    elif source.startswith(("https://", "http://")):
        parsed = urlsplit(source)
        if not parsed.hostname or parsed.username or parsed.password:
            raise MediaError("media URL is invalid")
        if not proxy_url:
            raise MediaError("account proxy is required for media download")
        proxy = proxy_url.replace("socks5://", "socks5h://", 1)
        session = requests.Session()
        session.trust_env = False
        try:
            with session.get(source, timeout=(15, 90), stream=True, proxies={"http": proxy, "https": proxy},
                             headers={"User-Agent": "Mozilla/5.0"}) as response:
                response.raise_for_status()
                if int(response.headers.get("Content-Length") or 0) > MAX_MEDIA_BYTES:
                    raise MediaError("reference media exceeds 200 MB")
                chunks, size = [], 0
                for chunk in response.iter_content(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_MEDIA_BYTES:
                        raise MediaError("reference media exceeds 200 MB")
                    chunks.append(chunk)
                data = b"".join(chunks)
                mime = response.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].lower()
                filename = Path(unquote(parsed.path)).name or kind + MIME_EXTENSIONS.get(mime, ".bin")
        except requests.RequestException as exc:
            raise MediaError(f"reference download failed: {type(exc).__name__}") from exc
        finally:
            session.close()
    else:
        raise MediaError("media must be an HTTPS URL, HTTP URL, or base64 data URL")
    if not data:
        raise MediaError("reference media is empty")
    if len(data) > MAX_MEDIA_BYTES:
        raise MediaError("reference media exceeds 200 MB")
    prefix = {"images": "image/", "videos": "video/", "audios": "audio/"}[kind]
    if not mime.startswith(prefix):
        guessed = mimetypes.guess_type(filename)[0] or ""
        if not guessed.startswith(prefix) or data.lstrip().startswith((b"<!DOCTYPE", b"<html")):
            raise MediaError(f"{kind} reference has the wrong content type")
        mime = guessed
    return data, mime, filename


def _uploaded_reference(record: dict[str, Any], filename: str, kind: str, size: int) -> dict[str, Any]:
    url = record.get("extranetUrl") or record.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise MediaError("Nadou upload returned no HTTPS material URL")
    metadata = record.get("metadata") or {}
    duration = float(metadata.get("duration") or 0) / 1000
    # Captured file/upload and material/upload_url metadata durations are ms;
    # graph attachments use seconds for both audio and video.
    return {"asset_id": str(record.get("id") or metadata.get("assetId") or ""),
            "name": filename, "url": url, "thumbnailUrl": metadata.get("thumbnailUrl") or record.get("thumbnailUrl") or "",
            "mediumThumbnailUrl": metadata.get("mediumThumbnailUrl") or record.get("mediumThumbnailUrl") or "",
            "width": int(metadata.get("width") or 0), "height": int(metadata.get("height") or 0),
            "frameRate": metadata.get("fps") or 0, "withAudio": bool(metadata.get("isContainAudio")),
            "duration": duration, "size": int(metadata.get("size") or size), "kind": kind}


def upload_media_bytes(data: bytes, mime: str, filename: str, kind: str, *, client: Any) -> dict[str, Any]:
    filename = Path(filename).name
    suffix = Path(filename).suffix.lower().lstrip(".") or MIME_EXTENSIONS.get(mime, ".bin").lstrip(".")
    if not filename or not Path(filename).suffix:
        filename = kind + "." + suffix
    if len(data) < CHUNK_THRESHOLD:
        response = client.request("POST", "/apis/nadouai/file/upload", files={"file": (filename, data, mime)})
        record = response.get("data")
    else:
        request = client.request("POST", "/apis/nadouai/qichuan/file/upload/request",
                                 {"fileType": suffix, "fileSize": len(data)}).get("data") or {}
        upload_url, file_id = request.get("uploadUrl"), request.get("fileId")
        parsed = urlsplit(str(upload_url or ""))
        # The signed third-party upload URL receives neither Nadou cookies nor
        # the browser device header. Never send credentials to another host.
        if parsed.scheme != "https" or parsed.hostname != "intl-upload.iqiyi.com" or not file_id:
            raise MediaError("Nadou upload destination is not recognized")
        upload_session = requests.Session()
        upload_session.trust_env = False
        upload_session.proxies.update(client.session.proxies)
        try:
            for start in range(0, len(data), CHUNK_BYTES):
                chunk = data[start:start + CHUNK_BYTES]
                digest = hashlib.md5(chunk, usedforsecurity=False).hexdigest()
                response = upload_session.post(upload_url, timeout=(15, 120), allow_redirects=False,
                    data={"file_id": file_id, "range": f"{start}-{start + len(chunk) - 1}", "range_md5": digest},
                    files={"file": (filename, chunk, mime)})
                response.raise_for_status()
                receipt = response.json()
                if (receipt.get("file_id") != file_id or receipt.get("range_md5") != digest
                        or not receipt.get("file_range_accepted")):
                    raise MediaError("Nadou upload chunk confirmation does not match the source file")
        except requests.RequestException as exc:
            raise MediaError(f"Nadou chunk upload failed: {type(exc).__name__}") from exc
        finally:
            upload_session.close()
        finished = client.request("POST", "/apis/nadouai/qichuan/file/upload/finish", {"fileId": file_id}).get("data") or {}
        if not finished.get("staticwebPath"):
            raise MediaError("Nadou upload finish returned no material path")
        record = client.request("POST", "/apis/nadouai/material/upload_url", None,
                                params={"url": finished["staticwebPath"]}).get("data")
    if not isinstance(record, dict):
        raise MediaError("Nadou upload returned no material record")
    return _uploaded_reference(record, filename, kind, len(data))


def upload_media(source: str, kind: str, *, client: Any, proxy_url: str = "") -> dict[str, Any]:
    data, mime, filename = _download(source, kind, proxy_url)
    return upload_media_bytes(data, mime, filename, kind, client=client)


def validate_material_durations(normalized: dict[str, Any], assets: dict[str, list[dict[str, Any]]]) -> None:
    family = normalized["family"]
    limits = {"2.0": {"videos": (2, 15.2), "audios": (1, 15)},
              "2.5": {"videos": (2, 30.2), "audios": (2, 30.2)},
              "vidu": {"audios": (3, 12)}, "wan": {"videos": (1, 15), "audios": (1, 15)},
              "kling": {"videos": (3, 30)}, "minimax": {"videos": (2, 15), "audios": (2, 15)}}[family]
    for kind, (minimum, maximum) in limits.items():
        for item in assets[kind]:
            duration = float(item.get("duration") or 0)
            if not minimum <= duration <= maximum:
                raise BrowserRejected(f"{normalized['model']} {kind} reference must be {minimum}–{maximum} seconds; upstream reports {duration:g}")
