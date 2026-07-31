"""Object storage for chart screenshots.

Screenshots are the one part of a trade record that cannot be regenerated: bars can be
re-fetched and trades rebuilt, but the chart as it looked at the moment of entry — with
the trader's own indicators and drawings — exists once. So storage here is
write-once-and-keep, keyed by content so a re-upload of the same image is free.

The protocol is deliberately small. ``ObjectStore`` is satisfied by S3, MinIO, or the
in-memory implementation the tests use, and nothing above this module knows which.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
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
