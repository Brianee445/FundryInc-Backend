from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_role
from app.models import (
    ConnectionInitiatorEnum,
    ConnectionRequest,
    ConnectionStatusEnum,
    FounderProfile,
    InvestorProfile,
    User,
    VerificationTierEnum,
)
from app.schemas import (
    ConnectionRequestCreate,
    ConnectionRequestCreateToInvestor,
    ConnectionRequestDecision,
    ConnectionRequestResponse,
)

router = APIRouter(prefix="/api/v1/connections", tags=["Connections"])

# Basic-tier founders are capped on INTROS — new connection requests they
# send to investors — not on regular chat once a connection exists. See
# POST /to-investor below. starter/premium have no cap here (starter
# can't send these at all per the product's current plan structure is
# unchanged for starter; premium is simply unlimited).
BASIC_INTRO_MONTHLY_LIMIT = 5
BASIC_INTRO_WINDOW = timedelta(days=30)


def _to_response(connection: ConnectionRequest) -> ConnectionRequestResponse:
    response = ConnectionRequestResponse.model_validate(connection)
    response.startup_name = connection.founder_profile.startup_name
    response.investor_email = connection.investor.email
    return response


def _get_owned_founder_profile_or_404(profile_id, current_user: User, db: Session) -> FounderProfile:
    profile = db.query(FounderProfile).filter(FounderProfile.id == profile_id).first()
    if profile is None or profile.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Startup profile not found.")
    return profile


def _my_founder_profile_ids(current_user: User, db: Session) -> list:
    return [
        row[0]
        for row in db.query(FounderProfile.id).filter(FounderProfile.user_id == current_user.id).all()
    ]


@router.post("", response_model=ConnectionRequestResponse, status_code=status.HTTP_201_CREATED)
def create_connection_request(
    payload: ConnectionRequestCreate,
    current_user: User = Depends(require_role("investor")),
    db: Session = Depends(get_db),
):
    """Investor-initiated — an investor requesting to connect with a founder's specific startup."""
    profile = db.query(FounderProfile).filter(FounderProfile.id == payload.founder_profile_id).first()
    if profile is None or not profile.published:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")

    connection = ConnectionRequest(
        investor_id=current_user.id,
        founder_profile_id=profile.id,
        initiator=ConnectionInitiatorEnum.investor.value,
        message=payload.message,
    )
    db.add(connection)
    try:
        db.commit()
    except IntegrityError:
        # Hits the uq_connection_investor_founder constraint — there's
        # already an open request between this pair (either direction).
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="There's already a connection request between you and this founder.",
        )

    db.refresh(connection)
    return _to_response(connection)


@router.post("/to-investor", response_model=ConnectionRequestResponse, status_code=status.HTTP_201_CREATED)
def create_connection_request_to_investor(
    payload: ConnectionRequestCreateToInvestor,
    current_user: User = Depends(require_role("founder")),
    db: Session = Depends(get_db),
):
    """
    Founder-initiated — the mirror of the investor-initiated endpoint
    above. A founder pitching an investor, from one of their (possibly
    several) startup profiles, rather than waiting to be discovered. Same
    underlying table and unique constraint as the investor-initiated
    path — the pair can only have one open request regardless of who
    started it.

    Basic-tier founders are capped at BASIC_INTRO_MONTHLY_LIMIT of these
    per rolling 30 days, counted across every startup they own (the cap
    is on the founder's account, not per-profile). starter and premium
    are unaffected — see BASIC_INTRO_MONTHLY_LIMIT above.
    """
    founder_profile = _get_owned_founder_profile_or_404(payload.founder_profile_id, current_user, db)

    if current_user.verification_tier == VerificationTierEnum.starter:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Reaching out to investors first requires a Basic or Premium plan. Upgrade to send intro messages.",
        )

    if current_user.verification_tier == VerificationTierEnum.basic:
        my_profile_ids = _my_founder_profile_ids(current_user, db)
        window_start = datetime.now(timezone.utc) - BASIC_INTRO_WINDOW
        sent_this_window = (
            db.query(ConnectionRequest)
            .filter(
                ConnectionRequest.founder_profile_id.in_(my_profile_ids),
                ConnectionRequest.initiator == ConnectionInitiatorEnum.founder.value,
                ConnectionRequest.created_at >= window_start,
            )
            .count()
        )
        if sent_this_window >= BASIC_INTRO_MONTHLY_LIMIT:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Your Basic plan allows {BASIC_INTRO_MONTHLY_LIMIT} introductory messages to investors "
                    "every 30 days. Upgrade to Premium for unlimited messaging."
                ),
            )

    investor_profile = (
        db.query(InvestorProfile)
        .filter(InvestorProfile.user_id == payload.investor_user_id, InvestorProfile.published.is_(True))
        .first()
    )
    if investor_profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investor profile not found.")

    connection = ConnectionRequest(
        investor_id=payload.investor_user_id,
        founder_profile_id=founder_profile.id,
        initiator=ConnectionInitiatorEnum.founder.value,
        message=payload.message,
    )
    db.add(connection)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="There's already a connection request between you and this investor.",
        )

    db.refresh(connection)
    return _to_response(connection)


@router.get("/sent", response_model=list[ConnectionRequestResponse])
def list_sent_requests(
    current_user: User = Depends(require_role("investor", "founder")),
    db: Session = Depends(get_db),
):
    """Requests *this* user initiated, regardless of role. For founders, aggregated across all their startups."""
    if current_user.role == "investor":
        query = db.query(ConnectionRequest).filter(
            ConnectionRequest.investor_id == current_user.id,
            ConnectionRequest.initiator == ConnectionInitiatorEnum.investor.value,
        )
    else:
        my_profile_ids = _my_founder_profile_ids(current_user, db)
        if not my_profile_ids:
            return []
        query = db.query(ConnectionRequest).filter(
            ConnectionRequest.founder_profile_id.in_(my_profile_ids),
            ConnectionRequest.initiator == ConnectionInitiatorEnum.founder.value,
        )

    connections = query.order_by(ConnectionRequest.created_at.desc()).all()
    return [_to_response(c) for c in connections]


@router.get("/received", response_model=list[ConnectionRequestResponse])
def list_received_requests(
    current_user: User = Depends(require_role("investor", "founder")),
    db: Session = Depends(get_db),
):
    """Requests initiated *at* this user by the other side. For founders, aggregated across all their startups."""
    if current_user.role == "founder":
        my_profile_ids = _my_founder_profile_ids(current_user, db)
        if not my_profile_ids:
            return []
        query = db.query(ConnectionRequest).filter(
            ConnectionRequest.founder_profile_id.in_(my_profile_ids),
            ConnectionRequest.initiator == ConnectionInitiatorEnum.investor.value,
        )
    else:
        query = db.query(ConnectionRequest).filter(
            ConnectionRequest.investor_id == current_user.id,
            ConnectionRequest.initiator == ConnectionInitiatorEnum.founder.value,
        )

    connections = query.order_by(ConnectionRequest.created_at.desc()).all()
    return [_to_response(c) for c in connections]


@router.patch("/{connection_id}", response_model=ConnectionRequestResponse)
def decide_connection_request(
    connection_id: str,
    payload: ConnectionRequestDecision,
    current_user: User = Depends(require_role("investor", "founder")),
    db: Session = Depends(get_db),
):
    """Only the side that did NOT initiate a request may accept/decline it."""
    connection = db.query(ConnectionRequest).filter(ConnectionRequest.id == connection_id).first()
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection request not found.")

    if connection.initiator == ConnectionInitiatorEnum.investor.value:
        is_recipient = current_user.role == "founder" and connection.founder_profile.user_id == current_user.id
    else:
        is_recipient = current_user.role == "investor" and connection.investor_id == current_user.id

    if not is_recipient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection request not found.")

    if connection.status != ConnectionStatusEnum.pending:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This request has already been responded to.",
        )

    connection.status = ConnectionStatusEnum(payload.status)
    if connection.status == ConnectionStatusEnum.accepted:
        # Mutual once accepted, regardless of who initiated — both sides'
        # contact_visibility gating checks this same row (see
        # founder_profiles._attach_contact_if_visible and its mirror in
        # investor_profiles.py), so one timestamp here unlocks both.
        connection.contact_revealed_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(connection)
    return _to_response(connection)
