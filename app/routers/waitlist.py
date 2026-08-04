from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import WaitlistEntry, RoleEnum
from app.schemas import WaitlistCreate, WaitlistResponse

router = APIRouter(prefix="/api/v1/waitlist", tags=["Waitlist"])

@router.post("/", response_model=WaitlistResponse)
def join_waitlist(
    payload: WaitlistCreate,
    db: Session = Depends(get_db)
):
    existing = db.query(WaitlistEntry).filter(WaitlistEntry.email == payload.email).first()
    
    if existing:
        raise HTTPException(status_code=409, detail="Email already exists on the waitlist.")
    
    new_entry = WaitlistEntry(
        email=payload.email,
        role=RoleEnum(payload.role)
    )
    db.add(new_entry)
    db.commit()
    
    return WaitlistResponse(success=True, message="Successfully joined the waitlist.")
