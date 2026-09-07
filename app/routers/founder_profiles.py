from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user_optional, require_role
from app.models import (
    ConnectionStatusEnum,
    ConnectionRequest,
    FounderProfile,
    SavedFounderProfile,
    StageEnum,
    User,
)
from app.schemas import FounderContactInfo, FounderProfileResponse, FounderProfileUpsert

router = APIRouter(prefix="/api/v1/founder-profiles", tags=["Founder Profiles"])


def _attach_contact_if_visible(profile: FounderProfile, viewer: Optional[User], db: Session) -> FounderProfileResponse:
    """
    Build the response for a single profile, including contact info only
    when the PRD's rules allow it: the founder made it public, the viewer
    *is* the founder, or the viewer is an investor with an accepted
    connection request to this profile.
    """
    response = FounderProfileResponse.model_validate(profile)

    if viewer is not None and viewer.id == profile.user_id:
        response.contact = FounderContactInfo(email=profile.user.email)
        return response

    if profile.contact_visibility == "public":
        response.contact = FounderContactInfo(email=profile.user.email)
        return response

    if viewer is not None:
        accepted = (
            db.query(ConnectionRequest)
            .filter(
                ConnectionRequest.founder_profile_id == profile.id,
                ConnectionRequest.investor_id == viewer.id,
                ConnectionRequest.status == ConnectionStatusEnum.accepted,
            )
            .first()
        )
        if accepted is not None:
            response.contact = FounderContactInfo(email=profile.user.email)

        response.is_saved = (
            db.query(SavedFounderProfile)
            .filter(
                SavedFounderProfile.founder_profile_id == profile.id,
                SavedFounderProfile.investor_id == viewer.id,
            )
            .first()
            is not None
        )

    return response


@router.get("/me", response_model=FounderProfileResponse)
def get_my_profile(
    current_user: User = Depends(require_role("founder")),
    db: Session = Depends(get_db),
):
    profile = db.query(FounderProfile).filter(FounderProfile.user_id == current_user.id).first()
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="You haven't created a profile yet.")
    return _attach_contact_if_visible(profile, current_user, db)


@router.put("/me", response_model=FounderProfileResponse)
def upsert_my_profile(
    payload: FounderProfileUpsert,
    current_user: User = Depends(require_role("founder")),
    db: Session = Depends(get_db),
):
    """Create the founder's profile if none exists yet, otherwise update it in place."""
    profile = db.query(FounderProfile).filter(FounderProfile.user_id == current_user.id).first()

    if profile is None:
        profile = FounderProfile(user_id=current_user.id, stage=StageEnum(payload.stage))
        db.add(profile)

    for field, value in payload.model_dump(exclude={"stage"}).items():
        setattr(profile, field, value)
    profile.stage = StageEnum(payload.stage)

    db.commit()
    db.refresh(profile)
    return _attach_contact_if_visible(profile, current_user, db)


@router.patch("/me/publish", response_model=FounderProfileResponse)
def set_publish_state(
    published: bool,
    current_user: User = Depends(require_role("founder")),
    db: Session = Depends(get_db),
):
    profile = db.query(FounderProfile).filter(FounderProfile.user_id == current_user.id).first()
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Create your profile before publishing it.")

    if published and not profile.startup_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Add a startup name before publishing.")

    profile.published = published
    db.commit()
    db.refresh(profile)
    return _attach_contact_if_visible(profile, current_user, db)


@router.get("", response_model=list[FounderProfileResponse])
def list_founder_profiles(
    sector: Optional[str] = None,
    stage: Optional[str] = None,
    funding_min: Optional[float] = Query(None, description="Minimum funding ask, in USD"),
    funding_max: Optional[float] = Query(None, description="Maximum funding ask, in USD"),
    search: Optional[str] = Query(None, description="Matches against startup name and tagline"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Public investor-facing directory — published profiles only."""
    query = db.query(FounderProfile).filter(FounderProfile.published.is_(True))

    if sector:
        query = query.filter(FounderProfile.sector.ilike(f"%{sector}%"))
    if stage:
        query = query.filter(FounderProfile.stage == stage)
    if funding_min is not None:
        query = query.filter(FounderProfile.funding_ask_max >= funding_min)
    if funding_max is not None:
        query = query.filter(FounderProfile.funding_ask_min <= funding_max)
    if search:
        like = f"%{search}%"
        query = query.filter(
            (FounderProfile.startup_name.ilike(like)) | (FounderProfile.tagline.ilike(like))
        )

    profiles = query.order_by(FounderProfile.created_at.desc()).all()
    return [_attach_contact_if_visible(profile, current_user, db) for profile in profiles]


@router.get("/saved", response_model=list[FounderProfileResponse])
def list_saved_profiles(
    current_user: User = Depends(require_role("investor")),
    db: Session = Depends(get_db),
):
    saved = (
        db.query(FounderProfile)
        .join(SavedFounderProfile, SavedFounderProfile.founder_profile_id == FounderProfile.id)
        .filter(SavedFounderProfile.investor_id == current_user.id)
        .all()
    )
    return [_attach_contact_if_visible(profile, current_user, db) for profile in saved]


@router.get("/{profile_id}", response_model=FounderProfileResponse)
def get_founder_profile(
    profile_id: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    profile = db.query(FounderProfile).filter(FounderProfile.id == profile_id).first()
    is_owner = current_user is not None and profile is not None and current_user.id == profile.user_id
    if profile is None or (not profile.published and not is_owner):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")
    return _attach_contact_if_visible(profile, current_user, db)


@router.post("/{profile_id}/save", status_code=status.HTTP_204_NO_CONTENT)
def save_profile(
    profile_id: str,
    current_user: User = Depends(require_role("investor")),
    db: Session = Depends(get_db),
):
    profile = db.query(FounderProfile).filter(FounderProfile.id == profile_id).first()
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")

    already_saved = (
        db.query(SavedFounderProfile)
        .filter(
            SavedFounderProfile.founder_profile_id == profile_id,
            SavedFounderProfile.investor_id == current_user.id,
        )
        .first()
    )
    if already_saved is None:
        db.add(SavedFounderProfile(founder_profile_id=profile_id, investor_id=current_user.id))
        db.commit()


@router.delete("/{profile_id}/save", status_code=status.HTTP_204_NO_CONTENT)
def unsave_profile(
    profile_id: str,
    current_user: User = Depends(require_role("investor")),
    db: Session = Depends(get_db),
):
    db.query(SavedFounderProfile).filter(
        SavedFounderProfile.founder_profile_id == profile_id,
        SavedFounderProfile.investor_id == current_user.id,
    ).delete()
    db.commit()
