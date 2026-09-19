from typing import Optional

from fastapi import Cookie, Depends, HTTPException

from crawler import db
from . import queries
from .models import User

SESSION_COOKIE_NAME = "tdoc_session"


def get_current_user(
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME)
) -> Optional[User]:
    """Every call re-checks the session against the database — including
    the user's current status — rather than trusting anything cached in
    the cookie itself. That's what makes an admin's revoke actually
    immediate (see set_user_status in queries.py)."""
    if not session_token:
        return None
    with db.session() as conn:
        return queries.get_user_by_session_token(conn, session_token)


def require_user(user: Optional[User] = Depends(get_current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    return user
