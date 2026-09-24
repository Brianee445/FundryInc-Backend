"""
Founder premium billing via Bachs.

Flow:
  1. Founder hits POST /api/v1/billing/checkout -> we create a Bachs
     checkout session against the recurring premium product and return
     the hosted checkout URL. Frontend redirects the browser there.
  2. Founder pays on Bachs' hosted page. Bachs redirects back to
     frontend_billing_return_url — that redirect is UX only, it never
     grants premium by itself.
  3. Bachs POSTs collection.succeeded (and later invoice.paid on renewal,
     subscription.canceled on cancellation) to
     POST /api/v1/billing/webhook. That webhook is the only thing that
     ever upgrades verification_tier — per Bachs' own guidance, webhooks
     are the source of truth for fulfilment, never the client-side
     redirect.
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import require_role
from app.models import (
    BillingEvent,
    FounderProfile,
    FounderSubscription,
    SubscriptionPlanEnum,
    SubscriptionStatusEnum,
    User,
    VerificationTierEnum,
)
from app.schemas import CheckoutRequest, CheckoutResponse, SubscriptionStatusResponse
from app.services import bachs
from app.services.webhook_verify import verify_signature

router = APIRouter(prefix="/api/v1/billing", tags=["Billing"])


def _product_id_for_plan(plan: str) -> str:
    if plan == SubscriptionPlanEnum.annual.value:
        return settings.bachs_premium_annual_product_id
    return settings.bachs_premium_monthly_product_id


@router.post("/checkout", response_model=CheckoutResponse)
def create_checkout(
    payload: CheckoutRequest,
    current_user: User = Depends(require_role("founder")),
    db: Session = Depends(get_db),
):
    """Founder-only. Starts a Bachs checkout for the premium (gold) verification plan."""
    profile = db.query(FounderProfile).filter(FounderProfile.user_id == current_user.id).first()
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Create your profile before upgrading.")

    if profile.verification_tier != VerificationTierEnum.basic:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You already have an active premium plan.")

    product_id = _product_id_for_plan(payload.plan)
    if not product_id:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Billing is not configured yet.")

    try:
        session = bachs.create_checkout_session(
            product_id=product_id,
            customer_email=current_user.email,
            metadata={"founder_profile_id": str(profile.id), "plan": payload.plan},
        )
    except bachs.BachsError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    checkout_url = session.get("checkout_url") or session.get("url")
    if not checkout_url:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Bachs did not return a checkout URL.")

    # Record the pending subscription now, keyed by the checkout session id,
    # so the webhook has a row to attach the real bachs_subscription_id to
    # once payment completes. Left at status=active with no period end
    # until the webhook fills those in — reads of verification_tier never
    # look at this row directly, only at the profile flag it sets.
    existing = db.query(FounderSubscription).filter(FounderSubscription.founder_profile_id == profile.id).first()
    if existing is None:
        existing = FounderSubscription(founder_profile_id=profile.id, plan=SubscriptionPlanEnum(payload.plan))
        db.add(existing)
    else:
        existing.plan = SubscriptionPlanEnum(payload.plan)
    existing.bachs_checkout_session_id = session.get("id")
    existing.currency = "NGN"
    db.commit()

    return CheckoutResponse(checkout_url=checkout_url)


@router.get("/subscription", response_model=SubscriptionStatusResponse)
def get_my_subscription(
    current_user: User = Depends(require_role("founder")),
    db: Session = Depends(get_db),
):
    profile = db.query(FounderProfile).filter(FounderProfile.user_id == current_user.id).first()
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Create your profile first.")

    sub = db.query(FounderSubscription).filter(FounderSubscription.founder_profile_id == profile.id).first()
    return SubscriptionStatusResponse(
        verification_tier=profile.verification_tier.value,
        plan=sub.plan.value if sub else None,
        status=sub.status.value if sub else None,
        current_period_end=sub.current_period_end if sub else None,
    )


@router.post("/cancel", status_code=status.HTTP_204_NO_CONTENT)
def cancel_my_subscription(
    current_user: User = Depends(require_role("founder")),
    db: Session = Depends(get_db),
):
    """
    Cancels at Bachs immediately. verification_tier is NOT downgraded here —
    that only happens when the subscription.canceled webhook confirms it,
    keeping this endpoint consistent with "webhooks are the source of
    truth for fulfilment" rather than trusting the cancel-call response.
    """
    profile = db.query(FounderProfile).filter(FounderProfile.user_id == current_user.id).first()
    sub = db.query(FounderSubscription).filter(
        FounderSubscription.founder_profile_id == profile.id if profile else False
    ).first()
    if sub is None or not sub.bachs_subscription_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active subscription found.")

    try:
        bachs.cancel_subscription(sub.bachs_subscription_id)
    except bachs.BachsError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def bachs_webhook(
    request: Request,
    db: Session = Depends(get_db),
    bachs_signature: str | None = Header(default=None, alias="Bachs-Signature"),
    x_bachs_signature: str | None = Header(default=None, alias="X-Bachs-Signature"),
):
    """
    Receives Bachs webhook events. No auth dependency — Bachs calls this,
    not a logged-in Fundry user — so the HMAC signature check below IS the
    authentication for this endpoint. Confirm the actual header name Bachs
    sends against their webhook docs; both common conventions are checked.
    """
    raw_body = await request.body()
    signature = bachs_signature or x_bachs_signature

    if not verify_signature(raw_body, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature.")

    event = json.loads(raw_body)
    event_id = event.get("id") or event.get("event_id")
    event_type = event.get("type") or event.get("event")

    if not event_id or not event_type:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed webhook payload.")

    # Idempotency: Bachs (like most webhook senders) may redeliver the same
    # event. If we've already logged this event id, ack it without
    # re-applying side effects.
    if db.query(BillingEvent).filter(BillingEvent.bachs_event_id == event_id).first() is not None:
        return {"status": "already_processed"}

    db.add(BillingEvent(bachs_event_id=event_id, event_type=event_type, payload=raw_body.decode("utf-8")))
    db.commit()

    data = event.get("data", event)
    metadata = data.get("metadata", {}) or {}
    founder_profile_id = metadata.get("founder_profile_id")

    if event_type in ("collection.succeeded", "checkout.completed", "invoice.paid"):
        if not founder_profile_id:
            return {"status": "ignored_no_metadata"}

        profile = db.query(FounderProfile).filter(FounderProfile.id == founder_profile_id).first()
        if profile is None:
            return {"status": "ignored_unknown_profile"}

        sub = db.query(FounderSubscription).filter(FounderSubscription.founder_profile_id == profile.id).first()
        if sub is None:
            sub = FounderSubscription(
                founder_profile_id=profile.id,
                plan=SubscriptionPlanEnum(metadata.get("plan", "monthly")),
            )
            db.add(sub)

        sub.status = SubscriptionStatusEnum.active
        sub.bachs_subscription_id = data.get("subscription_id") or data.get("subscription", {}).get("id")
        sub.bachs_customer_id = data.get("customer_id") or data.get("customer", {}).get("id")
        period_end = data.get("current_period_end") or data.get("subscription", {}).get("current_period_end")
        if period_end:
            sub.current_period_end = datetime.fromisoformat(period_end.replace("Z", "+00:00"))
        amount = data.get("amount")
        if amount is not None:
            sub.amount = amount
        sub.currency = data.get("currency", "NGN")

        profile.verification_tier = VerificationTierEnum.business_verified
        db.commit()

    elif event_type in ("subscription.canceled", "subscription.expired", "invoice.payment_failed"):
        bachs_sub_id = data.get("subscription_id") or data.get("id")
        sub = db.query(FounderSubscription).filter(FounderSubscription.bachs_subscription_id == bachs_sub_id).first()
        if sub is None:
            return {"status": "ignored_unknown_subscription"}

        if event_type == "invoice.payment_failed":
            sub.status = SubscriptionStatusEnum.past_due
        else:
            sub.status = SubscriptionStatusEnum.canceled
            sub.canceled_at = datetime.now(timezone.utc)
            profile = db.query(FounderProfile).filter(FounderProfile.id == sub.founder_profile_id).first()
            if profile is not None:
                profile.verification_tier = VerificationTierEnum.basic
        db.commit()

    return {"status": "processed"}
