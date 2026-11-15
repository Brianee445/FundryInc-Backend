import hmac
import html
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
        "resend_configured": bool(settings.resend_api_key and settings.resend_from_email),
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


def _draft_with_gemini() -> tuple[str, str, bool]:
    """Returns (subject, plain_text_body, used_fallback)."""
    if not settings.gemini_api_key:
        return _FALLBACK_SUBJECT, _FALLBACK_BODY, True

    prompt = (
        "Write a launch announcement email from the Fundry team to people on our waitlist. "
        "Fundry is a platform that connects startup founders with investors (profiles, discovery, "
        "connection requests, messaging). Tell them Fundry has launched and they can sign up now at "
        f"{settings.launch_app_url}. Tone: warm, confident, concise, no hype or emojis. "
        "Subject under 70 characters. Body 80-130 words, plain text, short paragraphs separated by "
        "blank lines, start with 'Hi there,' and sign off 'The Fundry Team'. "
        "Do not use placeholders or merge tags. Do not include the subject inside the body."
    )
    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent",
            headers={"x-goog-api-key": settings.gemini_api_key, "Content-Type": "application/json"},
            json={
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
            },
            timeout=30,
        )
        resp.raise_for_status()
        import json

        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(text)
        subject, body = str(data["subject"]).strip(), str(data["body"]).strip()
        if not subject or len(body) < 40:
            raise ValueError("empty draft")
        return subject[:150], body, False
    except Exception:
        return _FALLBACK_SUBJECT, _FALLBACK_BODY, True


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


# ------------------------------ Send launch ------------------------------


@router.post("/waitlist/send-launch", dependencies=[Depends(require_admin)])
def send_launch_emails(db: Session = Depends(get_db)):
    if not (settings.resend_api_key and settings.resend_from_email):
        raise HTTPException(status_code=503, detail="Resend is not configured (RESEND_API_KEY / RESEND_FROM_EMAIL).")

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

        subject, body, used_fallback = _draft_with_gemini()
        html_body = _to_html(body)

        # One message per recipient so nobody sees anyone else's address.
        batch = [
            {"from": settings.resend_from_email, "to": [e.email], "subject": subject, "html": html_body, "text": body}
            for e in entries
        ]
        try:
            resp = requests.post(
                "https://api.resend.com/emails/batch",
                headers={"Authorization": f"Bearer {settings.resend_api_key}", "Content-Type": "application/json"},
                json=batch,
                timeout=45,
            )
        except requests.RequestException:
            raise HTTPException(status_code=502, detail="Could not reach Resend. Nothing was marked as sent.")

        if resp.status_code >= 300:
            raise HTTPException(status_code=502, detail=f"Resend rejected the send: {resp.text[:300]}")

        now = datetime.now(timezone.utc)
        for e in entries:
            e.launch_notified_at = now
        db.commit()

        remaining = (
            db.query(func.count(WaitlistEntry.id)).filter(WaitlistEntry.launch_notified_at.is_(None)).scalar() or 0
        )
        return {
            "sent": len(entries),
            "remaining": remaining,
            "subject": subject,
            "body": body,
            "used_fallback": used_fallback,
        }
    finally:
        _send_lock.release()
