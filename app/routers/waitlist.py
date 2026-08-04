from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import WaitlistEntry, RoleEnum
from app.schemas import WaitlistCreate, WaitlistResponse

router = APIRouter(prefix="/api/v1/waitlist", tags=["Waitlist"])

@router.post("/", response_model=WaitlistResponse)
async def join_waitlist(
    payload: WaitlistCreate,
    db: AsyncSession = Depends(get_db)
):
    # Check if exists
    stmt = select(WaitlistEntry).where(WaitlistEntry.email == payload.email)
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()
    
    if existing:
        raise HTTPException(status_code=409, detail="Email already exists on the waitlist.")
    
    new_entry = WaitlistEntry(
        email=payload.email,
        role=RoleEnum(payload.role)
    )
    db.add(new_entry)
    await db.commit()
    
    return WaitlistResponse(success=True, message="Successfully joined the waitlist.")
