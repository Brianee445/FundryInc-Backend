from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import ConnectionRequest, ConnectionStatusEnum, FounderProfile, Message, User
from app.schemas import MessageCreate, MessageResponse, MessageThreadResponse

router = APIRouter(prefix="/api/v1", tags=["Messages"])


def _authorized_connection(connection_id: str, user: User, db: Session) -> ConnectionRequest:
    """
    Confirms the current user is a participant on this connection request
    and that it's accepted — messaging only unlocks post-acceptance, per
    PRD 3.4's safety-layer intent (early conversation without exposing
    personal contact info immediately).
    """
    connection = db.query(ConnectionRequest).filter(ConnectionRequest.id == connection_id).first()
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found.")

    is_investor_party = user.role == "investor" and connection.investor_id == user.id
    is_founder_party = (
        user.role == "founder"
        and connection.founder_profile is not None
        and connection.founder_profile.user_id == user.id
    )
    if not (is_investor_party or is_founder_party):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a participant on this connection.")

    if connection.status != ConnectionStatusEnum.accepted:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Messaging unlocks once this connection request is accepted.",
        )
    return connection


@router.get("/connections/{connection_id}/messages", response_model=list[MessageResponse])
def list_messages(
    connection_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _authorized_connection(connection_id, current_user, db)

    messages = (
        db.query(Message)
        .filter(Message.connection_id == connection_id)
        .order_by(Message.created_at.asc())
        .all()
    )

    # Mark the other party's messages read now that this user has fetched them.
    unread_ids = [m.id for m in messages if m.sender_id != current_user.id and m.read_at is None]
    if unread_ids:
        db.query(Message).filter(Message.id.in_(unread_ids)).update(
            {"read_at": sa_func.now()}, synchronize_session=False
        )
        db.commit()
        messages = (
            db.query(Message)
            .filter(Message.connection_id == connection_id)
            .order_by(Message.created_at.asc())
            .all()
        )

    return [
        MessageResponse(
            id=m.id,
            connection_id=m.connection_id,
            sender_id=m.sender_id,
            body=m.body,
            created_at=m.created_at,
            read_at=m.read_at,
            is_mine=(m.sender_id == current_user.id),
        )
        for m in messages
    ]


@router.post(
    "/connections/{connection_id}/messages",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
)
def send_message(
    connection_id: str,
    payload: MessageCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _authorized_connection(connection_id, current_user, db)

    message = Message(connection_id=connection_id, sender_id=current_user.id, body=payload.body)
    db.add(message)
    db.commit()
    db.refresh(message)

    return MessageResponse(
        id=message.id,
        connection_id=message.connection_id,
        sender_id=message.sender_id,
        body=message.body,
        created_at=message.created_at,
        read_at=message.read_at,
        is_mine=True,
    )


@router.get("/messages/threads", response_model=list[MessageThreadResponse])
def list_threads(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Every accepted connection this user can message on, with a preview —
    powers a 'Messages' list without loading full history per thread."""
    if current_user.role == "investor":
        connections = (
            db.query(ConnectionRequest)
            .filter(
                ConnectionRequest.investor_id == current_user.id,
                ConnectionRequest.status == ConnectionStatusEnum.accepted,
            )
            .all()
        )
    elif current_user.role == "founder":
        connections = (
            db.query(ConnectionRequest)
            .join(FounderProfile, FounderProfile.id == ConnectionRequest.founder_profile_id)
            .filter(
                FounderProfile.user_id == current_user.id,
                ConnectionRequest.status == ConnectionStatusEnum.accepted,
            )
            .all()
        )
    else:
        connections = []

    threads_with_sort_key: list[tuple[MessageThreadResponse, "datetime"]] = []
    for connection in connections:
        last_message = (
            db.query(Message)
            .filter(Message.connection_id == connection.id)
            .order_by(Message.created_at.desc())
            .first()
        )
        unread_count = (
            db.query(Message)
            .filter(
                Message.connection_id == connection.id,
                Message.sender_id != current_user.id,
                Message.read_at.is_(None),
            )
            .count()
        )
        label = (
            connection.founder_profile.startup_name
            if current_user.role == "investor"
            else connection.investor.email
        )

        thread = MessageThreadResponse(
            connection_id=connection.id,
            counterparty_label=label,
            last_message=last_message.body if last_message else None,
            last_message_at=last_message.created_at if last_message else None,
            unread_count=unread_count,
        )
        sort_key = last_message.created_at if last_message else connection.created_at
        threads_with_sort_key.append((thread, sort_key))

    threads_with_sort_key.sort(key=lambda pair: pair[1], reverse=True)
    return [thread for thread, _ in threads_with_sort_key]
