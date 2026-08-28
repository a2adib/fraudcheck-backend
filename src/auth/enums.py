from enum import StrEnum


class TokenDelivery(StrEnum):
    """
    Where the tokens are handed back (FR-1.9).

    ``JSON`` puts them in the response body; ``COOKIE`` puts them in ``HttpOnly``,
    ``SameSite=strict`` cookies and returns no token string at all (AC-1.11).
    """

    JSON = "json"
    COOKIE = "cookie"


class Channel(StrEnum):
    """The kind of identifier a merchant logged in with (FR-1.7)."""

    EMAIL = "email"
    MOBILE = "mobile"


class PermissionCode(StrEnum):
    """
    Permission codes: dotted, lowercase, ``area.resource.action`` (FR-1.11).

    A code is an opaque string — nothing parses its segments. This enum is the single
    source of truth: ``sync_permission_catalogue()`` mints one ``Permission`` row per
    member, and the guards check membership against the JWT ``pb`` claim.

    **Append-only.** The ``pb`` claim packs granted permissions into a bitmask keyed by
    declaration order, so reordering or deleting a member silently re-points every
    already-issued token at the wrong permission. Retire a code by leaving the member in
    place and commenting it, never by removing it.
    """

    # ── Credential vault (FR-2) ─────────────────────────────────────────────
    CREDENTIAL_CREATE = "credential.create"
    CREDENTIAL_READ = "credential.read"
    CREDENTIAL_UPDATE = "credential.update"
    CREDENTIAL_DELETE = "credential.delete"

    # ── Checks (FR-6, FR-8) ─────────────────────────────────────────────────
    CHECK_RUN = "check.run"
    CHECK_READ = "check.read"

    # ── Bulk (FR-9) ─────────────────────────────────────────────────────────
    BULK_RUN = "bulk.run"

    # ── Public API keys (FR-10) ─────────────────────────────────────────────
    APIKEY_CREATE = "apikey.create"
    APIKEY_READ = "apikey.read"
    APIKEY_DELETE = "apikey.delete"

    # ── Risk review (FR-15) ─────────────────────────────────────────────────
    RISK_ORDER_READ = "risk.order.read"
    RISK_ORDER_REVIEW = "risk.order.review"  # AC-1.13, FR-15.10 — decision override

    # ── Administration (FR-1.11) ────────────────────────────────────────────
    ADMIN_ROLE_CREATE = "admin.role.create"
    ADMIN_ROLE_READ = "admin.role.read"
    ADMIN_ROLE_UPDATE = "admin.role.update"
    ADMIN_ROLE_DELETE = "admin.role.delete"
    ADMIN_USER_READ = "admin.user.read"
    ADMIN_USER_UPDATE = "admin.user.update"
