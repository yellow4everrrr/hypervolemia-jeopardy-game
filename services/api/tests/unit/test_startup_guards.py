"""What the process refuses to start without, when it is deployed.

Three things are only true in a deployed environment — encrypted secrets, S3 screenshot
storage, enforced row-level security — and all three share a property that makes them
dangerous: **the local substitute works, so nothing about the failure is visible until it
is in production.**

That is not hypothetical. `aioboto3` was imported lazily by `S3ObjectStore` so the tests
and the domain would not carry an AWS SDK, and it was then neither declared as a
dependency nor installed by the Dockerfile. The resulting image booted, answered every
endpoint, passed its health check, and raised ``RuntimeError`` at the first screenshot
capture. Locally the store is a directory and that code never runs, so no amount of local
testing could have found it; CI could not either, because CI runs as ``test``.

Startup is the last place to catch this class honestly. A guard that fails the process is
strictly better than a 500 in front of a trader, and much better than a capture pipeline
that appears to work and silently stores nothing.

These tests are cheap and they are about *policy*, not plumbing: they pin which
environments demand what, which is the part a well-meaning change to `is_deployed` or to
the store factory would quietly alter.
"""

from __future__ import annotations

import builtins
import os
from collections.abc import Callable
from typing import Any

import pytest

from app.core.config import Environment, Settings
from app.infrastructure.secrets.encrypted import EncryptionKeyError
from app.interfaces.http.app import (
    _verify_object_storage,
    _verify_secret_encryption,
    _verify_tenant_isolation,
)

#: A genuinely valid Fernet key — 32 bytes, url-safe base64 — not a plausible-looking
#: string. `build_cipher` constructs the cipher rather than checking the value is
#: non-empty, so a test key that merely looks right fails for the wrong reason and proves
#: nothing about the case it claims to cover. (Written by hand first; it did not
#: validate, which is the point.) Fixed rather than generated, so a failure here is a
#: change in behaviour and never a flake.
KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run these tests against the settings they declare, and nothing else.

    ``Settings`` reads `LEDGERLINE_*` from the process environment and from `.env`. Most
    of the tests below assert what happens when a setting is *absent*, so a developer who
    happens to export `LEDGERLINE_SECRET_ENCRYPTION_KEYS` — entirely reasonable, it is how
    you run the app locally against a deployed-shaped config — would see them fail for a
    reason that has nothing to do with the code.

    CI has none of these set, so this changes nothing there. That is the argument for
    adding it rather than against: a test that passes in CI and fails on one machine sends
    whoever hits it looking for a defect that is not there.
    """
    for name in list(os.environ):
        if name.startswith("LEDGERLINE_"):
            monkeypatch.delenv(name, raising=False)


def settings_for(environment: Environment, **overrides: Any) -> Settings:
    # `_env_file=None` so a `.env` sitting in the working directory cannot supply a value
    # a test is asserting the absence of.
    return Settings(_env_file=None, environment=environment, **overrides)


def without_module(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make one import fail, leaving every other import alone.

    Deleting from ``sys.modules`` is not enough — the package may genuinely be installed
    in the environment running these tests, and it is in CI once the `s3` extra is
    present. The guard's behaviour when the package is *absent* is the thing under test,
    so absence has to be simulated rather than assumed.
    """
    real_import: Callable[..., Any] = builtins.__import__

    def fake_import(module: str, *args: Any, **kwargs: Any) -> Any:
        if module == name or module.startswith(f"{name}."):
            raise ImportError(f"No module named {name!r}")
        return real_import(module, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_a_deployed_process_will_not_start_without_screenshot_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The test this file exists for.**

    Without it, the failure surfaces as a `RuntimeError` on the first capture, in the one
    environment where nobody is watching a terminal.
    """
    without_module(monkeypatch, "aioboto3")

    with pytest.raises(RuntimeError, match="aioboto3"):
        _verify_object_storage(settings_for(Environment.PRODUCTION))
    with pytest.raises(RuntimeError, match="aioboto3"):
        _verify_object_storage(settings_for(Environment.STAGING))


def test_the_message_says_how_to_fix_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A boot failure that does not name its own remedy costs whoever reads it the same
    investigation twice."""
    without_module(monkeypatch, "aioboto3")

    with pytest.raises(RuntimeError) as caught:
        _verify_object_storage(settings_for(Environment.PRODUCTION))

    assert '.[s3]' in str(caught.value), "the error should name the extra that fixes it"


def test_local_and_test_do_not_need_an_aws_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason the import is lazy in the first place.

    Local development stores screenshots in a directory, and requiring an AWS SDK to run
    the test suite would be a dependency nobody can justify. The guard has to be silent
    here or it becomes the thing people disable.
    """
    without_module(monkeypatch, "aioboto3")

    _verify_object_storage(settings_for(Environment.LOCAL))
    _verify_object_storage(settings_for(Environment.TEST))


def test_a_deployed_process_will_not_start_without_an_encryption_key() -> None:
    """Broker credentials are Fernet-encrypted at rest. A store that silently falls back
    to plaintext passes every test and is discovered by whoever reads the dump."""
    with pytest.raises(EncryptionKeyError):
        _verify_secret_encryption(settings_for(Environment.PRODUCTION))
    with pytest.raises(EncryptionKeyError):
        _verify_secret_encryption(settings_for(Environment.STAGING))


def test_a_malformed_key_fails_at_boot_rather_than_at_first_use() -> None:
    """`build_cipher` constructs the cipher instead of checking the string is non-empty.

    The difference matters: the first ``put`` happens while a user is handing over a
    broker password, and a key that is present but invalid would fail exactly there.
    """
    with pytest.raises(EncryptionKeyError):
        _verify_secret_encryption(
            settings_for(Environment.PRODUCTION, secret_encryption_keys="not-a-key")
        )


def test_a_deployed_process_starts_with_a_valid_key() -> None:
    """The guard must not be so strict that a correct configuration cannot boot."""
    _verify_secret_encryption(
        settings_for(Environment.PRODUCTION, secret_encryption_keys=KEY)
    )


def test_local_runs_without_a_key() -> None:
    """Linking a demo broker account in development needs no provisioning."""
    _verify_secret_encryption(settings_for(Environment.LOCAL))
    _verify_secret_encryption(settings_for(Environment.TEST))


async def test_production_will_not_start_when_isolation_cannot_be_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**"Could not check" is not "checked and fine."**

    Postgres exempts superusers from row-level security unconditionally, so an application
    connected as the owning role reads every tenant's rows with no error anywhere. That is
    what `_verify_tenant_isolation` exists to catch, and it is fatal in production when the
    check runs and comes back false.

    The check's *own* failure used to be a warning in every environment. So a production
    process that could not reach the database at boot logged one line and started — with
    tenant isolation unverified rather than confirmed. Measured before the fix: an
    unreachable database, `LEDGERLINE_ENVIRONMENT=production`, "Application startup
    complete", and `/health` answering 200.

    A guard whose failure is non-fatal reports success when it did nothing, which is the
    exact shape of the defect it was written to prevent.
    """
    monkeypatch.setattr(
        "app.infrastructure.db.session.get_sessionmaker",
        lambda: (_ for _ in ()).throw(OSError("[Errno 111] Connection refused")),
    )

    with pytest.raises(RuntimeError, match="could not verify"):
        await _verify_tenant_isolation(settings_for(Environment.PRODUCTION))


async def test_other_environments_still_start_when_the_check_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A developer with no database running should still get a process to look at.

    Staging included: it is deployed, but it is not where a real trader's rows live, and
    making it fatal there would mostly teach people to disable the check.
    """
    monkeypatch.setattr(
        "app.infrastructure.db.session.get_sessionmaker",
        lambda: (_ for _ in ()).throw(OSError("[Errno 111] Connection refused")),
    )

    await _verify_tenant_isolation(settings_for(Environment.LOCAL))
    await _verify_tenant_isolation(settings_for(Environment.STAGING))
