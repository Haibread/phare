"""Per-profile source-token storage (encrypted at rest).

Lets a profile sync from Trakt/Plex/Jellyfin without re-pasting a token each time. Storage is a
no-op when encryption is unavailable (no secret configured) — the caller falls back to the
token supplied in the request.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.core.config import Settings
from phare.core.crypto import decrypt, encrypt, encryption_available
from phare.db.models import SourceToken

logger = logging.getLogger(__name__)


def store_source_token(
    session: Session, settings: Settings, profile_id: uuid.UUID, source: str, token: str
) -> bool:
    """Encrypt + persist a token for (profile, source). Returns False if encryption is off."""
    if not encryption_available(settings):
        return False
    ciphertext = encrypt(settings, token)
    existing = session.scalar(
        select(SourceToken).where(
            SourceToken.profile_id == profile_id, SourceToken.source == source
        )
    )
    if existing is None:
        session.add(SourceToken(profile_id=profile_id, source=source, token_encrypted=ciphertext))
    else:
        existing.token_encrypted = ciphertext
    session.flush()
    logger.info("tokens.stored", extra={"profile_id": str(profile_id), "source": source})
    return True


def get_source_token(
    session: Session, settings: Settings, profile_id: uuid.UUID, source: str
) -> str | None:
    """Fetch + decrypt a stored token, or ``None`` if absent / undecryptable."""
    if not encryption_available(settings):
        return None
    row = session.scalar(
        select(SourceToken).where(
            SourceToken.profile_id == profile_id, SourceToken.source == source
        )
    )
    if row is None:
        return None
    return decrypt(settings, row.token_encrypted)
