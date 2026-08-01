"""A secret store that can actually be written to in a deployment.

``EnvironmentSecretStore`` reads credentials from environment variables and raises
``NotImplementedError`` on ``put``, which is honest — you cannot write to the process
environment and have it persist. The consequence went unnoticed: ``POST
/broker/connections`` calls ``put``, so **linking a broker returned a 500 in staging and
production**. Every test used the in-memory store, and the endpoint had never been
exercised against the store it would actually run with.

This is the writable one. Secrets are encrypted with Fernet — AES-128-CBC with an
HMAC-SHA256 authentication tag, from ``cryptography``, chosen because the alternative to a
vetted authenticated construction is inventing one — and the ciphertext is stored in
Postgres. The key lives only in the environment.

**What that protects against, precisely.** A stolen database dump, a leaked backup, a
replica someone forgot was public, an operator with read access to the table: none of
those yield broker credentials, because the key is not in the database. That is the
threat this addresses and it is the common one.

**What it does not protect against.** Anyone who can read the application's environment or
its memory can decrypt everything. This is envelope encryption with a single static key,
not a hardware-backed vault: there is no per-secret key, no automatic rotation, and no
audit trail of decryptions. A deployment holding real money at scale should point
:class:`SecretStore` at AWS Secrets Manager or equivalent — the Protocol exists so that is
a substitution rather than a rewrite. This is the honest middle: strictly better than
plaintext, strictly worse than a vault, and *writable*, which the alternative was not.

**Rotation** is supported through ``MultiFernet``: set ``LEDGERLINE_SECRET_ENCRYPTION_KEYS``
to a comma-separated list, newest first. Reads try every key; writes always use the first.
Rotating therefore means prepending a new key, re-putting each secret, then dropping the
old one — with no window where existing secrets cannot be read.
"""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.infrastructure.db.models.secrets import StoredSecret

logger = get_logger(__name__)


class EncryptionKeyError(RuntimeError):
    """Raised when the configured keys are missing or malformed.

    Deliberately fatal rather than falling back to plaintext. A store that quietly stops
    encrypting is the worst outcome available: everything keeps working, and nobody finds
    out until the dump leaks.
    """


def build_cipher(keys: list[str]) -> MultiFernet:
    """A cipher over one or more base64 keys, newest first.

    ``MultiFernet`` decrypts with any key and encrypts with the first, which is what makes
    rotation a prepend rather than a migration.
    """
    if not keys:
        raise EncryptionKeyError(
            "no encryption key configured; generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"`'
        )
    try:
        return MultiFernet([Fernet(key.strip().encode()) for key in keys if key.strip()])
    except (ValueError, TypeError) as exc:
        raise EncryptionKeyError(
            "encryption key is not a valid Fernet key (32 url-safe base64-encoded bytes)"
        ) from exc


class EncryptedDatabaseSecretStore:
    """Broker credentials, encrypted at rest in Postgres.

    Takes a session *factory* rather than a session. The store outlives any one request —
    the sync worker reads credentials with no request in scope at all — and a store
    holding a borrowed session would either leak it or use it after the request that
    owned it had closed.
    """

    def __init__(
        self, sessions: async_sessionmaker[Any], cipher: MultiFernet
    ) -> None:
        self._sessions = sessions
        self._cipher = cipher

    async def get(self, reference: str) -> dict[str, Any]:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(StoredSecret).where(StoredSecret.reference == reference)
                )
            ).scalar_one_or_none()

        if row is None:
            raise NotFoundError(f"no secret stored for {reference!r}")

        try:
            plaintext = self._cipher.decrypt(row.ciphertext)
        except InvalidToken as exc:
            # A key that cannot decrypt is a configuration error, not a missing secret,
            # and conflating the two would send someone hunting for a row that is right
            # there. Raised rather than logged-and-skipped for the same reason.
            raise EncryptionKeyError(
                f"the secret for {reference!r} cannot be decrypted with any configured "
                "key — was LEDGERLINE_SECRET_ENCRYPTION_KEYS rotated without re-putting?"
            ) from exc

        parsed = json.loads(plaintext)
        if not isinstance(parsed, dict):  # pragma: no cover - only a corrupt row
            raise NotFoundError(f"secret for {reference!r} is not a JSON object")
        return parsed

    async def put(self, reference: str, secret: dict[str, Any]) -> None:
        """Encrypt and store, replacing any existing secret for this reference.

        Upsert rather than insert: re-linking a connection whose credentials changed must
        replace them, and an insert would fail on the primary key while the caller has
        already verified the new credentials against the broker.
        """
        ciphertext = self._cipher.encrypt(json.dumps(secret).encode())

        async with self._sessions() as session:
            statement = insert(StoredSecret).values(
                reference=reference, ciphertext=ciphertext
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["reference"],
                    set_={"ciphertext": statement.excluded.ciphertext},
                )
            )
            await session.commit()

        # The reference, never the secret and never its length — a length is a hint about
        # the credential and costs nothing to omit.
        logger.info("secrets.stored", reference=reference)

    async def delete(self, reference: str) -> None:
        async with self._sessions() as session:
            await session.execute(
                sql_delete(StoredSecret).where(StoredSecret.reference == reference)
            )
            await session.commit()
        logger.info("secrets.deleted", reference=reference)
