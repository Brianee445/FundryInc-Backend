from pydantic import BaseModel, EmailStr
from typing import Literal

class WaitlistCreate(BaseModel):
    email: EmailStr
    role: Literal["founder", "investor", "advisor", "accelerator"]

class WaitlistResponse(BaseModel):
    success: bool
    message: str
