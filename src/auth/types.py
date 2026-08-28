"""Internal result types passed from the auth service back to its routes."""

from typing import NamedTuple

from src.auth.models import UserSession
from src.users.models import User


class LoginResult(NamedTuple):
    user: User
    session: UserSession
    access_token: str
    refresh_token: str
    new_device: bool


class RefreshResult(NamedTuple):
    user_id: int
    session: UserSession
    access_token: str
    refresh_token: str
