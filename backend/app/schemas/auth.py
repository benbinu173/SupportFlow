"""Authentication request and response schemas."""

from pydantic import BaseModel, Field

from app.schemas.fields import Email, Password


class RegisterRequest(BaseModel):
    """Create a new organization and its first administrator.

    Registration creates a *tenant*, not just a user. There is no way to join an
    existing organization through this endpoint — that requires an invite, which is
    not built yet — so every registration starts a new one, and the first account in
    it is necessarily an admin. Spec §36.
    """

    organization_name: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    email: Email
    password: Password


class LoginRequest(BaseModel):
    """Credentials submitted to the login endpoint."""

    email: Email

    # A plain `str`, not the validated `Password` type, and that is deliberate.
    #
    # Validating the length here would reject a too-short password with a 422 *before*
    # any credential check, which tells an attacker the policy and distinguishes
    # "wrong length" from "wrong password" in the response. It would also break login
    # for any account whose password predates a policy change. Login's job is to
    # compare against a stored hash, and a hash check needs no policy.
    password: str = Field(max_length=1024)


class TokenResponse(BaseModel):
    """A successful authentication.

    Carries the access token only. The refresh token is set as an `HttpOnly` cookie
    and never appears in a response body, so a cross-site scripting flaw cannot read
    it (ADR-014).
    """

    access_token: str
    # Not a credential — the literal scheme name from RFC 6750, which the client
    # echoes back in the `Authorization` header. Bandit flags the assignment because
    # of the field it sits next to.
    token_type: str = "bearer"  # noqa: S105
    # Seconds until the access token expires. The client cannot decode the token
    # without duplicating JWT logic, and the alternative — assuming a fixed lifetime —
    # is wrong the moment the setting changes.
    expires_in: int


class LogoutResponse(BaseModel):
    """Acknowledges a logout that revoked a session."""

    detail: str = "Signed out."
