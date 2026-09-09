import re

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.dependencies import get_current_user
from app.models import User
from app.schemas import LinkPreviewResponse

router = APIRouter(prefix="/api/v1", tags=["Link Preview"])

_META_TAG_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?P<key>og:[a-z:]+|twitter:[a-z:]+|description)["\'][^>]+'
    r'content=["\'](?P<value>[^"\']*)["\']',
    re.IGNORECASE,
)
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(?P<value>[^<]*)</title>", re.IGNORECASE)


def _extract_og_tags(html: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for match in _META_TAG_RE.finditer(html):
        tags.setdefault(match.group("key").lower(), match.group("value"))
    return tags


@router.get("/link-preview", response_model=LinkPreviewResponse)
def get_link_preview(
    url: str = Query(..., description="The startup link to preview"),
    current_user: User = Depends(get_current_user),
):
    """
    Fetches OG/Twitter meta tags server-side — avoids browser CORS issues
    and gives a Vercel-style preview card for a founder's startup link.
    MVP-quality regex parse; swap for BeautifulSoup if you hit sites with
    unusual markup.
    """
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="URL must start with http:// or https://")

    try:
        response = requests.get(
            url,
            timeout=5,
            headers={"User-Agent": "Mozilla/5.0 (compatible; FundryLinkPreview/1.0)"},
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Could not fetch that URL: {exc}"
        ) from exc

    html = response.text[:200_000]  # cap — only the <head> is needed
    tags = _extract_og_tags(html)
    title_match = _TITLE_TAG_RE.search(html)

    return LinkPreviewResponse(
        url=url,
        title=tags.get("og:title") or (title_match.group("value").strip() if title_match else None),
        description=tags.get("og:description") or tags.get("description"),
        image=tags.get("og:image") or tags.get("twitter:image"),
        site_name=tags.get("og:site_name"),
    )
