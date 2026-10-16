"""
Paid verification tiers via Paystack, for both founders and investors.

Tiers: starter (free, default) -> basic -> premium — an ACCOUNT-level
subscription on User.verification_tier, not tied to any one startup
profile. For founders this means the plan covers every startup they
manage (how many they're allowed to create is enforced in
routers/founder_profiles.py; how many intro messages they can send is
enforced in routers/connections.py).

Flow:
  1. POST /api/v1/billing/checkout {tier, interval} -> initializes a
     Paystack transaction against the matching plan and returns its
     authorization_url. Frontend redirects the browser there.
  2. User pays on Paystack's hosted page. Paystack redirects back to
     frontend_billing_return_url — UX only, never grants the tier itself.
  3. Paystack POSTs charge.success (first payment / each renewal) and
     subscription.create, subscription.disable to
     POST /api/v1/billing/webhook. That webhook is the only thing that
     ever changes verification_tier — per Paystack's own guidance,
     webhooks are the source of truth for fulfilment, never the client
     redirect.
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models import (
    BillingEvent,
    BillingIntervalEnum,
    Subscription,
    SubscriptionStatusEnum,
    User,
    VerificationTierEnum,
)
from app.schemas import CheckoutRequest, CheckoutResponse, SubscriptionStatusResponse
from app.services import paystack
from app.services.webhook_verify import verify_signature

router = APIRouter(prefix="/api/v1/billing", tags=["Billing"])

# (role, tier, interval) -> settings attribute name for the Paystack plan
# code. starter is free and deliberately absent — there is no checkout
# for it.
_PLAN_CODE_SETTINGS = {
    ("founder", "basic", "monthly"): "paystack_founder_basic_monthly_plan_code",
    ("founder", "basic", "annual"): "paystack_founder_basic_annual_plan_code",
    ("founder", "premium", "monthly"): "paystack_founder_premium_monthly_plan_code",
    ("founder", "premium", "annual"): "paystack_founder_premium_annual_plan_code",
    ("investor", "basic", "monthly"): "paystack_investor_basic_monthly_plan_code",
    ("investor", "basic", "annual"): "paystack_investor_basic_annual_plan_code",
    ("investor", "premium", "monthly"): "paystack_investor_premium_monthly_plan_code",
    ("investor", "premium", "annual"): "paystack_investor_premium_annual_plan_code",
}

# Naira prices per (tier, interval) — same across founder/investor roles.
# Mirrors the frontend's billing page; sent alongside the plan code on
# transaction initialize (Paystack ignores this once a plan is attached,
# but keeping it accurate avoids any confusion in the Paystack dashboard's
# transaction log, and some test-mode flows do still read it).
_TIER_PRICING_NAIRA = {
    ("basic", "monthly"): 2000,
    ("basic", "annual"): 24000,
    ("premium", "monthly"): 5000,
    ("premium", "annual"): 50000,  # discounted/promo annual price
}


def _plan_code(role: str, tier: str, interval: str) -> str:
    attr = _PLAN_CODE_SETTINGS.get((role, tier, interval))
    return getattr(settings, attr, "") if attr else ""


def _require_billable_role(current_user: User) -> str:
    if current_user.role not in ("founder", "investor"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Billing is only available to founders and investors.")
    return current_user.role


@router.post("/checkout", response_model=CheckoutResponse)
def create_checkout(
    payload: CheckoutRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Founder or investor. Starts a Paystack transaction for the chosen paid tier + billing interval."""
    role = _require_billable_role(current_user)

    plan_code = _plan_code(role, payload.tier, payload.interval)
    if not plan_code:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Billing is not configured yet for this plan.")

    amount_naira = _TIER_PRICING_NAIRA[(payload.tier, payload.interval)]

    try:
        transaction = paystack.initialize_transaction(
            plan_code=plan_code,
            amount_naira=amount_naira,
            customer_email=current_user.email,
            metadata={
                "role": role,
                "user_id": str(current_user.id),
                "tier": payload.tier,
                "interval": payload.interval,
            },
        )
    except paystack.PaystackError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    checkout_url = transaction.get("authorization_url")
    if not checkout_url:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Paystack did not return an authorization URL.")

    # Record the pending subscription now, keyed by the transaction
    # reference, so the webhook has a row to attach the real
    # subscription_code/email_token to once payment completes. Reads of
    # verification_tier never look at this row directly, only at
    # User.verification_tier, which the webhook sets.
    existing = db.query(Subscription).filter(Subscription.user_id == current_user.id).first()
    if existing is None:
        existing = Subscription(
            user_id=current_user.id,
            tier=VerificationTierEnum(payload.tier),
            interval=BillingIntervalEnum(payload.interval),
        )
        db.add(existing)
    else:
        existing.tier = VerificationTierEnum(payload.tier)
        existing.interval = BillingIntervalEnum(payload.interval)
    existing.paystack_reference = transaction.get("reference")
    existing.currency = "NGN"
    existing.amount = amount_naira
    db.commit()

    return CheckoutResponse(checkout_url=checkout_url)


@router.get("/subscription", response_model=SubscriptionStatusResponse)
def get_my_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_billable_role(current_user)
    sub = db.query(Subscription).filter(Subscription.user_id == current_user.id).first()

    return SubscriptionStatusResponse(
        verification_tier=current_user.verification_tier.value,
        tier=sub.tier.value if sub else None,
        interval=sub.interval.value if sub else None,
        status=sub.status.value if sub else None,
        current_period_end=sub.current_period_end if sub else None,
    )


@router.post("/cancel", status_code=status.HTTP_204_NO_CONTENT)
def cancel_my_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Cancels at Paystack immediately. verification_tier is NOT downgraded
    here — that only happens once the subscription.disable webhook
    confirms it, so the badge/feature access stays consistent with
    "webhooks are the source of truth for fulfilment" and the user keeps
    what they already paid for through the end of the current period.
    """
    _require_billable_role(current_user)
    sub = db.query(Subscription).filter(Subscription.user_id == current_user.id).first()
    if sub is None or not sub.paystack_subscription_code or not sub.paystack_email_token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active subscription found.")

    try:
        paystack.disable_subscription(sub.paystack_subscription_code, sub.paystack_email_token)
    except paystack.PaystackError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def paystack_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_paystack_signature: str | None = Header(default=None, alias="x-paystack-signature"),
):
    """
    Receives Paystack webhook events. No auth dependency — Paystack calls
    this, not a logged-in Fundry user — so the HMAC signature check below
    IS the authentication for this endpoint. Paystack always uses the
    header name x-paystack-signature (unlike Bachs, there's no
    alternate casing to check).
    """
    raw_body = await request.body()

    if not verify_signature(raw_body, x_paystack_signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature.")

    event = json.loads(raw_body)
    event_type = event.get("event")
    data = event.get("data", {})

    # Paystack has no separate event id field the way Bachs did — the
    # transaction reference (for charge.success) or subscription code
    # (for subscription.* events) is the closest stable identifier for
    # redelivery dedup. Falling back to a type+reference composite when
    # neither is present still catches the common redelivery case.
    event_id = data.get("reference") or data.get("subscription_code") or f"{event_type}:{data.get('id')}"

    if db.query(BillingEvent).filter(BillingEvent.provider_event_id == event_id).first() is not None:
        return {"status": "already_processed"}

    db.add(BillingEvent(provider_event_id=event_id, event_type=event_type, payload=raw_body.decode("utf-8")))
    db.commit()

    metadata = data.get("metadata", {}) or {}
    user_id = metadata.get("user_id")

    if event_type == "charge.success":
        if not user_id:
            return {"status": "ignored_no_metadata"}

        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            return {"status": "ignored_unknown_user"}

        sub = db.query(Subscription).filter(Subscription.user_id == user.id).first()
        if sub is None:
            sub = Subscription(
                user_id=user.id,
                tier=VerificationTierEnum(metadata.get("tier", "basic")),
                interval=BillingIntervalEnum(metadata.get("interval", "monthly")),
            )
            db.add(sub)
        else:
            sub.tier = VerificationTierEnum(metadata.get("tier", sub.tier.value))
            sub.interval = BillingIntervalEnum(metadata.get("interval", sub.interval.value))

        sub.status = SubscriptionStatusEnum.active
        sub.paystack_customer_code = data.get("customer", {}).get("customer_code")
        sub.paystack_reference = data.get("reference")
        amount_kobo = data.get("amount")
        if amount_kobo is not None:
            sub.amount = amount_kobo / 100
        sub.currency = data.get("currency", "NGN")

        # subscription_code/email_token come from the plan object embedded
        # in a charge.success for a subscription-backed transaction, or
        # from the dedicated subscription.create event below — either can
        # arrive first depending on timing, so both branches write them
        # when present rather than assuming one particular order.
        subscription_obj = data.get("plan_object") or data.get("subscription") or {}
        if subscription_obj.get("subscription_code"):
            sub.paystack_subscription_code = subscription_obj["subscription_code"]
        if subscription_obj.get("email_token"):
            sub.paystack_email_token = subscription_obj["email_token"]

        user.verification_tier = sub.tier
        db.commit()

    elif event_type == "subscription.create":
        subscription_code = data.get("subscription_code")
        email_token = data.get("email_token")
        customer_code = data.get("customer", {}).get("customer_code")

        # Match by the customer code Paystack just created/reused, since
        # metadata isn't guaranteed to be echoed on this event the same
        # way it is on charge.success.
        sub = None
        if customer_code:
            sub = db.query(Subscription).filter(Subscription.paystack_customer_code == customer_code).first()
        if sub is None and user_id:
            sub = db.query(Subscription).filter(Subscription.user_id == user_id).first()
        if sub is None:
            return {"status": "ignored_unknown_subscription"}

        if subscription_code:
            sub.paystack_subscription_code = subscription_code
        if email_token:
            sub.paystack_email_token = email_token
        sub.status = SubscriptionStatusEnum.active

        next_payment = data.get("next_payment_date")
        if next_payment:
            sub.current_period_end = datetime.fromisoformat(next_payment.replace("Z", "+00:00"))

        db.commit()

    elif event_type == "subscription.disable":
        subscription_code = data.get("subscription_code")
        sub = db.query(Subscription).filter(Subscription.paystack_subscription_code == subscription_code).first()
        if sub is None:
            return {"status": "ignored_unknown_subscription"}

        sub.status = SubscriptionStatusEnum.canceled
        sub.canceled_at = datetime.now(timezone.utc)
        user = db.query(User).filter(User.id == sub.user_id).first()
        if user is not None:
            user.verification_tier = VerificationTierEnum.starter
        db.commit()

    elif event_type == "invoice.payment_failed":
        subscription_code = (data.get("subscription") or {}).get("subscription_code")
        sub = db.query(Subscription).filter(Subscription.paystack_subscription_code == subscription_code).first()
        if sub is not None:
            sub.status = SubscriptionStatusEnum.past_due
            db.commit()

    return {"status": "processed"}
