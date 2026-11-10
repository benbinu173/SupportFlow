"""Confirm ownership of one account's email; public resends do not disclose account state."""

import hmac
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import get_settings
from app.core.exceptions import InvalidVerificationTokenError
from app.core.security import decode_email_verification_token, email_verification_fingerprint
from app.core.tenancy import RequestOrigin
from app.models.enums import AuditAction, OrganizationStatus
from app.models.organization import Organization
from app.models.user import User
from app.services import audit_service
from app.workers.email_tasks import send_verification_email

logger = structlog.get_logger(__name__)


def enqueue_verification(user: User) -> None:
    """Call only after the account commits. The worker creates the actual credential."""
    if not user.is_active or user.email_verified_at is not None:
        return
    expires_at = datetime.now(UTC) + timedelta(
        minutes=get_settings().EMAIL_VERIFICATION_EXPIRE_MINUTES
    )
    try:
        send_verification_email.apply_async(
            args=[
                str(user.id),
                str(user.organization_id),
                email_verification_fingerprint(user.email),
                expires_at.timestamp(),
            ],
            expires=expires_at,
        )
    except Exception as exc:
        # Creation remains committed and resend remains available after a queue outage.
        # A public resend must not reveal whether an account was found.
        logger.warning(
            "verification_email_enqueue_failed", user_id=str(user.id), error_type=type(exc).__name__
        )


async def resend_verification(session: AsyncSession, email: str) -> None:
    result = await session.execute(
        select(User)
        .join(Organization, Organization.id == User.organization_id)
        .where(
            User.email == email,
            User.email_verified_at.is_(None),
            User.is_active.is_(True),
            Organization.status == OrganizationStatus.ACTIVE,
        )
        .order_by(User.created_at, User.id)
        .limit(5)
    )
    for user in result.scalars():
        enqueue_verification(user)


async def verify_email(
    session: AsyncSession, token: str, *, origin: RequestOrigin | None = None
) -> User:
    claims = decode_email_verification_token(token)
    result = await session.execute(
        select(User)
        .where(User.id == claims.user_id, User.organization_id == claims.organization_id)
        .options(joinedload(User.organization))
        .with_for_update(of=User)
    )
    user = result.scalar_one_or_none()
    if (
        user is None
        or not user.is_active
        or user.organization.status != OrganizationStatus.ACTIVE
        or user.email_verified_at is not None
        or not hmac.compare_digest(claims.email_hash, email_verification_fingerprint(user.email))
    ):
        raise InvalidVerificationTokenError()

    # The lock makes consumption of this link and its resent siblings exclusive.
    # Verification changes neither the password nor anyone's existing session.
    user.email_verified_at = datetime.now(UTC)
    audit_service.record(
        session,
        organization_id=user.organization_id,
        actor_user_id=user.id,
        actor_email=user.email,
        action=AuditAction.EMAIL_VERIFIED,
        target_type="user",
        target_id=user.id,
        metadata={"source": "email_link"},
        origin=origin,
    )
    await session.commit()
    logger.info("email_verified", user_id=str(user.id), organization_id=str(user.organization_id))
    return user
