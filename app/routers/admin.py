import hmac
import html
import json
import logging
import re
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
    named = db.query(func.count(WaitlistEntry.id)).filter(WaitlistEntry.first_name.isnot(None)).scalar() or 0
    return {
        "named": named,
        "total": total,
        "notified": notified,
        "pending": pending,
        "next_batch": min(MAX_EMAILS_PER_SEND, pending),
        "max_per_send": MAX_EMAILS_PER_SEND,
        "gemini_configured": bool(settings.gemini_api_key),
        "email_configured": bool(settings.brevo_api_key and settings.brevo_sender_email),
    }


# ---------------------------- Gemini drafting ----------------------------

_FALLBACK_SUBJECT = "Welcome to Fundry, we're live"
_FALLBACK_BODY = (
    "Welcome to Fundry! You joined our waitlist, and today we're excited to tell you that Fundry is officially live.\n\n"
    "Whether you're a founder building something meaningful or an investor looking for the next big opportunity, "
    "Fundry was built with people like you in mind. Your first step is to create your profile: choose Founder or "
    "Investor, tell us a little about yourself, and follow the steps. This helps us connect you to the right "
    "people, ideas and opportunities.\n\n"
    "Next, stay updated on all our platforms using the buttons below. We can't wait to see what you bring to the table."
)


_log = logging.getLogger("uvicorn.error")
# Tried in order if the configured model has been retired (404).
_GEMINI_FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-3.8-flash"]


def _draft_with_gemini() -> tuple[str, str, bool, str | None]:
    """Returns (subject, plain_text_body, used_fallback, fallback_reason)."""
    if not settings.gemini_api_key:
        return _FALLBACK_SUBJECT, _FALLBACK_BODY, True, "GEMINI_API_KEY is not set on the server."

    prompt = (
        "Write a welcome and launch announcement email from the Fundry team to people on our waitlist. "
        "Fundry is a platform that connects startup founders with investors (profiles, discovery, "
        "connection requests, messaging). Tell them Fundry has launched. Speak to both founders and investors. "
        "Say their first step is to create a profile, choose Founder or Investor as their category, tell us a "
        "little about themselves, and follow the steps. Then say to follow Fundry on X and subscribe on YouTube "
        "using the buttons below the email. Tone: warm, confident, concise, no hype. No emojis. "
        "Subject under 70 characters. Body 80-120 words, plain text, short paragraphs separated by blank lines. "
        "IMPORTANT: do NOT write a greeting line (it is added automatically per person), do NOT write a sign-off "
        "or signature, do NOT include any URLs or links, and do not use placeholders or merge tags. "
        "Start directly with the first paragraph. Do not include the subject inside the body."
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


_GENERIC_NAMES = {
    "info", "admin", "hello", "hi", "hey", "contact", "support", "team", "sales", "mail", "office", "help",
    "noreply", "enquiries", "inquiries", "accounts", "billing", "founder", "ceo", "investor", "business",
    "careers", "jobs", "press", "media", "studio", "test", "user", "email", "me",
}


def _first_name(entry: WaitlistEntry) -> str | None:
    """Stored first name if we have one, else a cautious guess from the email
    (only for patterns like first.last@ or first123@; otherwise None)."""
    stored = (entry.first_name or "").strip()
    if stored:
        return stored.split()[0][:30].capitalize() if stored.islower() else stored.split()[0][:30]
    m = re.match(r"^([a-z]{3,15})(?:[._\-+]|\d)", entry.email.split("@")[0].lower())
    if m and m.group(1) not in _GENERIC_NAMES:
        return m.group(1).capitalize()
    return None


def _greeting(entry: WaitlistEntry) -> str:
    name = _first_name(entry)
    return f"Hi {name}," if name else "Hi there,"


def _company_line(entry: WaitlistEntry) -> str | None:
    role = getattr(entry.role, "value", entry.role)
    company = (entry.company or "").strip()
    if role == "investor" and company:
        return f"It's great to have {company} on the Fundry waitlist."
    return None


def _clean_body(body: str) -> str:
    body = re.sub(r"^\s*(hi|hello|hey|dear)\b[^\n]*\n+", "", body.strip(), flags=re.I)
    return re.sub(r"\n+\s*(best[^\n]*,|regards,|cheers,)?\s*(the )?fundry team\s*$", "", body, flags=re.I).strip()


def _render_email(body: str, entry: WaitlistEntry) -> tuple[str, str]:
    """Returns (html, plain_text) personalized for one recipient."""
    paragraphs = [_greeting(entry)]
    company = _company_line(entry)
    if company:
        paragraphs.append(company)
    paragraphs += [p.strip() for p in _clean_body(body).split("\n\n") if p.strip()]

    links = [
        ("Open Fundry", settings.launch_app_url, "#2563eb"),
        ("Follow us on X", settings.x_url, "#111111"),
        ("Subscribe on YouTube", settings.youtube_url, "#dc2626"),
    ]
    text = "\n\n".join(paragraphs) + "\n\n" + "\n".join(f"{label}: {url}" for label, url, _ in links) + "\n\nThe Fundry Team"

    body_html = "".join(
        f'<p style="margin:0 0 16px;line-height:1.6">{html.escape(p).replace(chr(10), "<br>")}</p>' for p in paragraphs
    )
    buttons = "".join(
        f'<a href="{html.escape(url)}" style="display:inline-block;margin:0 8px 10px 0;padding:12px 20px;'
        f'background:{color};color:#ffffff;text-decoration:none;border-radius:8px;font-weight:600;font-size:15px">'
        f"{html.escape(label)}</a>"
        for label, url, color in links
    )
    html_out = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:16px;color:#111;max-width:560px;margin:0 auto;padding:24px">'
        f'{body_html}<div style="margin:24px 0 8px">{buttons}</div>'
        '<p style="margin:16px 0 0;line-height:1.6">The Fundry Team</p></div>'
    )
    return html_out, text


class LaunchDraft(BaseModel):
    subject: str | None = None
    body: str | None = None
    used_fallback: bool = False


@router.post("/waitlist/preview-launch", dependencies=[Depends(require_admin)])
def preview_launch_email(db: Session = Depends(get_db)):
    """Drafts the email with Gemini without sending anything."""
    subject, body, used_fallback, reason = _draft_with_gemini()
    upcoming = (
        db.query(WaitlistEntry)
        .filter(WaitlistEntry.launch_notified_at.is_(None))
        .order_by(WaitlistEntry.created_at.asc())
        .limit(5)
        .all()
    )
    return {
        "subject": subject,
        "body": _clean_body(body),
        "used_fallback": used_fallback,
        "fallback_reason": reason,
        "samples": [{"email": e.email, "greeting": _greeting(e), "company": _company_line(e)} for e in upcoming],
    }


class ContactsUpload(BaseModel):
    rows: str


@router.post("/waitlist/contacts", dependencies=[Depends(require_admin)])
def upload_contacts(payload: ContactsUpload, db: Session = Depends(get_db)):
    """Fill in first names / company names. One person per line:
    email, first name, company (company optional)."""
    updated, not_found = 0, []
    for line in payload.rows.splitlines():
        parts = [x.strip() for x in re.split(r"[,\t;]", line)]
        if len(parts) < 2 or "@" not in parts[0]:
            continue
        entry = db.query(WaitlistEntry).filter(func.lower(WaitlistEntry.email) == parts[0].lower()).first()
        if not entry:
            not_found.append(parts[0])
            continue
        if parts[1]:
            entry.first_name = parts[1][:60]
        if len(parts) > 2 and parts[2]:
            entry.company = parts[2][:120]
        updated += 1
    db.commit()
    return {"updated": updated, "not_found": not_found[:20]}


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
        sent_entries: list[WaitlistEntry] = []
        bad_entries: list[WaitlistEntry] = []
        warning: str | None = None
        last_error = ""
        session = requests.Session()
        session.headers.update({"api-key": settings.brevo_api_key, "accept": "application/json"})

        # One message per recipient so nobody sees anyone else's address.
        for e in entries:
            html_body, text_body = _render_email(body, e)
            try:
                r = session.post(
                    BREVO_URL,
                    json={
                        "sender": {"name": settings.brevo_sender_name, "email": settings.brevo_sender_email},
                        "to": [{"email": e.email}],
                        "subject": subject,
                        "htmlContent": html_body,
                        "textContent": text_body,
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
