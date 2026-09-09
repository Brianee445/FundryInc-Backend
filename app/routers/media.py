from typing import Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from pydantic import BaseModel

from app.dependencies import require_role
from app.models import User
from app.services.media_storage import upload_founder_media

router = APIRouter(prefix="/api/v1/media", tags=["Media"])

MediaKind = Literal["profile_picture", "gallery", "demo_video"]


class MediaUploadResponse(BaseModel):
    url: str


@router.post("/upload", response_model=MediaUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_media(
    kind: MediaKind = Form(...),
    file: UploadFile = File(...),
    current_user: User = Depends(require_role("founder")),
):
    """
    Uploads a founder's profile picture, a gallery image, or their demo
    video to Storage and returns its public URL — the frontend then saves
    that URL into the founder profile the same way it always handled
    pitch_deck_url/demo_video_url (this endpoint only produces the URL, it
    doesn't touch founder_profiles itself; PUT /founder-profiles/me still
    does that, same as before).
    """
    data = await file.read()
    url = upload_founder_media(
        user_id=str(current_user.id),
        kind=kind,
        content_type=file.content_type or "application/octet-stream",
        data=data,
    )
    return MediaUploadResponse(url=url)
