"""Key handling for the encrypted secret store.

Separate from the integration tests because none of this needs a database — and because
the property they assert is the one with no safe default. Every other misconfiguration in
this codebase degrades loudly; a secret store that stops encrypting keeps working
perfectly and is discovered by whoever reads the leaked dump.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.core.config import Environment, Settings
from app.infrastructure.secrets.encrypted import EncryptionKeyError, build_cipher


def test_no_key_is_refused_rather_than_defaulted() -> None:
    """There is no unencrypted fallback, by construction."""
    with pytest.raises(EncryptionKeyError, match="no encryption key"):
        build_cipher([])


def test_a_malformed_key_is_refused() -> None:
    """Rejected here rather than at the first write.

    A key that fails when used first fails during `POST /broker/connections` — which is
    the moment a user is handing over a broker password, and the worst available moment
    to discover a deployment is misconfigured.
    """
    with pytest.raises(EncryptionKeyError, match="not a valid Fernet key"):
        build_cipher(["not-a-real-key"])


def test_blank_entries_are_ignored() -> None:
    """A trailing comma in an environment variable is not a configuration error.

    `LEDGERLINE_SECRET_ENCRYPTION_KEYS=key1,key2,` is what a human writes, and failing on
    it would be pedantry that costs a deploy.
    """
    key = Fernet.generate_key().decode()

    cipher = build_cipher([key, "", "  "])

    assert cipher.decrypt(cipher.encrypt(b"payload")) == b"payload"


def test_encryption_is_required_where_it_matters() -> None:
    """Local and test run without a key; staging and production may not.

    Asserted against the enum rather than a restated list, so a new deployed environment
    added later fails this test instead of silently defaulting to unencrypted.
    """
    requires = {
        environment: Settings(environment=environment).requires_secret_encryption
        for environment in Environment
    }

    assert requires[Environment.PRODUCTION] is True
    assert requires[Environment.STAGING] is True
    assert requires[Environment.LOCAL] is False
    assert requires[Environment.TEST] is False
    assert set(Environment) == {
        Environment.LOCAL,
        Environment.TEST,
        Environment.STAGING,
        Environment.PRODUCTION,
    }, "a new environment was added without deciding whether it encrypts secrets"


def test_comma_separated_keys_parse_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented form has to be the working form.

    pydantic-settings JSON-decodes any `list[str]` env var *inside the settings source*,
    before field validators run, so `KEY=abc,def` raised `SettingsError: error parsing
    value` — a confusing thing to be told while holding a Fernet key. `NoDecode`
    suppresses that decoding so the `_split_list` validator actually sees the string.

    `cors_origins` carried the same validator for years against a form it could never
    receive: every caller had to pass JSON. This asserts both.
    """
    first, second = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setenv("LEDGERLINE_SECRET_ENCRYPTION_KEYS", f"{first},{second}")
    monkeypatch.setenv("LEDGERLINE_CORS_ORIGINS", "http://a.example,http://b.example")

    settings = Settings()

    assert settings.secret_encryption_keys == [first, second]
    assert settings.cors_origins == ["http://a.example", "http://b.example"]


def test_json_lists_still_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """The previously-working form must keep working — deployments already use it."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("LEDGERLINE_SECRET_ENCRYPTION_KEYS", f'["{key}"]')

    assert Settings().secret_encryption_keys == [key]
