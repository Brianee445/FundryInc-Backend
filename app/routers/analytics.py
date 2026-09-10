from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_role
from app.models import (
    ConnectionRequest,
    ConnectionStatusEnum,
    FounderProfile,
    FounderProfileView,
    Message,
    SavedFounderProfile,
    User,
)
from app.schemas import DailyCount, FounderAnalyticsResponse, InvestorAnalyticsResponse

router = APIRouter(prefix="/api/v1/analytics", tags=["Analytics"])

WINDOW_DAYS = 30


def _daily_series(timestamps: list[datetime]) -> list[DailyCount]:
    """
    Buckets a list of timestamps into one count per calendar day over the
    trailing WINDOW_DAYS, filling in zero-count days so the frontend chart
    doesn't have to — a gap in the data would otherwise render as a
    misleading flat gap instead of an explicit zero.
    """
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=WINDOW_DAYS - 1)

    counts = Counter(ts.date() for ts in timestamps if ts.date() >= start)

    return [
        DailyCount(date=start + timedelta(days=offset), count=counts.get(start + timedelta(days=offset), 0))
        for offset in range(WINDOW_DAYS)
    ]


@router.get("/founder", response_model=FounderAnalyticsResponse)
def founder_analytics(
    current_user: User = Depends(require_role("founder")),
    db: Session = Depends(get_db),
):
    profile = db.query(FounderProfile).filter(FounderProfile.user_id == current_user.id).first()
    if profile is None:
        # No profile yet — everything is zero rather than a 404, since an
        # empty dashboard chart is a perfectly valid state, not an error.
        empty_daily = _daily_series([])
        return FounderAnalyticsResponse(
            profile_views_total=0,
            profile_views_daily=empty_daily,
            connection_requests_total=0,
            connection_requests_pending=0,
            connection_requests_accepted=0,
            connection_requests_declined=0,
            connection_requests_daily=empty_daily,
            saved_by_investors_count=0,
            messages_total=0,
        )

    views = db.query(FounderProfileView).filter(FounderProfileView.founder_profile_id == profile.id).all()
    connections = db.query(ConnectionRequest).filter(ConnectionRequest.founder_profile_id == profile.id).all()
    saved_count = (
        db.query(SavedFounderProfile).filter(SavedFounderProfile.founder_profile_id == profile.id).count()
    )
    messages_total = (
        db.query(Message)
        .join(ConnectionRequest, ConnectionRequest.id == Message.connection_id)
        .filter(ConnectionRequest.founder_profile_id == profile.id, Message.sender_id == current_user.id)
        .count()
    )

    return FounderAnalyticsResponse(
        profile_views_total=len(views),
        profile_views_daily=_daily_series([v.created_at for v in views]),
        connection_requests_total=len(connections),
        connection_requests_pending=sum(1 for c in connections if c.status == ConnectionStatusEnum.pending),
        connection_requests_accepted=sum(1 for c in connections if c.status == ConnectionStatusEnum.accepted),
        connection_requests_declined=sum(1 for c in connections if c.status == ConnectionStatusEnum.declined),
        connection_requests_daily=_daily_series([c.created_at for c in connections]),
        saved_by_investors_count=saved_count,
        messages_total=messages_total,
    )


@router.get("/investor", response_model=InvestorAnalyticsResponse)
def investor_analytics(
    current_user: User = Depends(require_role("investor")),
    db: Session = Depends(get_db),
):
    connections = db.query(ConnectionRequest).filter(ConnectionRequest.investor_id == current_user.id).all()
    saved_count = db.query(SavedFounderProfile).filter(SavedFounderProfile.investor_id == current_user.id).count()
    messages_total = (
        db.query(Message)
        .join(ConnectionRequest, ConnectionRequest.id == Message.connection_id)
        .filter(ConnectionRequest.investor_id == current_user.id, Message.sender_id == current_user.id)
        .count()
    )

    return InvestorAnalyticsResponse(
        connection_requests_sent_total=len(connections),
        connection_requests_pending=sum(1 for c in connections if c.status == ConnectionStatusEnum.pending),
        connection_requests_accepted=sum(1 for c in connections if c.status == ConnectionStatusEnum.accepted),
        connection_requests_declined=sum(1 for c in connections if c.status == ConnectionStatusEnum.declined),
        connection_requests_daily=_daily_series([c.created_at for c in connections]),
        saved_founders_count=saved_count,
        messages_total=messages_total,
    )
