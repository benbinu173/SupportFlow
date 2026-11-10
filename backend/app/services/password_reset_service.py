"""Email recovery for a specific account, without disclosing account lookup results."""

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import get_settings
from app.core.exceptions import InvalidResetTokenError
from app.core.security import decode_password_reset_token, hash_password
from app.core.tenancy import RequestOrigin
from app.models.enums import AuditAction, OrganizationStatus
from app.models.organization import Organization
from app.models.user import User
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.services import audit_service
from app.websocket.manager import manager
from app.workers.email_tasks import send_password_reset_email

logger = structlog.get_logger(__name__)


async def request_reset(session: AsyncSession, email: str) -> None:
    # Public recovery has no authenticated tenant yet, just like login. Each eligible
    # account receives its own link; the email recipient chooses the named workspace.
    result = await session.execute(
        select(User)
        .join(Organization, Organization.id == User.organization_id)
        .where(
            User.email == email,
            User.is_active.is_(True),
            Organization.status == OrganizationStatus.ACTIVE,
        )
        .order_by(User.created_at, User.id)
        .limit(5)
    )
    expires_at = datetime.now(UTC) + timedelta(minutes=get_settings().PASSWORD_RESET_EXPIRE_MINUTES)
    for user in result.scalars():
        try:
            # No recovery credential goes into Redis. The worker generates the signed
            # link only after verifying the account and the requested version again.
            send_password_reset_email.apply_async(
                args=[str(user.id), user.token_version, expires_at.timestamp()], expires=expires_at
            )
        except Exception as exc:
            # Queue availability must not turn the public response into an account oracle.
            logger.warning(
                "password_reset_enqueue_failed", user_id=str(user.id), error_type=type(exc).__name__
            )


async def reset_password(
    session: AsyncSession, token: str, password: str, *, origin: RequestOrigin | None = None
) -> None:
    claims = decode_password_reset_token(token)
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
        or user.token_version != claims.token_version
    ):
        raise InvalidResetTokenError()

    # The row lock makes concurrent consumption of this link (or a sibling) exclusive.
    user.password_hash = hash_password(password)
    user.token_version += 1
    revoked = await RefreshTokenRepository(session).revoke_all_for_user(user.id)
    audit_service.record(
        session,
        organization_id=user.organization_id,
        actor_user_id=user.id,
        actor_email=user.email,
        action=AuditAction.PASSWORD_RESET,
        target_type="user",
        target_id=user.id,
        metadata={"source": "email_recovery", "revoked_refresh_tokens": revoked},
        origin=origin,
    )
    await session.commit()
    manager.revoke_user(user.organization_id, user.id, user.token_version)
    logger.info(
        "password_reset_completed", user_id=str(user.id), organization_id=str(user.organization_id)
    )
