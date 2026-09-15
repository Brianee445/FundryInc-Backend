import uuid

from sqlalchemy import (
    Column,
    String,
    Text,
    DateTime,
    Enum,
    BigInteger,
    ForeignKey,
    Boolean,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base
import enum

class RoleEnum(str, enum.Enum):
    founder = "founder"
    investor = "investor"
    advisor = "advisor"
    accelerator = "accelerator"

class WaitlistEntry(Base):
    __tablename__ = "waitlist_entries"

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    email = Column(String, unique=True, index=True, nullable=False)
    role = Column(Enum(RoleEnum), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class UserRoleEnum(str, enum.Enum):
    """
    Deliberately separate from the waitlist's RoleEnum: the waitlist accepts
    "advisor" and "accelerator" as interest signals, but only founders and
    investors are real account types in the product (per the PRD — admin
    accounts are provisioned directly, never through public signup).
    """

    founder = "founder"
    investor = "investor"
    admin = "admin"


class UserStatusEnum(str, enum.Enum):
    active = "active"
    suspended = "suspended"
    blocked = "blocked"
    pending = "pending"


class AuthProviderEnum(str, enum.Enum):
    local = "local"
    google = "google"


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    # Nullable because Google-authenticated users never set a local password.
    # Every route that checks credentials must handle password_hash being None
    # (see routers/auth.py login()) rather than assuming it's always set.
    password_hash = Column(String, nullable=True)
    auth_provider = Column(Enum(AuthProviderEnum), nullable=False, default=AuthProviderEnum.local)
    # Google's stable per-account identifier ("sub" claim). Used to recognize
    # a returning Google user even if they ever change their Google email.
    google_sub = Column(String, unique=True, index=True, nullable=True)
    role = Column(Enum(UserRoleEnum), nullable=False)
    status = Column(Enum(UserStatusEnum), nullable=False, default=UserStatusEnum.active)
    # Security requirement: only one device/browser may be signed in at a
    # time. Every login/signup generates a fresh value here and embeds it in
    # the issued JWT's "sid" claim; get_current_user (dependencies.py)
    # rejects any token whose "sid" doesn't match the current value, which
    # is exactly what "logged in elsewhere" needs — a stateless JWT alone
    # can't be invalidated early, so this one stateful field is what makes
    # that possible without a full session-store rewrite.
    current_session_id = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    founder_profile = relationship("FounderProfile", back_populates="user", uselist=False)
    investor_profile = relationship("InvestorProfile", back_populates="user", uselist=False)


class StageEnum(str, enum.Enum):
    idea = "idea"
    preseed = "preseed"
    seed = "seed"
    series_a = "series_a"
    series_b_plus = "series_b_plus"


class VerificationTierEnum(str, enum.Enum):
    basic = "basic"
    business_verified = "business_verified"
    investor_ready = "investor_ready"


class FounderProfile(Base):
    """
    One-to-one with a founder User. Per PRD 3.2 — the profile a founder
    builds and publishes for investors to discover.
    """

    __tablename__ = "founder_profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False)

    startup_name = Column(String, nullable=False)
    tagline = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    sector = Column(String, nullable=True)
    stage = Column(Enum(StageEnum), nullable=False, default=StageEnum.idea)
    funding_ask_min = Column(Numeric(14, 2), nullable=True)
    funding_ask_max = Column(Numeric(14, 2), nullable=True)
    location_country = Column(String, nullable=True)
    location_city = Column(String, nullable=True)
    pitch_deck_url = Column(String, nullable=True)
    demo_video_url = Column(String, nullable=True)
    profile_picture_url = Column(String, nullable=True)
    gallery_image_urls = Column(ARRAY(String), nullable=False, default=list)
    startup_link = Column(String, nullable=True)

    # Founder's choice, off by default per PRD 3.4 — contact stays gated
    # behind an accepted connection request unless they opt in here.
    contact_visibility = Column(String, nullable=False, default="private")
    verification_tier = Column(Enum(VerificationTierEnum), nullable=False, default=VerificationTierEnum.basic)
    published = Column(Boolean, nullable=False, default=False)
    # Per PRD 3.5 (Founder Interview/Spotlight Program). Admin-set only —
    # see require_role("admin") on PATCH /{id}/spotlight in
    # routers/founder_profiles.py. No self-nomination workflow yet; that's
    # the "applications go into an admin review queue" piece of 3.5, which
    # is a bigger, separate admin-panel build.
    is_spotlighted = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="founder_profile")


class InvestorTypeEnum(str, enum.Enum):
    angel = "angel"
    vc = "vc"
    fund = "fund"
    family_office = "family_office"
    other = "other"


class InvestorProfile(Base):
    """
    One-to-one with an investor User. Mirrors FounderProfile — per PRD 2.2's
    investor onboarding fields (investor type, check size range, sectors/
    geographies of interest), which were captured as onboarding questions
    in the PRD but never actually got a profile model until now. Lets
    founders discover and pitch investors the same way investors discover
    founders (previously one-directional only).
    """

    __tablename__ = "investor_profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False)

    investor_type = Column(Enum(InvestorTypeEnum), nullable=False, default=InvestorTypeEnum.angel)
    firm_name = Column(String, nullable=True)
    bio = Column(Text, nullable=True)
    check_size_min = Column(Numeric(14, 2), nullable=True)
    check_size_max = Column(Numeric(14, 2), nullable=True)
    sectors_of_interest = Column(ARRAY(String), nullable=False, default=list)
    geographies_of_interest = Column(ARRAY(String), nullable=False, default=list)
    profile_picture_url = Column(String, nullable=True)
    linkedin_url = Column(String, nullable=True)

    # Same opt-in-visibility pattern as FounderProfile.published — an
    # investor profile a founder can browse only exists once this is true.
    # This *is* the consent mechanism discussed for this feature: nothing
    # about an investor is discoverable until they choose to publish.
    published = Column(Boolean, nullable=False, default=False)
    # Same gating pattern as FounderProfile.contact_visibility.
    contact_visibility = Column(String, nullable=False, default="private")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="investor_profile")


class ConnectionStatusEnum(str, enum.Enum):
    pending = "pending"
    accepted = "accepted"
    declined = "declined"


class ConnectionInitiatorEnum(str, enum.Enum):
    investor = "investor"
    founder = "founder"


class ConnectionRequest(Base):
    """
    A connection request between one investor and one founder. Originally
    investor-initiated only (per PRD 3.4); `initiator` now tracks which
    side started it, since founders can also pitch investors. Whichever
    side did *not* initiate is the one who must accept/decline — enforced
    in routers/connections.py, not here.
    """

    __tablename__ = "connection_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    investor_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    founder_profile_id = Column(UUID(as_uuid=True), ForeignKey("founder_profiles.id"), nullable=False)
    # Plain string rather than a native Postgres enum, deliberately — this
    # column is being added to an *existing* table via a manual ALTER
    # (migration_add_bidirectional_connections.sql), and matching the
    # convention contact_visibility already uses elsewhere in this file
    # avoids having to also CREATE TYPE a matching native enum in that
    # migration for a column this low-stakes.
    initiator = Column(String, nullable=False, default=ConnectionInitiatorEnum.investor.value)
    message = Column(Text, nullable=True)
    status = Column(Enum(ConnectionStatusEnum), nullable=False, default=ConnectionStatusEnum.pending)
    contact_revealed_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    investor = relationship("User", foreign_keys=[investor_id])
    founder_profile = relationship("FounderProfile")

    __table_args__ = (
        # One open request per investor-founder pair, regardless of which
        # side initiated it — prevents either side from spamming repeat
        # requests at the same counterparty.
        UniqueConstraint("investor_id", "founder_profile_id", name="uq_connection_investor_founder"),
    )


class Message(Base):
    """
    In-platform chat, scoped to a single ConnectionRequest. Only exists —
    and is only ever readable/writable — once that connection is accepted,
    per PRD 3.4's "early conversation without exposing personal contact
    info immediately" safety layer. Enforced in routers/messages.py, not
    here (this table has no independent notion of who's allowed to see it).
    """

    __tablename__ = "messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    connection_id = Column(UUID(as_uuid=True), ForeignKey("connection_requests.id"), nullable=False, index=True)
    sender_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    read_at = Column(DateTime(timezone=True), nullable=True)

    connection = relationship("ConnectionRequest")
    sender = relationship("User")


class FounderProfileView(Base):
    """
    One row per profile view — powers the founder-side analytics chart
    (PRD 7: 'success metrics' needs *some* engagement signal, and profile
    views are the simplest one that doesn't need new user-facing UI).
    Written from GET /founder-profiles/{id} in routers/founder_profiles.py,
    skipped when the viewer is the profile's own owner.
    """

    __tablename__ = "founder_profile_views"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    founder_profile_id = Column(UUID(as_uuid=True), ForeignKey("founder_profiles.id"), nullable=False, index=True)
    viewer_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class SavedFounderProfile(Base):
    """An investor's watchlist entry. Per PRD 3.3 ('save/bookmark founder profiles')."""

    __tablename__ = "saved_founder_profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    investor_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    founder_profile_id = Column(UUID(as_uuid=True), ForeignKey("founder_profiles.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    founder_profile = relationship("FounderProfile")

    __table_args__ = (
        UniqueConstraint("investor_id", "founder_profile_id", name="uq_saved_investor_founder"),
    )
