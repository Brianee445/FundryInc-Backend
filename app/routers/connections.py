from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_role
from app.models import ConnectionRequest, ConnectionStatusEnum, FounderProfile, User
from app.schemas import ConnectionRequestCreate, ConnectionRequestDecision, ConnectionRequestResponse

router = APIRouter(prefix="/api/v1/connections", tags=["Connections"])


def _to_response(connection: ConnectionRequest) -> ConnectionRequestResponse:
    response = ConnectionRequestResponse.model_validate(connection)
    response.startup_name = connection.founder_profile.startup_name
    response.investor_email = connection.investor.email
    return response


@router.post("", response_model=ConnectionRequestResponse, status_code=status.HTTP_201_CREATED)
def create_connection_request(
    payload: ConnectionRequestCreate,
    current_user: User = Depends(require_role("investor")),
    db: Session = Depends(get_db),
):
    profile = db.query(FounderProfile).filter(FounderProfile.id == payload.founder_profile_id).first()
    if profile is None or not profile.published:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")

    connection = ConnectionRequest(
        investor_id=current_user.id,
        founder_profile_id=profile.id,
        message=payload.message,
    )
    db.add(connection)
    try:
        db.commit()
    except IntegrityError:
        # Hits the uq_connection_investor_founder constraint — this investor
        # already has an open request with this founder.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You've already sent a connection request to this founder.",
        )

    db.refresh(connection)
    return _to_response(connection)


@router.get("/sent", response_model=list[ConnectionRequestResponse])
def list_sent_requests(
    current_user: User = Depends(require_role("investor")),
    db: Session = Depends(get_db),
):
    connections = (
        db.query(ConnectionRequest)
        .filter(ConnectionRequest.investor_id == current_user.id)
        .order_by(ConnectionRequest.created_at.desc())
        .all()
    )
    return [_to_response(c) for c in connections]


@router.get("/received", response_model=list[ConnectionRequestResponse])
def list_received_requests(
    current_user: User = Depends(require_role("founder")),
    db: Session = Depends(get_db),
):
    profile = db.query(FounderProfile).filter(FounderProfile.user_id == current_user.id).first()
    if profile is None:
        return []

    connections = (
        db.query(ConnectionRequest)
        .filter(ConnectionRequest.founder_profile_id == profile.id)
        .order_by(ConnectionRequest.created_at.desc())
        .all()
    )
    return [_to_response(c) for c in connections]


@router.patch("/{connection_id}", response_model=ConnectionRequestResponse)
def decide_connection_request(
    connection_id: str,
    payload: ConnectionRequestDecision,
    current_user: User = Depends(require_role("founder")),
    db: Session = Depends(get_db),
):
    connection = db.query(ConnectionRequest).filter(ConnectionRequest.id == connection_id).first()
    if connection is None or connection.founder_profile.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection request not found.")

    if connection.status != ConnectionStatusEnum.pending:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This request has already been responded to.",
        )

    connection.status = ConnectionStatusEnum(payload.status)
    if connection.status == ConnectionStatusEnum.accepted:
        connection.contact_revealed_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(connection)
    return _to_response(connection)
