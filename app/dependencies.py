from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.security import decode_access_token

# tokenUrl points at the login endpoint purely so FastAPI's auto-generated
# /docs page renders an "Authorize" button that works. The frontend does not
# use this OAuth2 form flow — it logs in via plain JSON and stores the token
# itself (see app/lib/api.ts on the frontend).
_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")
# auto_error=False so routes that are public-but-personalized (e.g. the
# founder directory showing "is_saved" for a logged-in investor) don't force
# a 401 on anonymous visitors — see get_current_user_optional below.
_oauth2_scheme_optional = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


def get_current_user(
    token: str = Depends(_oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        if user_id is None:
            raise credentials_error
    except JWTError:
        raise credentials_error

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise credentials_error

    return user


def get_current_user_optional(
    token: Optional[str] = Depends(_oauth2_scheme_optional),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """
    Same as get_current_user, but returns None instead of raising when no
    token is provided — for public routes that only need to know *who's
    asking* if someone happens to be logged in. An invalid/expired token
    still resolves to None here rather than erroring, since the route is
    public either way; if the caller wanted to require it, they'd use
    get_current_user instead.
    """
    if token is None:
        return None
    try:
        payload = decode_access_token(token)
    except JWTError:
        return None

    user_id = payload.get("sub")
    if user_id is None:
        return None
    return db.query(User).filter(User.id == user_id).first()


def require_role(*allowed_roles: str):
    """
    Dependency factory for endpoints that need to restrict access by role,
    e.g. `Depends(require_role("admin"))` on admin-only routes.
    """

    def _check_role(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action.",
            )
        return current_user

    return _check_role
