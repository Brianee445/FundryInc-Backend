import uuid
from datetime import date, datetime

from pydantic import BaseModel, EmailStr, ConfigDict, field_validator
from typing import Literal, Optional

class WaitlistCreate(BaseModel):
    email: EmailStr
    role: Literal["founder", "investor", "advisor", "accelerator"]

class WaitlistResponse(BaseModel):
    success: bool
    message: str


class SignupRequest(BaseModel):
    email: EmailStr
    password: str
    role: Literal["founder", "investor"]  # admin accounts are never self-signed-up

    @field_validator("password")
    @classmethod
    def password_minimum_length(cls, value: str) -> str:
        if len(value) < 8:
            raise ValueError("Password must be at least 8 characters long")
        return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class GoogleAuthRequest(BaseModel):
    id_token: str
    # Only required the first time — i.e. when this Google account has no
    # matching Fundry user yet. Ignored on subsequent logins.
    role: Literal["founder", "investor"] | None = None


class UserResponse(BaseModel):
    id: uuid.UUID
    email: EmailStr
    role: str
    status: str
    created_at: datetime

    # Lets Pydantic build this response directly from the SQLAlchemy User
    # model's attributes instead of requiring a dict.
    model_config = ConfigDict(from_attributes=True)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    user: UserResponse


StageLiteral = Literal["idea", "preseed", "seed", "series_a", "series_b_plus"]
InvestorTypeLiteral = Literal["angel", "vc", "fund", "family_office", "other"]


class FounderProfileUpsert(BaseModel):
    """
    Used for both create and update — a founder has exactly one profile, so
    the endpoint is a single upsert rather than separate create/update
    routes with different validation rules.
    """

    startup_name: str
    tagline: Optional[str] = None
    description: Optional[str] = None
    sector: Optional[str] = None
    stage: StageLiteral = "idea"
    funding_ask_min: Optional[float] = None
    funding_ask_max: Optional[float] = None
    location_country: Optional[str] = None
    location_city: Optional[str] = None
    pitch_deck_url: Optional[str] = None
    demo_video_url: Optional[str] = None
    profile_picture_url: Optional[str] = None
    gallery_image_urls: list[str] = []
    startup_link: Optional[str] = None
    contact_visibility: Literal["private", "public"] = "private"

    @field_validator("gallery_image_urls")
    @classmethod
    def max_six_gallery_images(cls, value: list[str]) -> list[str]:
        if len(value) > 6:
            raise ValueError("Up to 6 gallery images allowed")
        return value

    @field_validator("funding_ask_max")
    @classmethod
    def max_not_below_min(cls, value: Optional[float], info) -> Optional[float]:
        min_value = info.data.get("funding_ask_min")
        if value is not None and min_value is not None and value < min_value:
            raise ValueError("funding_ask_max cannot be less than funding_ask_min")
        return value


class FounderContactInfo(BaseModel):
    """Only ever included once a connection request has been accepted, or the founder has made it public."""

    email: EmailStr


class FounderProfileResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    startup_name: str
    tagline: Optional[str]
    description: Optional[str]
    sector: Optional[str]
    stage: str
    funding_ask_min: Optional[float]
    funding_ask_max: Optional[float]
    location_country: Optional[str]
    location_city: Optional[str]
    pitch_deck_url: Optional[str]
    demo_video_url: Optional[str]
    profile_picture_url: Optional[str] = None
    gallery_image_urls: list[str] = []
    startup_link: Optional[str] = None
    contact_visibility: str
    verification_tier: str
    published: bool
    is_spotlighted: bool = False
    created_at: datetime
    # Populated by the router, not the ORM object directly — see
    # routers/founder_profiles.py for when this is (and isn't) attached.
    contact: Optional[FounderContactInfo] = None
    is_saved: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)


class ConnectionRequestCreate(BaseModel):
    founder_profile_id: uuid.UUID
    message: Optional[str] = None


class ConnectionRequestCreateToInvestor(BaseModel):
    """Founder-initiated — the mirror of ConnectionRequestCreate. See
    POST /connections/to-investor in routers/connections.py."""

    investor_user_id: uuid.UUID
    message: Optional[str] = None


class ConnectionRequestDecision(BaseModel):
    status: Literal["accepted", "declined"]


class ConnectionRequestResponse(BaseModel):
    id: uuid.UUID
    investor_id: uuid.UUID
    founder_profile_id: uuid.UUID
    initiator: str
    message: Optional[str]
    status: str
    contact_revealed_at: Optional[datetime]
    created_at: datetime
    # Denormalized for convenience so the frontend doesn't need a second
    # request just to show who/what a connection request is about.
    startup_name: Optional[str] = None
    investor_email: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class InvestorProfileUpsert(BaseModel):
    """Used for both create and update, same pattern as FounderProfileUpsert
    — one profile per investor, so a single upsert route rather than
    separate create/update endpoints."""

    investor_type: InvestorTypeLiteral = "angel"
    firm_name: Optional[str] = None
    bio: Optional[str] = None
    check_size_min: Optional[float] = None
    check_size_max: Optional[float] = None
    sectors_of_interest: list[str] = []
    geographies_of_interest: list[str] = []
    profile_picture_url: Optional[str] = None
    linkedin_url: Optional[str] = None
    contact_visibility: Literal["private", "public"] = "private"

    @field_validator("sectors_of_interest", "geographies_of_interest")
    @classmethod
    def max_ten_tags(cls, value: list[str]) -> list[str]:
        if len(value) > 10:
            raise ValueError("Up to 10 entries allowed")
        return value

    @field_validator("check_size_max")
    @classmethod
    def max_not_less_than_min(cls, value: Optional[float], info) -> Optional[float]:
        check_min = info.data.get("check_size_min")
        if value is not None and check_min is not None and value < check_min:
            raise ValueError("Maximum check size cannot be less than minimum")
        return value


class InvestorContactInfo(BaseModel):
    """Only ever included once a connection request has been accepted, or the investor has made it public."""

    email: EmailStr


class InvestorProfileResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    investor_type: str
    firm_name: Optional[str]
    bio: Optional[str]
    check_size_min: Optional[float]
    check_size_max: Optional[float]
    sectors_of_interest: list[str] = []
    geographies_of_interest: list[str] = []
    profile_picture_url: Optional[str] = None
    linkedin_url: Optional[str] = None
    contact_visibility: str
    published: bool
    created_at: datetime
    # Populated by the router, not the ORM object directly — see
    # routers/investor_profiles.py for when this is (and isn't) attached.
    contact: Optional[InvestorContactInfo] = None

    model_config = ConfigDict(from_attributes=True)


class MessageCreate(BaseModel):
    body: str

    @field_validator("body")
    @classmethod
    def body_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message cannot be empty")
        if len(value) > 4000:
            raise ValueError("Message is too long (4000 character max)")
        return value


class MessageResponse(BaseModel):
    id: uuid.UUID
    connection_id: uuid.UUID
    sender_id: uuid.UUID
    body: str
    created_at: datetime
    read_at: Optional[datetime]
    # Set by the router relative to the requesting user — not a stored column.
    is_mine: bool = False

    model_config = ConfigDict(from_attributes=True)


class MessageThreadResponse(BaseModel):
    connection_id: uuid.UUID
    counterparty_label: str
    counterparty_avatar_url: Optional[str] = None
    last_message: Optional[str] = None
    last_message_at: Optional[datetime] = None
    unread_count: int = 0


class LinkPreviewResponse(BaseModel):
    url: str
    title: Optional[str] = None
    description: Optional[str] = None
    image: Optional[str] = None
    site_name: Optional[str] = None


class SpotlightUpdate(BaseModel):
    is_spotlighted: bool


class DailyCount(BaseModel):
    date: date
    count: int


class FounderAnalyticsResponse(BaseModel):
    profile_views_total: int
    profile_views_daily: list[DailyCount]
    connection_requests_total: int
    connection_requests_pending: int
    connection_requests_accepted: int
    connection_requests_declined: int
    connection_requests_daily: list[DailyCount]
    saved_by_investors_count: int
    messages_total: int


class InvestorAnalyticsResponse(BaseModel):
    connection_requests_sent_total: int
    connection_requests_pending: int
    connection_requests_accepted: int
    connection_requests_declined: int
    connection_requests_daily: list[DailyCount]
    saved_founders_count: int
    messages_total: int
