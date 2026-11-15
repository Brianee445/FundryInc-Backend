import hmac
import html
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import WaitlistEntry

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])

MAX_EMAILS_PER_SEND = 50
ADMIN_TOKEN_MINUTES = 120
_bearer = HTTPBearer(auto_error=False)

# Signed with a key derived from SECRET_KEY so an admin token can never
# validate as a normal user token (and vice versa).
_ADMIN_KEY = lambda: f"{settings.secret_key}:admin-console"  # noqa: E731

# --- brute-force guard (in-memory; fine for a single Render instance) ---
_attempts: dict[str, list[float]] = {}
_MAX_ATTEMPTS, _WINDOW_SECONDS = 5, 15 * 60
_send_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


def _check_rate_limit(ip: str) -> None:
    now = time.time()
    recent = [t for t in _attempts.get(ip, []) if now - t < _WINDOW_SECONDS]
    _attempts[ip] = recent
    if len(recent) >= _MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many attempts. Try again in 15 minutes.")


def require_admin(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    unauthorized = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin session expired. Log in again.")
    if creds is None:
        raise unauthorized
    try:
        payload = jwt.decode(creds.credentials, _ADMIN_KEY(), algorithms=[settings.algorithm])
    except JWTError:
        raise unauthorized
    if payload.get("scope") != "admin_console":
        raise unauthorized


class AdminLogin(BaseModel):
    passcode: str


@router.post("/login")
def admin_login(payload: AdminLogin, request: Request):
    if not settings.admin_passcode:
        raise HTTPException(status_code=503, detail="Admin access is not configured (ADMIN_PASSCODE missing).")
    ip = _client_ip(request)
    _check_rate_limit(ip)

    if not hmac.compare_digest(payload.passcode.encode(), settings.admin_passcode.encode()):
        _attempts.setdefault(ip, []).append(time.time())
        raise HTTPException(status_code=401, detail="Incorrect passcode.")

    _attempts.pop(ip, None)
    expire = datetime.now(timezone.utc) + timedelta(minutes=ADMIN_TOKEN_MINUTES)
    token = jwt.encode({"sub": "admin", "scope": "admin_console", "exp": expire}, _ADMIN_KEY(), algorithm=settings.algorithm)
    return {"token": token, "expires_in_minutes": ADMIN_TOKEN_MINUTES}


@router.get("/waitlist/summary", dependencies=[Depends(require_admin)])
def waitlist_summary(db: Session = Depends(get_db)):
    total = db.query(func.count(WaitlistEntry.id)).scalar() or 0
    notified = (
        db.query(func.count(WaitlistEntry.id)).filter(WaitlistEntry.launch_notified_at.isnot(None)).scalar() or 0
    )
    pending = total - notified
    return {
        "total": total,
        "notified": notified,
        "pending": pending,
        "next_batch": min(MAX_EMAILS_PER_SEND, pending),
        "max_per_send": MAX_EMAILS_PER_SEND,
        "gemini_configured": bool(settings.gemini_api_key),
        "email_configured": bool(settings.brevo_api_key and settings.brevo_sender_email),
    }


# ---------------------------- Gemini drafting ----------------------------

_FALLBACK_SUBJECT = "Fundry is live — your early access is here"
_FALLBACK_BODY = (
    "Hi there,\n\n"
    "You joined our waitlist, and today we're excited to tell you: Fundry is officially live.\n\n"
    "Fundry connects founders and investors directly — build your profile, discover the right people, "
    "and start real conversations. As a waitlist member, you can create your account right now.\n\n"
    "We can't wait to see what you build.\n\n"
    "The Fundry Team"
)


_log = logging.getLogger("uvicorn.error")
# Tried in order if the configured model has been retired (404).
_GEMINI_FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-3.8-flash"]


def _draft_with_gemini() -> tuple[str, str, bool, str | None]:
    """Returns (subject, plain_text_body, used_fallback, fallback_reason)."""
    if not settings.gemini_api_key:
        return _FALLBACK_SUBJECT, _FALLBACK_BODY, True, "GEMINI_API_KEY is not set on the server."

    prompt = (
        "Write a launch announcement email from the Fundry team to people on our waitlist. "
        "Fundry is a platform that connects startup founders with investors (profiles, discovery, "
        "connection requests, messaging). Tell them Fundry has launched and they can sign up now at "
        f"{settings.launch_app_url}. Tone: warm, confident, concise, no hype or emojis. "
        "Subject under 70 characters. Body 80-130 words, plain text, short paragraphs separated by "
        "blank lines, start with 'Hi there,' and sign off 'The Fundry Team'. "
        "Do not use placeholders or merge tags. Do not include the subject inside the body."
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {"subject": {"type": "STRING"}, "body": {"type": "STRING"}},
                "required": ["subject", "body"],
            },
            "temperature": 0.7,
        },
    }

    models = list(dict.fromkeys([settings.gemini_model, *_GEMINI_FALLBACK_MODELS]))
    reason = "Unknown error"
    for model in models:
        try:
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": settings.gemini_api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=40,
            )
            if resp.status_code != 200:
                try:
                    detail = resp.json().get("error", {}).get("message", resp.text)
                except ValueError:
                    detail = resp.text
                reason = f"{model}: HTTP {resp.status_code} - {str(detail)[:180]}"
                _log.warning("Gemini draft failed: %s", reason)
                if resp.status_code == 404:
                    continue  # model retired/renamed: try the next one
                break  # auth/quota/etc: other models won't help
            data = json.loads(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
            subject, body = str(data["subject"]).strip(), str(data["body"]).strip()
            if not subject or len(body) < 40:
                raise ValueError("empty draft")
            return subject[:150], body, False, None
        except Exception as exc:  # noqa: BLE001
            reason = f"{model}: {type(exc).__name__} - {str(exc)[:150]}"
            _log.warning("Gemini draft failed: %s", reason)
            break
    return _FALLBACK_SUBJECT, _FALLBACK_BODY, True, reason


def _to_html(body: str) -> str:
    paragraphs = "".join(
        f'<p style="margin:0 0 16px;line-height:1.6">{html.escape(p).replace(chr(10), "<br>")}</p>'
        for p in body.split("\n\n")
        if p.strip()
    )
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:16px;color:#111;max-width:560px;margin:0 auto;padding:24px">'
        f"{paragraphs}"
        f'<p style="margin:24px 0"><a href="{html.escape(settings.launch_app_url)}" '
        'style="background:#2563eb;color:#fff;text-decoration:none;padding:12px 22px;border-radius:8px;display:inline-block">'
        "Open Fundry</a></p></div>"
    )


class LaunchDraft(BaseModel):
    subject: str | None = None
    body: str | None = None
    used_fallback: bool = False


@router.post("/waitlist/preview-launch", dependencies=[Depends(require_admin)])
def preview_launch_email():
    """Drafts the email with Gemini without sending anything."""
    subject, body, used_fallback, reason = _draft_with_gemini()
    return {"subject": subject, "body": body, "used_fallback": used_fallback, "fallback_reason": reason}


# ------------------------------ Send launch ------------------------------
# Sends through Brevo's HTTPS API (Render's free tier blocks SMTP ports).

BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def _brevo_error(r: requests.Response) -> str:
    try:
        return str(r.json().get("message", r.text))[:200]
    except ValueError:
        return r.text[:200]


@router.post("/waitlist/send-launch", dependencies=[Depends(require_admin)])
def send_launch_emails(payload: LaunchDraft | None = None, db: Session = Depends(get_db)):
    if not (settings.brevo_api_key and settings.brevo_sender_email):
        raise HTTPException(status_code=503, detail="Brevo is not configured (BREVO_API_KEY / BREVO_SENDER_EMAIL).")

    if not _send_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A send is already in progress.")
    try:
        entries = (
            db.query(WaitlistEntry)
            .filter(WaitlistEntry.launch_notified_at.is_(None))
            .order_by(WaitlistEntry.created_at.asc())
            .limit(MAX_EMAILS_PER_SEND)
            .all()
        )
        if not entries:
            raise HTTPException(status_code=404, detail="Everyone on the waitlist has already been notified.")

        if payload and payload.subject and payload.body and len(payload.body.strip()) >= 40:
            # Send exactly the draft the admin previewed.
            subject, body, used_fallback = payload.subject.strip()[:150], payload.body.strip()[:5000], payload.used_fallback
        else:
            subject, body, used_fallback, _ = _draft_with_gemini()
        html_body = _to_html(body)

        sent_entries: list[WaitlistEntry] = []
        bad_entries: list[WaitlistEntry] = []
        warning: str | None = None
        last_error = ""
        session = requests.Session()
        session.headers.update({"api-key": settings.brevo_api_key, "accept": "application/json"})

        # One message per recipient so nobody sees anyone else's address.
        for e in entries:
            try:
                r = session.post(
                    BREVO_URL,
                    json={
                        "sender": {"name": settings.brevo_sender_name, "email": settings.brevo_sender_email},
                        "to": [{"email": e.email}],
                        "subject": subject,
                        "htmlContent": html_body,
                        "textContent": body,
                    },
                    timeout=20,
                )
            except requests.RequestException:
                warning = "Lost connection to Brevo partway through. Click again to continue."
                break
            if r.status_code in (200, 201):
                sent_entries.append(e)
            elif r.status_code == 400:
                last_error = _brevo_error(r)
                bad_entries.append(e)
            else:
                last_error = f"Brevo stopped the send ({r.status_code}): {_brevo_error(r)}"
                warning = last_error + ". Click again later to continue."
                break

        if not sent_entries:
            # Nothing went out: don't mark anyone, and surface the real reason
            # (e.g. unverified sender, bad API key) instead of burning the list.
            raise HTTPException(status_code=502, detail=last_error or warning or "Nothing was sent.")

        now = datetime.now(timezone.utc)
        # Addresses Brevo rejected as invalid are marked done so they don't
        # clog every future batch.
        for e in sent_entries + bad_entries:
            e.launch_notified_at = now
        db.commit()

        remaining = (
            db.query(func.count(WaitlistEntry.id)).filter(WaitlistEntry.launch_notified_at.is_(None)).scalar() or 0
        )
        return {
            "sent": len(sent_entries),
            "failed": len(bad_entries),
            "remaining": remaining,
            "subject": subject,
            "body": body,
            "used_fallback": used_fallback,
            "warning": warning,
        }
    finally:
        _send_lock.release()
