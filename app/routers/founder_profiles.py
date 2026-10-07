from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user_optional, require_role
from app.models import (
    ConnectionStatusEnum,
    ConnectionRequest,
    FounderProfile,
    FounderProfileView,
    SavedFounderProfile,
    StageEnum,
    User,
    VerificationTierEnum,
)
from app.schemas import FounderContactInfo, FounderProfileResponse, FounderProfileUpsert, SpotlightUpdate

router = APIRouter(prefix="/api/v1/founder-profiles", tags=["Founder Profiles"])

# How many startup profiles each plan allows. premium has no cap — the
# trade-off (per the monetization plan) is that each profile auto-expires
# 90 days after being published instead of staying up indefinitely.
PROFILE_LIMITS = {
    VerificationTierEnum.starter: 1,
    VerificationTierEnum.basic: 3,
    VerificationTierEnum.premium: None,  # unlimited
}

PREMIUM_PUBLISH_WINDOW = timedelta(days=90)


def _attach_contact_if_visible(profile: FounderProfile, viewer: Optional[User], db: Session) -> FounderProfileResponse:
    """
    Build the response for a single profile, including contact info only
    when the PRD's rules allow it: the founder made it public, the viewer
    *is* the founder, or the viewer is an investor with an accepted
    connection request to this profile. verification_tier is read off the
    profile's OWNER's account (User.verification_tier), not the profile
    row itself — it's an account-level subscription that covers every
    startup that founder manages.
    """
    response = FounderProfileResponse.model_validate(profile)
    response.verification_tier = profile.user.verification_tier.value

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


def _maybe_auto_unpublish(profile: FounderProfile, db: Session) -> None:
    """
    A premium founder's profile auto-unpublishes 90 days after it was
    last (re)published — see published_until, set in set_publish_state.
    Checked lazily on read rather than via a background job: cheap, and
    nothing downstream needs this enforced faster than "the next time
    anyone looks at it."
    """
    if profile.published and profile.published_until is not None:
        if datetime.now(timezone.utc) >= profile.published_until:
            profile.published = False
            profile.published_until = None
            db.commit()
            db.refresh(profile)


@router.get("/mine", response_model=list[FounderProfileResponse])
def list_my_profiles(
    current_user: User = Depends(require_role("founder")),
    db: Session = Depends(get_db),
):
    """All of the current founder's startup profiles, newest first."""
    profiles = (
        db.query(FounderProfile)
        .filter(FounderProfile.user_id == current_user.id)
        .order_by(FounderProfile.created_at.desc())
        .all()
    )
    for profile in profiles:
        _maybe_auto_unpublish(profile, db)
    return [_attach_contact_if_visible(profile, current_user, db) for profile in profiles]


@router.get("/me", response_model=FounderProfileResponse)
def get_my_profile(
    current_user: User = Depends(require_role("founder")),
    db: Session = Depends(get_db),
):
    """
    Back-compat single-profile accessor — returns the founder's oldest
    (first-created) profile. New frontend code should use GET /mine and
    let the founder pick which profile they're managing; this stays for
    anything not yet updated to the multi-profile UI.
    """
    profile = (
        db.query(FounderProfile)
        .filter(FounderProfile.user_id == current_user.id)
        .order_by(FounderProfile.created_at.asc())
        .first()
    )
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="You haven't created a profile yet.")
    _maybe_auto_unpublish(profile, db)
    return _attach_contact_if_visible(profile, current_user, db)


@router.post("/mine", response_model=FounderProfileResponse, status_code=status.HTTP_201_CREATED)
def create_profile(
    payload: FounderProfileUpsert,
    current_user: User = Depends(require_role("founder")),
    db: Session = Depends(get_db),
):
    """Creates a new startup profile, enforcing the founder's plan limit."""
    existing_count = (
        db.query(FounderProfile).filter(FounderProfile.user_id == current_user.id).count()
    )
    limit = PROFILE_LIMITS[current_user.verification_tier]
    if limit is not None and existing_count >= limit:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Your {current_user.verification_tier.value} plan allows up to {limit} startup profile(s). "
                "Upgrade to add more."
            ),
        )

    profile = FounderProfile(user_id=current_user.id, stage=StageEnum(payload.stage))
    for field, value in payload.model_dump(exclude={"stage"}).items():
        setattr(profile, field, value)

    db.add(profile)
    db.commit()
    db.refresh(profile)
    return _attach_contact_if_visible(profile, current_user, db)


@router.put("/me", response_model=FounderProfileResponse)
def upsert_my_profile(
    payload: FounderProfileUpsert,
    current_user: User = Depends(require_role("founder")),
    db: Session = Depends(get_db),
):
    """
    Back-compat: creates the founder's first profile if none exists yet
    (subject to the plan limit, same as POST /mine), otherwise updates
    their oldest profile in place. New frontend code should use
    PUT /{profile_id} once it has a specific profile to edit.
    """
    profile = (
        db.query(FounderProfile)
        .filter(FounderProfile.user_id == current_user.id)
        .order_by(FounderProfile.created_at.asc())
        .first()
    )

    if profile is None:
        return create_profile(payload, current_user, db)

    for field, value in payload.model_dump(exclude={"stage"}).items():
        setattr(profile, field, value)
    profile.stage = StageEnum(payload.stage)

    db.commit()
    db.refresh(profile)
    return _attach_contact_if_visible(profile, current_user, db)


@router.put("/{profile_id}", response_model=FounderProfileResponse)
def update_profile(
    profile_id: str,
    payload: FounderProfileUpsert,
    current_user: User = Depends(require_role("founder")),
    db: Session = Depends(get_db),
):
    """Updates one of the current founder's own startup profiles."""
    profile = db.query(FounderProfile).filter(FounderProfile.id == profile_id).first()
    if profile is None or profile.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")

    for field, value in payload.model_dump(exclude={"stage"}).items():
        setattr(profile, field, value)
    profile.stage = StageEnum(payload.stage)

    db.commit()
    db.refresh(profile)
    return _attach_contact_if_visible(profile, current_user, db)


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile(
    profile_id: str,
    current_user: User = Depends(require_role("founder")),
    db: Session = Depends(get_db),
):
    profile = db.query(FounderProfile).filter(FounderProfile.id == profile_id).first()
    if profile is None or profile.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")

    db.delete(profile)
    db.commit()


@router.patch("/me/publish", response_model=FounderProfileResponse)
def set_publish_state(
    published: bool,
    current_user: User = Depends(require_role("founder")),
    db: Session = Depends(get_db),
):
    """Back-compat single-profile publish toggle — see PATCH /{profile_id}/publish for the multi-profile version."""
    profile = (
        db.query(FounderProfile)
        .filter(FounderProfile.user_id == current_user.id)
        .order_by(FounderProfile.created_at.asc())
        .first()
    )
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Create your profile before publishing it.")
    return _set_publish_state_on(profile, published, current_user, db)


@router.patch("/{profile_id}/publish", response_model=FounderProfileResponse)
def set_profile_publish_state(
    profile_id: str,
    published: bool,
    current_user: User = Depends(require_role("founder")),
    db: Session = Depends(get_db),
):
    profile = db.query(FounderProfile).filter(FounderProfile.id == profile_id).first()
    if profile is None or profile.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")
    return _set_publish_state_on(profile, published, current_user, db)


def _set_publish_state_on(profile: FounderProfile, published: bool, current_user: User, db: Session) -> FounderProfileResponse:
    if published and not profile.startup_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Add a startup name before publishing.")

    profile.published = published
    if published and current_user.verification_tier == VerificationTierEnum.premium:
        profile.published_until = datetime.now(timezone.utc) + PREMIUM_PUBLISH_WINDOW
    else:
        profile.published_until = None

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
    for profile in profiles:
        _maybe_auto_unpublish(profile, db)
    # Re-filter in case auto-unpublish just dropped one of them.
    profiles = [p for p in profiles if p.published]
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


@router.get("/spotlight/current", response_model=Optional[FounderProfileResponse])
def get_current_spotlight(db: Session = Depends(get_db)):
    """
    Public — powers the marketing site's Spotlight section (PRD 3.5).
    Returns null when no founder is currently spotlighted rather than
    erroring, so the frontend can fall back to a placeholder instead of
    treating "nobody's spotlighted yet" as a failure.
    """
    profile = (
        db.query(FounderProfile)
        .filter(FounderProfile.is_spotlighted.is_(True), FounderProfile.published.is_(True))
        .order_by(FounderProfile.updated_at.desc())
        .first()
    )
    if profile is None:
        return None
    return _attach_contact_if_visible(profile, None, db)


@router.patch("/{profile_id}/spotlight", response_model=FounderProfileResponse)
def set_spotlight(
    profile_id: str,
    payload: SpotlightUpdate,
    current_user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """
    Admin-only. Only one founder is spotlighted at a time — setting this
    profile's flag true clears it on every other profile first, so the
    frontend never has to pick among several.
    """
    profile = db.query(FounderProfile).filter(FounderProfile.id == profile_id).first()
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")

    if payload.is_spotlighted:
        db.query(FounderProfile).filter(FounderProfile.id != profile_id).update(
            {"is_spotlighted": False}, synchronize_session=False
        )
    profile.is_spotlighted = payload.is_spotlighted
    db.commit()
    db.refresh(profile)
    return _attach_contact_if_visible(profile, current_user, db)


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

    if not is_owner:
        db.add(FounderProfileView(founder_profile_id=profile.id, viewer_id=current_user.id if current_user else None))
        db.commit()

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
