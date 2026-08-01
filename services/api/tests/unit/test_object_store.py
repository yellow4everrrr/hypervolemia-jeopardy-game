"""The screenshot store has to outlive the process that wrote to it.

Screenshots are the one part of a trade record that cannot be regenerated. Bars can be
re-fetched and trades rebuilt, but the chart as it looked at the moment of entry exists
once. The store was a dict in the process, and that is wrong in a way neither side can
detect on its own:

* **A restart empties it.** The rows recording each capture are in Postgres and survive.
  So the gallery lists six frames, the list endpoint reports six healthy screenshots, and
  every image 404s. The database and the store disagree, and nothing compares them.
* **Two processes are two stores.** The worker renders a capture into its own dict; the
  API serves images out of a different one. Pressing "Re-capture" appears to do nothing,
  permanently.

Both look exactly like a broken capture pipeline, and both were investigated as one. The
fix is a directory, and the test that matters is
:func:`test_a_new_store_reads_what_an_earlier_one_wrote` — a second instance over the same
root standing in for the restarted process, since a test cannot restart itself.

:class:`InMemoryObjectStore` is kept and tested alongside, because tests want isolation
per test rather than persistence and construct it directly.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from app.core.config import Environment, Settings
from app.core.errors import NotFoundError, ValidationError
from app.domain.common.enums import ScreenshotKind, Timeframe
from app.infrastructure.storage.objects import (
    MAX_OBJECT_BYTES,
    FilesystemObjectStore,
    InMemoryObjectStore,
    S3ObjectStore,
    build_object_store,
    checksum_of,
    screenshot_key,
)

#: A one-pixel PNG. Real bytes rather than b"x" so nothing here passes only because the
#: store never looks at what it is given.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d4944415478da63fcffff3f0300050001a5f645b400"
    "00000049454e44ae426082"
)

USER = UUID("019fbed5-88bb-7337-84eb-d60dad817fd5")
TRADE = UUID("019fbed5-9701-77b7-a36a-851a00db5236")


def a_key() -> str:
    return screenshot_key(
        user_id=USER,
        trade_id=TRADE,
        kind=ScreenshotKind.BEFORE_ENTRY,
        timeframe=Timeframe.M2,
        checksum=checksum_of(PNG),
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "objects"


def files_under(root: Path, pattern: str = "*") -> list[Path]:
    """Sync so the async lint rule about blocking the loop stays on, and stays honest."""
    return [path for path in root.rglob(pattern) if path.is_file()]


async def test_a_new_store_reads_what_an_earlier_one_wrote(root: Path) -> None:
    """**The test this file exists for.**

    A second instance over the same root is what a restarted API process is. Under the
    dict this returned ``NotFoundError`` while the screenshot row sat in the database
    describing an image that no longer existed.
    """
    key = a_key()
    await FilesystemObjectStore(root).put(key, PNG, content_type="image/png")

    # Nothing carried over from the instance that wrote it — a different object, the same
    # directory, exactly as the next process sees it.
    restarted = FilesystemObjectStore(root)

    assert await restarted.get(key) == PNG


async def test_two_stores_over_one_root_are_one_store(root: Path) -> None:
    """The worker and the API, which are separate processes and must not be separate
    stores. This is the half of the defect a restart does not explain: both were running,
    both were healthy, and the bytes one wrote were unreadable by the other."""
    worker = FilesystemObjectStore(root)
    api = FilesystemObjectStore(root)

    await worker.put(a_key(), PNG, content_type="image/png")

    assert await api.get(a_key()) == PNG


async def test_the_root_does_not_have_to_exist_yet(tmp_path: Path) -> None:
    """First capture on a fresh machine must not need a directory created by hand."""
    store = FilesystemObjectStore(tmp_path / "not" / "created" / "yet")

    stored = await store.put(a_key(), PNG, content_type="image/png")

    assert stored.byte_size == len(PNG)
    assert await store.get(a_key()) == PNG


async def test_a_missing_object_is_not_found_rather_than_an_oserror(root: Path) -> None:
    """The router turns ``NotFoundError`` into a 404. An ``OSError`` reaching it is a 500,
    which says the server is broken rather than that the image is absent."""
    store = FilesystemObjectStore(root)

    with pytest.raises(NotFoundError):
        await store.get(a_key())
    with pytest.raises(NotFoundError):
        await store.presigned_url(a_key())


async def test_deleting_is_idempotent(root: Path) -> None:
    """A capture re-run after a partial failure must not fail on the cleanup step."""
    store = FilesystemObjectStore(root)
    await store.put(a_key(), PNG, content_type="image/png")

    await store.delete(a_key())
    await store.delete(a_key())

    with pytest.raises(NotFoundError):
        await store.get(a_key())


async def test_a_key_cannot_escape_the_root(root: Path) -> None:
    """The store owns the meaning of its own names.

    Keys come from `screenshot_key` and are built from UUIDs and a checksum, so no caller
    constructs a traversal today. That is an argument for the check being cheap, not for
    leaving it out: the one thing standing between a key and the filesystem is this class.
    """
    store = FilesystemObjectStore(root)

    for key in ("../escaped", "screenshots/../../escaped", "/etc/passwd"):
        with pytest.raises(ValidationError):
            await store.put(key, PNG, content_type="image/png")
        with pytest.raises(ValidationError):
            await store.get(key)


async def test_a_rejected_image_leaves_nothing_behind(root: Path) -> None:
    """Validation happens before any file is created, so a refused upload does not leave a
    directory that makes the next reader think something is there."""
    store = FilesystemObjectStore(root)

    with pytest.raises(ValidationError):
        await store.put(a_key(), PNG, content_type="text/html")
    with pytest.raises(ValidationError):
        await store.put(a_key(), b"", content_type="image/png")
    with pytest.raises(ValidationError):
        await store.put(a_key(), b"x" * (MAX_OBJECT_BYTES + 1), content_type="image/png")

    with pytest.raises(NotFoundError):
        await store.get(a_key())


async def test_no_partial_file_is_left_where_the_object_belongs(root: Path) -> None:
    """Writes land by rename, so the only thing at the key is a complete image.

    A truncated PNG is worse than a missing one: it renders as a broken image in the
    gallery rather than as an error, which is the failure mode this whole file is about.
    """
    store = FilesystemObjectStore(root)
    await store.put(a_key(), PNG, content_type="image/png")

    leftovers = [path.name for path in files_under(root, "*.partial")]

    assert leftovers == [], f"temporary files were left behind: {leftovers}"


async def test_the_same_bytes_written_twice_stay_one_object(root: Path) -> None:
    """Keys are content-addressed, so a re-capture of an unchanged chart is a no-op rather
    than a second copy."""
    store = FilesystemObjectStore(root)

    await store.put(a_key(), PNG, content_type="image/png")
    await store.put(a_key(), PNG, content_type="image/png")

    assert len(files_under(root)) == 1


async def test_the_in_memory_store_still_round_trips() -> None:
    """Kept for tests, which want an empty store per test rather than a durable one."""
    store = InMemoryObjectStore()

    await store.put(a_key(), PNG, content_type="image/png")

    assert await store.get(a_key()) == PNG
    assert len(store) == 1


def test_local_gets_a_directory_and_deployed_gets_a_bucket(tmp_path: Path) -> None:
    """The choice `build_object_store` makes, pinned.

    Local resolving to something non-persistent is the defect, not a detail: it is why a
    developer's screenshots disappeared between restarts while every list endpoint kept
    reporting them.
    """
    local = build_object_store(
        Settings(environment=Environment.LOCAL, object_storage_path=tmp_path)
    )
    deployed = build_object_store(
        Settings(environment=Environment.PRODUCTION, object_storage_path=tmp_path)
    )

    assert isinstance(local, FilesystemObjectStore)
    assert isinstance(deployed, S3ObjectStore)
