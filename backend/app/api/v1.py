"""Version 1 of the API, aggregated under `settings.API_V1_PREFIX`.

Prefixes live here rather than on each router so the version boundary is visible in
one place: adding `/api/v2` is a new module beside this one, not a change to every
route. `main.py` mounts this router and applies the configured prefix, so the
environment can move it without touching the routes.
"""

from fastapi import APIRouter

from app.api.attachments import router as attachments_router
from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.customers import router as customers_router
from app.api.messages import router as messages_router
from app.api.notifications import router as notifications_router
from app.api.sla import router as sla_router
from app.api.tickets import router as tickets_router
from app.api.users import router as users_router

router = APIRouter()

router.include_router(auth_router, prefix="/auth", tags=["auth"])
router.include_router(users_router, prefix="/users", tags=["users"])
router.include_router(customers_router, prefix="/customers", tags=["customers"])
router.include_router(tickets_router, prefix="/tickets", tags=["tickets"])
# Mounted under the same prefix as tickets rather than a `/messages` root: a message is
# reachable exactly when its ticket is, and the URL says so. Its paths carry the
# `{ticket_id}` segment themselves.
router.include_router(messages_router, prefix="/tickets", tags=["messages"])
# No prefix, because this router holds both shapes: the list is
# `/tickets/{ticket_id}/attachments` and the download is `/attachments/{id}`. Both paths
# are written in full, which is what lets the download exist at all — a download has only
# an id, so it cannot live under `/tickets`.
router.include_router(attachments_router, tags=["attachments"])
router.include_router(audit_router, tags=["audit"])
# No prefix, because the paths are written in full inside the router: the collection is
# `/notifications` and the per-row action is `/notifications/{id}/read`. Both are needed,
# and a prefix would only be re-stated.
router.include_router(notifications_router, tags=["notifications"])
# `/sla/policies` and `/sla/policies/{priority}` — spec §6 lists `/api/v1/sla/*`. A prefix
# here rather than full paths inside the module, because unlike notifications this router
# has exactly one resource under it and no second shape to accommodate.
router.include_router(sla_router, prefix="/sla", tags=["sla"])
