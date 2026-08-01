"""Broker credentials encrypted at rest.

The bug this closes is not subtle once stated: ``POST /broker/connections`` calls
``SecretStore.put``, and the store chosen for staging and production raises
``NotImplementedError`` on write. Linking a broker returned a 500 in every deployed
environment — and every test passed, because every test used the in-memory store.

So the tests below run against the *real* store, with a real database and a real cipher.
Two of them assert the property that motivates encryption at all: that what lands in the
table is not the password. That is checkable, and checking it is the only way to know the
cipher is actually wired in rather than merely imported.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.errors import NotFoundError
from app.core.ids import uuid7
from app.infrastructure.db.models.secrets import StoredSecret
from app.infrastructure.secrets.encrypted import (
    EncryptedDatabaseSecretStore,
    EncryptionKeyError,
    build_cipher,
)
from app.infrastructure.secrets.store import credential_reference

pytestmark = pytest.mark.asyncio

#: A realistic credential document — the exact shape `credentials_from_secret` requires.
CREDENTIAL = {
    "username": "a-trader",
    "password": "correct-horse-battery-staple",
    "cid": "8342",
    "secret": "f4c0ffee-dead-beef-1234-0123456789ab",
    "device_id": None,
}


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[Any]]:
    engine = create_async_engine(os.environ["LEDGERLINE_TEST_DATABASE_URL"])
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def store(
    sessions: async_sessionmaker[Any],
) -> AsyncIterator[EncryptedDatabaseSecretStore]:
    made = EncryptedDatabaseSecretStore(sessions, build_cipher([Fernet.generate_key().decode()]))
    yield made
    async with sessions() as session:
        await session.execute(text("DELETE FROM stored_secrets"))
        await session.commit()


async def test_a_secret_round_trips(store: EncryptedDatabaseSecretStore) -> None:
    reference = credential_reference("tradovate", uuid7())

    await store.put(reference, CREDENTIAL)

    assert await store.get(reference) == CREDENTIAL


async def test_the_password_is_not_in_the_database(
    store: EncryptedDatabaseSecretStore, sessions: async_sessionmaker[Any]
) -> None:
    """The whole point, asserted against the bytes actually stored.

    A store that imported a cipher and forgot to call it would pass the round-trip test
    above — `json.loads(json.dumps(x))` is also a round trip.
    """
    reference = credential_reference("tradovate", uuid7())
    await store.put(reference, CREDENTIAL)

    async with sessions() as session:
        row = (
            await session.execute(
                select(StoredSecret).where(StoredSecret.reference == reference)
            )
        ).scalar_one()

    assert CREDENTIAL["password"] not in row.ciphertext.decode("utf-8", "replace")
    assert CREDENTIAL["secret"] not in row.ciphertext.decode("utf-8", "replace")
    assert b"username" not in row.ciphertext


async def test_a_second_put_replaces_rather_than_conflicts(
    store: EncryptedDatabaseSecretStore,
) -> None:
    """Re-linking with a changed password must work.

    An insert would fail on the primary key at the point where the caller has *already*
    verified the new credentials against the broker — leaving a user who did everything
    right staring at an error.
    """
    reference = credential_reference("tradovate", uuid7())
    await store.put(reference, CREDENTIAL)

    await store.put(reference, {**CREDENTIAL, "password": "a-new-password"})

    assert (await store.get(reference))["password"] == "a-new-password"


async def test_a_missing_reference_is_not_found(
    store: EncryptedDatabaseSecretStore,
) -> None:
    with pytest.raises(NotFoundError):
        await store.get(credential_reference("tradovate", uuid7()))


async def test_delete_removes_it(store: EncryptedDatabaseSecretStore) -> None:
    reference = credential_reference("tradovate", uuid7())
    await store.put(reference, CREDENTIAL)

    await store.delete(reference)

    with pytest.raises(NotFoundError):
        await store.get(reference)


async def test_rotation_reads_old_keys_and_writes_the_new_one(
    sessions: async_sessionmaker[Any],
) -> None:
    """The property that makes rotation a prepend rather than a migration.

    `MultiFernet` decrypts with any configured key and encrypts with the first, so adding
    a key leaves every existing secret readable — which is what allows re-writing them at
    leisure instead of during a maintenance window.
    """
    old_key, new_key = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    reference = credential_reference("tradovate", uuid7())

    before = EncryptedDatabaseSecretStore(sessions, build_cipher([old_key]))
    await before.put(reference, CREDENTIAL)

    # New key first, old key retained: exactly what a rotation deploys.
    after = EncryptedDatabaseSecretStore(sessions, build_cipher([new_key, old_key]))
    assert await after.get(reference) == CREDENTIAL

    # Re-writing moves it onto the new key, after which the old one is droppable.
    await after.put(reference, CREDENTIAL)
    only_new = EncryptedDatabaseSecretStore(sessions, build_cipher([new_key]))
    assert await only_new.get(reference) == CREDENTIAL

    async with sessions() as session:
        await session.execute(text("DELETE FROM stored_secrets"))
        await session.commit()


async def test_a_key_that_cannot_decrypt_says_so(
    sessions: async_sessionmaker[Any],
) -> None:
    """Undecryptable is a configuration error, not a missing secret.

    Reporting "no secret stored" would send someone hunting for a row that is sitting
    right there, and — worse — a caller might treat it as "never linked" and prompt the
    user to enter their credentials again.
    """
    reference = credential_reference("tradovate", uuid7())
    written = EncryptedDatabaseSecretStore(sessions, build_cipher([Fernet.generate_key().decode()]))
    await written.put(reference, CREDENTIAL)

    wrong = EncryptedDatabaseSecretStore(sessions, build_cipher([Fernet.generate_key().decode()]))
    with pytest.raises(EncryptionKeyError, match="cannot be decrypted"):
        await wrong.get(reference)

    async with sessions() as session:
        await session.execute(text("DELETE FROM stored_secrets"))
        await session.commit()
