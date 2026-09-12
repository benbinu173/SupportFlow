"""Reusable request-field types.

Email and password both need normalization-or-validation that must behave identically
everywhere they appear. Declaring them once as annotated types means `RegisterRequest`
and `UserCreate` cannot drift into accepting different passwords — which is the kind of
inconsistency that only shows up when someone registers one way and is created another.
"""

from typing import Annotated

from pydantic import AfterValidator, EmailStr

from app.core.config import get_settings


def _normalize_email(value: str) -> str:
    """Lowercase and trim an email address.

    Not cosmetic. `users` has a unique constraint on `(organization_id, email)`, and
    that constraint is case-sensitive — so storing what the user typed would let
    `Alice@example.com` and `alice@example.com` coexist as two accounts, and would
    make login fail for anyone who capitalizes their address differently than they
    did when registering.

    Lowercasing the whole address (not just the domain) is technically wrong for the
    rare mail server where the local part is case-sensitive. Every mainstream provider
    treats it as case-insensitive, and the alternative — two accounts that look
    identical to a human — is the worse failure.
    """
    return value.strip().lower()


def _validate_password(value: str) -> str:
    """Enforce the configured length window.

    Length only, no composition rules: current NIST guidance (SP 800-63B) is that
    character-class requirements push users toward predictable substitutions like
    `P@ssw0rd1` without meaningfully raising entropy, while a length floor does raise
    it. The upper bound is a denial-of-service ceiling — Argon2 hashes the whole input,
    so an unbounded field is an unbounded amount of work per request.

    Reads the settings on each call rather than baking the numbers into the class at
    import, so a test that overrides the policy is actually testing the override.
    """
    settings = get_settings()
    if len(value) < settings.PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {settings.PASSWORD_MIN_LENGTH} characters.")
    if len(value) > settings.PASSWORD_MAX_LENGTH:
        raise ValueError(f"Password must be at most {settings.PASSWORD_MAX_LENGTH} characters.")
    return value


# `EmailStr` requires the `email-validator` package; without it Pydantic raises at
# import time, not on first use.
Email = Annotated[EmailStr, AfterValidator(_normalize_email)]
Password = Annotated[str, AfterValidator(_validate_password)]
