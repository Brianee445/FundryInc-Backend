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
from sqlalchemy.dialects.postgresql import UUID
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
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    founder_profile = relationship("FounderProfile", back_populates="user", uselist=False)


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

    # Founder's choice, off by default per PRD 3.4 — contact stays gated
    # behind an accepted connection request unless they opt in here.
    contact_visibility = Column(String, nullable=False, default="private")
    verification_tier = Column(Enum(VerificationTierEnum), nullable=False, default=VerificationTierEnum.basic)
    published = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="founder_profile")


class ConnectionStatusEnum(str, enum.Enum):
    pending = "pending"
    accepted = "accepted"
    declined = "declined"


class ConnectionRequest(Base):
    """
    An investor's request to connect with a founder. Per PRD 3.4: contact
    details only become visible to the investor once the founder accepts.
    """

    __tablename__ = "connection_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    investor_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    founder_profile_id = Column(UUID(as_uuid=True), ForeignKey("founder_profiles.id"), nullable=False)
    message = Column(Text, nullable=True)
    status = Column(Enum(ConnectionStatusEnum), nullable=False, default=ConnectionStatusEnum.pending)
    contact_revealed_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    investor = relationship("User", foreign_keys=[investor_id])
    founder_profile = relationship("FounderProfile")

    __table_args__ = (
        # An investor can only have one open request per founder profile —
        # prevents spamming the same founder with repeat requests.
        UniqueConstraint("investor_id", "founder_profile_id", name="uq_connection_investor_founder"),
    )


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
