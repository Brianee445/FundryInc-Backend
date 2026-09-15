from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user_optional, require_role
from app.models import ConnectionRequest, ConnectionStatusEnum, FounderProfile, InvestorProfile, InvestorTypeEnum, User
from app.schemas import InvestorContactInfo, InvestorProfileResponse, InvestorProfileUpsert

router = APIRouter(prefix="/api/v1/investor-profiles", tags=["Investor Profiles"])


def _attach_contact_if_visible(
    profile: InvestorProfile, viewer: Optional[User], db: Session
) -> InvestorProfileResponse:
    """
    Mirrors founder_profiles._attach_contact_if_visible: contact info is
    included only when the investor made it public, the viewer *is* the
    investor, or the viewer is a founder with an accepted connection to
    this investor (started by either side — see ConnectionRequest.initiator).
    """
    response = InvestorProfileResponse.model_validate(profile)

    if viewer is not None and viewer.id == profile.user_id:
        response.contact = InvestorContactInfo(email=profile.user.email)
        return response

    if profile.contact_visibility == "public":
        response.contact = InvestorContactInfo(email=profile.user.email)
        return response

    if viewer is not None and viewer.role == "founder":
        founder_profile = db.query(FounderProfile).filter(FounderProfile.user_id == viewer.id).first()
        if founder_profile is not None:
            accepted = (
                db.query(ConnectionRequest)
                .filter(
                    ConnectionRequest.investor_id == profile.user_id,
                    ConnectionRequest.founder_profile_id == founder_profile.id,
                    ConnectionRequest.status == ConnectionStatusEnum.accepted,
                )
                .first()
            )
            if accepted is not None:
                response.contact = InvestorContactInfo(email=profile.user.email)

    return response


@router.get("/me", response_model=InvestorProfileResponse)
def get_my_profile(
    current_user: User = Depends(require_role("investor")),
    db: Session = Depends(get_db),
):
    profile = db.query(InvestorProfile).filter(InvestorProfile.user_id == current_user.id).first()
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="You haven't created a profile yet.")
    return _attach_contact_if_visible(profile, current_user, db)


@router.put("/me", response_model=InvestorProfileResponse)
def upsert_my_profile(
    payload: InvestorProfileUpsert,
    current_user: User = Depends(require_role("investor")),
    db: Session = Depends(get_db),
):
    """Create the investor's profile if none exists yet, otherwise update it in place."""
    profile = db.query(InvestorProfile).filter(InvestorProfile.user_id == current_user.id).first()

    if profile is None:
        profile = InvestorProfile(user_id=current_user.id, investor_type=InvestorTypeEnum(payload.investor_type))
        db.add(profile)

    for field, value in payload.model_dump(exclude={"investor_type"}).items():
        setattr(profile, field, value)
    profile.investor_type = InvestorTypeEnum(payload.investor_type)

    db.commit()
    db.refresh(profile)
    return _attach_contact_if_visible(profile, current_user, db)


@router.patch("/me/publish", response_model=InvestorProfileResponse)
def set_publish_state(
    published: bool,
    current_user: User = Depends(require_role("investor")),
    db: Session = Depends(get_db),
):
    """
    This IS the visibility/consent control for the founder-pitches-investor
    feature: an investor's profile is invisible to founders until they
    choose to publish it — same opt-in pattern as a founder's own profile,
    nothing about an investor is discoverable by default.
    """
    profile = db.query(InvestorProfile).filter(InvestorProfile.user_id == current_user.id).first()
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Create your profile before publishing it.")

    profile.published = published
    db.commit()
    db.refresh(profile)
    return _attach_contact_if_visible(profile, current_user, db)


@router.get("", response_model=list[InvestorProfileResponse])
def list_investor_profiles(
    investor_type: Optional[str] = None,
    sector: Optional[str] = Query(None, description="Matches against sectors_of_interest"),
    geography: Optional[str] = Query(None, description="Matches against geographies_of_interest"),
    search: Optional[str] = Query(None, description="Matches against firm name and bio"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Public founder-facing directory — published profiles only, mirrors GET /founder-profiles."""
    query = db.query(InvestorProfile).filter(InvestorProfile.published.is_(True))

    if investor_type:
        query = query.filter(InvestorProfile.investor_type == investor_type)
    if sector:
        query = query.filter(InvestorProfile.sectors_of_interest.any(sector))
    if geography:
        query = query.filter(InvestorProfile.geographies_of_interest.any(geography))
    if search:
        like = f"%{search}%"
        query = query.filter((InvestorProfile.firm_name.ilike(like)) | (InvestorProfile.bio.ilike(like)))

    profiles = query.order_by(InvestorProfile.created_at.desc()).all()
    return [_attach_contact_if_visible(profile, current_user, db) for profile in profiles]


@router.get("/{profile_id}", response_model=InvestorProfileResponse)
def get_investor_profile(
    profile_id: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    profile = db.query(InvestorProfile).filter(InvestorProfile.id == profile_id).first()
    is_owner = current_user is not None and profile is not None and current_user.id == profile.user_id
    if profile is None or (not profile.published and not is_owner):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")

    return _attach_contact_if_visible(profile, current_user, db)
