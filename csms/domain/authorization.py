"""idTag validation, shared by Authorize and StartTransaction.

The rules are the ones OCPP 1.6 defines for idTagInfo.status, in the order a
charger expects them to be applied.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiosqlite

from ..db.database import from_db, utcnow
from ..db.enums import AuthorizationStatus
from ..repository import tags as tags_repo

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AuthorizationResult:
    status: AuthorizationStatus
    id_tag: str
    expiry_date: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status is AuthorizationStatus.ACCEPTED

    def to_id_tag_info(self) -> dict[str, Any]:
        """The idTagInfo object OCPP expects in a response."""
        info: dict[str, Any] = {"status": self.status.value}
        if self.expiry_date:
            info["expiry_date"] = self.expiry_date
        return info


async def authorize(
    conn: aiosqlite.Connection,
    id_tag: str,
    *,
    allow_concurrent: bool = False,
) -> AuthorizationResult:
    """Decide whether this tag may start or stop a transaction.

    `allow_concurrent` is set when the caller is stopping a transaction: the
    tag legitimately has a session open in that case, so ConcurrentTx would be
    the wrong answer.
    """
    row = await tags_repo.get(conn, id_tag)

    # Unknown tag. Invalid rather than Blocked -- Blocked implies we know the
    # tag and have chosen to refuse it, which would be misleading here.
    if row is None:
        log.info("Authorization refused: unknown tag %s", id_tag)
        return AuthorizationResult(AuthorizationStatus.INVALID, id_tag)

    status = AuthorizationStatus(row["status"])

    # Expiry beats the stored status: a tag marked Accepted whose date has
    # passed is Expired, and we write that back so the tag list stays honest.
    expiry = from_db(row["expiry_date"])
    if expiry is not None and expiry <= utcnow():
        await tags_repo.expire_if_due(conn, id_tag)
        log.info("Authorization refused: tag %s expired at %s", id_tag, row["expiry_date"])
        return AuthorizationResult(AuthorizationStatus.EXPIRED, id_tag)

    if status is not AuthorizationStatus.ACCEPTED:
        log.info("Authorization refused: tag %s is %s", id_tag, status.value)
        return AuthorizationResult(status, id_tag)

    if not allow_concurrent and await tags_repo.has_active_session(conn, id_tag):
        log.info("Authorization refused: tag %s already has an open session", id_tag)
        return AuthorizationResult(AuthorizationStatus.CONCURRENT_TX, id_tag)

    # A card says nothing about which car is plugged in -- that is bound to
    # the session when the cable goes in, not looked up from the credential.
    return AuthorizationResult(
        status=AuthorizationStatus.ACCEPTED,
        id_tag=id_tag,
        expiry_date=row["expiry_date"],
    )
