import mimetypes
import uuid

import requests
from app.config import settings
from fastapi import HTTPException, status

# (extension -> content-type) allow-lists, kept separate per media kind so a
# founder can't e.g. upload a video where a profile picture is expected.
IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
VIDEO_CONTENT_TYPES = {"video/mp4", "video/webm", "video/quicktime"}

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_VIDEO_BYTES = 75 * 1024 * 1024  # 75 MB

_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
}

# Content-types the browser reports when it genuinely doesn't know better —
# not a real answer, just its default. Worth falling back to the filename
# extension for these rather than rejecting the upload outright.
_GENERIC_CONTENT_TYPES = {"", "application/octet-stream", "application/x-www-form-urlencoded"}


def _resolve_content_type(reported_content_type: str, filename: str | None) -> str:
    """
    Many mobile browsers — Android in particular, for camera captures or
    files picked via certain gallery apps — don't set a real MIME type on
    the upload and report a generic fallback instead. Trusting that as-is
    means a perfectly valid JPEG gets rejected as "unsupported file type."
    This guesses from the filename extension whenever the reported type
    looks like a non-answer, and only then falls back to the original value.
    """
    if reported_content_type not in _GENERIC_CONTENT_TYPES:
        return reported_content_type

    if filename:
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            return guessed

    return reported_content_type


def upload_founder_media(*, user_id: str, kind: str, content_type: str, data: bytes, filename: str | None = None) -> str:
    """
    Uploads a file to the configured Supabase Storage bucket under
    {user_id}/{kind}/{random}.{ext} and returns its public URL. Raises
    HTTPException on any validation or upstream failure — callers don't
    need to handle Supabase-specific errors themselves.
    """
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="File uploads aren't configured on this server yet (missing SUPABASE_URL / "
            "SUPABASE_SERVICE_ROLE_KEY).",
        )

    content_type = _resolve_content_type(content_type, filename)

    if kind == "demo_video":
        allowed, max_bytes = VIDEO_CONTENT_TYPES, MAX_VIDEO_BYTES
    else:
        allowed, max_bytes = IMAGE_CONTENT_TYPES, MAX_IMAGE_BYTES

    if content_type not in allowed:
        readable_name = f" ({filename})" if filename else ""
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type for {kind}{readable_name}: {content_type or 'unknown'}",
        )
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File too large — max {max_bytes // (1024 * 1024)}MB for {kind}.",
        )

    ext = _EXT_BY_CONTENT_TYPE[content_type]
    path = f"{user_id}/{kind}/{uuid.uuid4()}.{ext}"
    bucket = settings.supabase_storage_bucket

    upload_url = f"{settings.supabase_url}/storage/v1/object/{bucket}/{path}"
    response = requests.post(
        upload_url,
        headers={
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
            "apikey": settings.supabase_service_role_key,
            "Content-Type": content_type,
            "x-upsert": "true",
        },
        data=data,
        timeout=30,
    )

    if not response.ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Upload to storage failed: {response.status_code} {response.text[:200]}",
        )

    return f"{settings.supabase_url}/storage/v1/object/public/{bucket}/{path}"
