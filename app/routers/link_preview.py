import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.dependencies import get_current_user
from app.models import User
from app.schemas import LinkPreviewResponse

router = APIRouter(prefix="/api/v1", tags=["Link Preview"])

# A normal desktop Chrome UA. The previous "Mozilla/5.0 (compatible;
# FundryLinkPreview/1.0)" string self-identifies as a bot via the
# "compatible;" convention, which a lot of CDN/WAF bot-protection (Cloudflare
# etc.) blocks outright — silently killing the preview for any site behind
# one, regardless of the parsing logic below.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_RELEVANT_META_KEYS = {"og:title", "og:description", "og:image", "og:site_name", "twitter:image", "description"}


class _MetaTagParser(HTMLParser):
    """
    Extracts <meta> and <title> content regardless of attribute order or
    quoting style. The previous regex-based approach required
    property/name to appear *before* content within the tag — which fails
    on any site that emits `<meta content="..." property="og:title">`
    (React/Helmet, WordPress, and plenty of others do exactly this) — so
    it silently produced no data for a large share of real sites.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title: str | None = None
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        attr_dict = {k.lower(): (v or "") for k, v in attrs}
        key = (attr_dict.get("property") or attr_dict.get("name") or "").lower()
        if key in _RELEVANT_META_KEYS and "content" in attr_dict:
            self.meta.setdefault(key, attr_dict["content"])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)  # self-closed <meta ... /> tags

    def handle_data(self, data: str) -> None:
        if self._in_title and self.title is None:
            self.title = data.strip()


class _TitleAwareParser(_MetaTagParser):
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        super().handle_starttag(tag, attrs)
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False


def _is_safe_public_url(url: str) -> bool:
    """
    SSRF guard: a founder's "startup link" is attacker-controllable input
    that our server fetches. Without this check, someone could point it at
    http://169.254.169.254/... (cloud metadata endpoints), an internal
    service on localhost, or another host on our own private network, and
    use this endpoint as a proxy to probe them. Resolves the hostname and
    rejects anything that lands in a private/loopback/link-local/reserved
    range before we ever make the request.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False

    try:
        resolved_ips = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, None)}
    except socket.gaierror:
        return False

    for ip_str in resolved_ips:
        ip = ipaddress.ip_address(ip_str)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


@router.get("/link-preview", response_model=LinkPreviewResponse)
def get_link_preview(
    url: str = Query(..., description="The startup link to preview"),
    current_user: User = Depends(get_current_user),
):
    """
    Fetches OG/Twitter meta tags server-side — avoids browser CORS issues
    and gives a Vercel-style preview card for a founder's startup link.
    """
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="URL must start with http:// or https://")

    if not _is_safe_public_url(url):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That URL can't be previewed (it resolves to a non-public address).",
        )

    try:
        response = requests.get(
            url,
            timeout=6,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            allow_redirects=True,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Could not fetch that URL: {exc}"
        ) from exc

    # Redirects can land somewhere non-public even when the original URL
    # passed the check (e.g. a shortener pointing at an internal host) —
    # re-validate the final URL actually reached.
    if not _is_safe_public_url(response.url):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That URL redirects somewhere that can't be previewed.",
        )

    html = response.text[:300_000]  # cap — only the <head> is needed
    parser = _TitleAwareParser()
    try:
        parser.feed(html)
    except Exception:
        # Malformed markup shouldn't 500 the request — just fall back to
        # whatever partial data was extracted before the parser choked.
        pass

    # Sites frequently set og:image to a path relative to their own domain
    # (e.g. "/og-image.png") rather than an absolute URL — resolve against
    # the final URL (after redirects) so the frontend always gets something
    # loadable rather than a broken relative path.
    raw_image = parser.meta.get("og:image") or parser.meta.get("twitter:image")
    image = urljoin(response.url, raw_image) if raw_image else None

    return LinkPreviewResponse(
        url=url,
        title=parser.meta.get("og:title") or parser.title,
        description=parser.meta.get("og:description") or parser.meta.get("description"),
        image=image,
        site_name=parser.meta.get("og:site_name"),
    )
