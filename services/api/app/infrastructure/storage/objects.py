"""Object storage for chart screenshots.

Screenshots are the one part of a trade record that cannot be regenerated: bars can be
re-fetched and trades rebuilt, but the chart as it looked at the moment of entry — with
the trader's own indicators and drawings — exists once. So storage here is
write-once-and-keep, keyed by content so a re-upload of the same image is free.

The protocol is deliberately small. ``ObjectStore`` is satisfied by S3, MinIO, or the
in-memory implementation the tests use, and nothing above this module knows which.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkstemp
from typing import Any, Protocol
from uuid import UUID

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.domain.common.enums import ScreenshotKind, Timeframe

logger = get_logger(__name__)

#: Images only, and only formats a browser renders without a plugin.
ALLOWED_CONTENT_TYPES = {"image/webp", "image/png", "image/jpeg", "image/avif"}

#: 8 MB. A chart screenshot that exceeds this is a screen recording or a mistake.
MAX_OBJECT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    content_type: str
    byte_size: int
    checksum: str
    stored_at: datetime


class ObjectStore(Protocol):
    async def put(
        self, key: str, data: bytes, *, content_type: str
    ) -> StoredObject: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...

    async def presigned_url(self, key: str, *, expires_seconds: int = 3600) -> str: ...


def screenshot_key(
    *,
    user_id: UUID,
    trade_id: UUID,
    kind: ScreenshotKind,
    timeframe: Timeframe | None,
    checksum: str,
) -> str:
    """Content-addressed key under a tenant prefix.

    The user prefix makes per-tenant lifecycle rules and access policies expressible at
    the bucket level. The checksum suffix makes re-uploading an identical capture a
    no-op rather than a duplicate object.
    """
    parts = [
        "screenshots",
        str(user_id),
        str(trade_id),
        kind.value,
        timeframe.value if timeframe else "na",
        checksum[:16],
    ]
    return "/".join(parts)


def checksum_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_image(data: bytes, content_type: str) -> None:
    """Reject anything that is not a plausible image before it reaches storage."""
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ValidationError(
            f"unsupported content type {content_type!r}",
            details={"allowed": sorted(ALLOWED_CONTENT_TYPES)},
        )
    if not data:
        raise ValidationError("refusing to store an empty object")
    if len(data) > MAX_OBJECT_BYTES:
        raise ValidationError(
            f"object is {len(data)} bytes, over the {MAX_OBJECT_BYTES} limit"
        )


class InMemoryObjectStore:
    """Non-persistent store for tests and local development."""

    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredObject:
        validate_image(data, content_type)
        self._objects[key] = (data, content_type)
        return StoredObject(
            key=key,
            content_type=content_type,
            byte_size=len(data),
            checksum=checksum_of(data),
            stored_at=datetime.now(UTC),
        )

    async def get(self, key: str) -> bytes:
        if key not in self._objects:
            raise NotFoundError(f"no object at {key!r}")
        return self._objects[key][0]

    async def delete(self, key: str) -> None:
        self._objects.pop(key, None)

    async def presigned_url(self, key: str, *, expires_seconds: int = 3600) -> str:
        if key not in self._objects:
            raise NotFoundError(f"no object at {key!r}")
        return f"memory://{key}"

    def __len__(self) -> int:
        return len(self._objects)


class FilesystemObjectStore:
    """Local development store, on disk.

    Replaces :class:`InMemoryObjectStore` outside tests, and the reason is not tidiness.
    A dict in the process has two failure modes that both look exactly like a bug in the
    capture code, and both were hit repeatedly while working on the replay screen:

    * **It does not survive a restart.** The rows recording each capture live in
      Postgres, so after the API is restarted the gallery still lists six frames and every
      one of them 404s. The database says the screenshots exist; the store has forgotten
      them. Nothing reports an inconsistency, because from either side alone there is
      none.
    * **It is not shared between processes.** The worker and the API each get their own
      dict, so a capture performed by the background job writes bytes the API cannot read.
      Pressing "Re-capture" appears to do nothing, forever.

    A directory fixes both without pretending to be S3. Screenshots stay content-addressed
    and immutable, so the only operations are write-once and read.

    Writes go to a temporary file and are renamed into place. ``rename`` within a
    filesystem is atomic, so a reader never observes a partially written image — a
    truncated PNG is worse than a missing one, since it renders as a broken image rather
    than as an error anyone can act on.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        """Resolve a key under the root, refusing anything that escapes it.

        Keys are built by :func:`screenshot_key` from UUIDs and a checksum and cannot
        contain a traversal today. This checks anyway: the store's job is to hold a byte
        range at a name, and a name that reaches outside the root is the store's problem
        to refuse rather than its callers' to never construct.
        """
        candidate = (self._root / key).resolve()
        root = self._root.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValidationError(f"key {key!r} resolves outside the object store")
        return candidate

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredObject:
        validate_image(data, content_type)
        path = self._path(key)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Same directory as the target, so the rename cannot cross a filesystem
            # boundary and stop being atomic.
            handle, temporary = mkstemp(dir=path.parent, suffix=".partial")
            try:
                with os.fdopen(handle, "wb") as file:
                    file.write(data)
                os.replace(temporary, path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise

        await asyncio.to_thread(write)
        logger.info("storage.object_written", key=key, bytes=len(data))
        return StoredObject(
            key=key,
            content_type=content_type,
            byte_size=len(data),
            checksum=checksum_of(data),
            stored_at=datetime.now(UTC),
        )

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except (FileNotFoundError, IsADirectoryError) as exc:
            raise NotFoundError(f"no object at {key!r}") from exc

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await asyncio.to_thread(path.unlink, True)

    async def presigned_url(self, key: str, *, expires_seconds: int = 3600) -> str:
        """A ``file://`` URL, and deliberately not a usable substitute for a signed one.

        Nothing in the application calls this — images are streamed through the API, which
        is what lets them be authorised per request. It exists to satisfy the protocol,
        and it still refuses a key that is not there so that a caller cannot mistake a
        missing object for a working link.
        """
        path = self._path(key)
        if not await asyncio.to_thread(path.is_file):
            raise NotFoundError(f"no object at {key!r}")
        return path.as_uri()


class S3ObjectStore:  # pragma: no cover — requires a live endpoint
    """S3-compatible storage.

    Typing is relaxed for this class only: ``aioboto3`` ships no stubs, and writing
    them for an SDK behind a swappable protocol would be maintenance without benefit.
    The protocol boundary above is fully typed, which is where it matters.

    ``aioboto3`` is imported lazily so that the domain, the tests and any deployment
    that does not store screenshots carry no dependency on it.
    """

    def __init__(
        self,
        bucket: str,
        *,
        region: str = "us-east-1",
        endpoint_url: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._region = region
        self._endpoint_url = endpoint_url

    def _session(self) -> Any:
        try:
            import aioboto3
        except ImportError as exc:
            raise RuntimeError(
                "the 'aioboto3' package is required for S3-backed screenshot storage"
            ) from exc
        return aioboto3.Session()

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredObject:
        validate_image(data, content_type)
        async with self._session().client(
            "s3", region_name=self._region, endpoint_url=self._endpoint_url
        ) as client:
            await client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                # Screenshots are immutable and content-addressed, so they can be
                # cached indefinitely by any layer in front of the bucket.
                CacheControl="public, max-age=31536000, immutable",
            )
        logger.info("storage.object_written", key=key, bytes=len(data))
        return StoredObject(
            key=key,
            content_type=content_type,
            byte_size=len(data),
            checksum=checksum_of(data),
            stored_at=datetime.now(UTC),
        )

    async def get(self, key: str) -> bytes:
        async with self._session().client(
            "s3", region_name=self._region, endpoint_url=self._endpoint_url
        ) as client:
            try:
                response = await client.get_object(Bucket=self._bucket, Key=key)
            except client.exceptions.NoSuchKey as exc:
                raise NotFoundError(f"no object at {key!r}") from exc
            data: bytes = await response["Body"].read()
            return data

    async def delete(self, key: str) -> None:
        async with self._session().client(
            "s3", region_name=self._region, endpoint_url=self._endpoint_url
        ) as client:
            await client.delete_object(Bucket=self._bucket, Key=key)

    async def presigned_url(self, key: str, *, expires_seconds: int = 3600) -> str:
        """A time-limited URL, so the bucket itself stays private.

        Screenshots are a trader's positions and timing. The bucket is never public;
        the frontend receives a signed URL that expires.
        """
        async with self._session().client(
            "s3", region_name=self._region, endpoint_url=self._endpoint_url
        ) as client:
            url: str = await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_seconds,
            )
            return url


def build_object_store(settings: Any) -> ObjectStore:
    """Where rendered chart images live, chosen from configuration.

    Lives in infrastructure rather than beside the FastAPI dependency that used to own it,
    because the background worker needs the same choice and reaching into
    `app.interfaces.http.deps` from `app.jobs` would make a queue handler depend on the
    HTTP layer to decide where a file goes. Nothing enforces that boundary for `app.jobs`
    — the architecture test covers `domain` and `application` — so it has to be kept by
    construction.

    S3-compatible when deployed, a directory on disk otherwise, so a developer sees the
    capture pipeline work end to end without provisioning a bucket.

    **On disk rather than in memory**, which is what it used to be. A dict per process is
    the shortest thing that satisfies the protocol and it is wrong in two ways that are
    indistinguishable from a broken capture pipeline: the bytes vanish when the process
    restarts while the rows describing them stay in Postgres, and the worker cannot read
    what the API wrote or the reverse. Both produce a gallery of frames that 404
    individually while every list endpoint reports six healthy screenshots.
    :class:`FilesystemObjectStore` fixes both, and costs a directory.

    :class:`InMemoryObjectStore` is still the right store for tests, which want isolation
    per test rather than persistence, and construct it directly.
    """
    if settings.is_deployed:
        return S3ObjectStore(
            settings.s3_bucket,
            region=settings.s3_region,
            endpoint_url=settings.s3_endpoint_url,
        )
    return FilesystemObjectStore(Path(settings.object_storage_path))
