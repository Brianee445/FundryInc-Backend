from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import AuthProviderEnum, User, UserRoleEnum
from app.schemas import GoogleAuthRequest, LoginRequest, SignupRequest, TokenResponse, UserResponse
from app.security import (
    GoogleTokenError,
    create_access_token,
    hash_password,
    verify_google_id_token,
    verify_password,
)

router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == payload.email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        auth_provider=AuthProviderEnum.local,
        role=UserRoleEnum(payload.role),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(subject=str(user.id), role=user.role.value)
    return TokenResponse(access_token=token, user=UserResponse.model_validate(user))


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    invalid_credentials = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect email or password.",
    )

    user = db.query(User).filter(User.email == payload.email).first()
    if user is None or user.password_hash is None:
        # `password_hash is None` covers Google-only accounts: same generic
        # error as "wrong password" so we don't leak which emails exist or
        # how they signed up.
        raise invalid_credentials

    if not verify_password(payload.password, user.password_hash):
        raise invalid_credentials

    if user.status.value != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is not active. Contact support for help.",
        )

    token = create_access_token(subject=str(user.id), role=user.role.value)
    return TokenResponse(access_token=token, user=UserResponse.model_validate(user))


@router.post("/google", response_model=TokenResponse)
def google_auth(payload: GoogleAuthRequest, db: Session = Depends(get_db)):
    try:
        claims = verify_google_id_token(payload.id_token)
    except GoogleTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not verify Google sign-in. Please try again.",
        )

    google_sub = claims["sub"]
    email = claims["email"]

    # A returning user is matched by Google's stable subject id first (in
    # case they've changed their Google email), falling back to email for
    # someone who first signed up with a local password and is now linking
    # Google sign-in to the same address.
    user = db.query(User).filter(User.google_sub == google_sub).first()
    if user is None:
        user = db.query(User).filter(User.email == email).first()

    if user is not None:
        if user.google_sub is None:
            user.google_sub = google_sub  # link Google to an existing local account
            db.commit()

        if user.status.value != "active":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This account is not active. Contact support for help.",
            )

        token = create_access_token(subject=str(user.id), role=user.role.value)
        return TokenResponse(access_token=token, user=UserResponse.model_validate(user))

    # No existing account — this is a first-time Google sign-in, which is
    # only valid as a signup if the client told us which role to create.
    if payload.role is None:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="Select whether you're a founder or an investor to finish creating your account.",
        )

    user = User(
        email=email,
        password_hash=None,
        auth_provider=AuthProviderEnum.google,
        google_sub=google_sub,
        role=UserRoleEnum(payload.role),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(subject=str(user.id), role=user.role.value)
    return TokenResponse(access_token=token, user=UserResponse.model_validate(user))


@router.get("/me", response_model=UserResponse)
def read_current_user(current_user: User = Depends(get_current_user)):
    return current_user
