from sqlalchemy import Column, String, DateTime, Enum, BigInteger
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
