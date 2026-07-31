"""Architecture fitness tests.

Clean architecture erodes one convenient import at a time. These tests fail the build
the moment it starts, which is cheaper than discovering later that the domain layer
cannot be tested without a database.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: Packages the domain layer may never import. The domain describes trading, not
#: storage, transport or configuration.
FORBIDDEN_IN_DOMAIN = {
    "sqlalchemy",
    "fastapi",
    "starlette",
    "alembic",
    "httpx",
    "redis",
    "pydantic",
    "app.infrastructure",
    "app.interfaces",
    "app.application",
}

#: The application layer orchestrates use cases. It may depend on the domain and on
#: its own ports, but never on a concrete adapter or on the HTTP layer.
FORBIDDEN_IN_APPLICATION = {
    "sqlalchemy",
    "fastapi",
    "starlette",
    "httpx",
    "redis",
    "app.infrastructure",
    "app.interfaces",
}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def _violations(package: str, forbidden: set[str]) -> list[str]:
    problems: list[str] = []
    for path in sorted((APP_ROOT / package).rglob("*.py")):
        for module in _imported_modules(path):
            for banned in forbidden:
                if module == banned or module.startswith(f"{banned}."):
                    problems.append(f"{path.relative_to(APP_ROOT.parent)} imports {module}")
    return problems


def test_domain_has_no_outward_dependencies() -> None:
    """The domain must remain a pure, dependency-free description of trading."""
    assert _violations("domain", FORBIDDEN_IN_DOMAIN) == []


def test_analytics_is_pure() -> None:
    """Statistics must be computable over a list built in a test or a simulation.

    The analytics package is reused by the what-if simulator (milestone 10), which
    feeds it counterfactual trades that were never in the database. An import of
    SQLAlchemy here would make that impossible and would also mean the AI layer's
    evidence could not be reproduced offline.
    """
    assert _violations("analytics", FORBIDDEN_IN_DOMAIN) == []


def test_the_evidence_contract_never_touches_a_model() -> None:
    """The fabrication guarantee has to be testable without an API key.

    ``app/ai/`` holds the evidence bundle, the placeholder contract and the validator —
    the machinery that makes an invented statistic impossible. If it could import the
    Anthropic SDK, the tests proving that guarantee would need a network, and a
    guarantee that is expensive to check stops being checked.
    """
    assert _violations("ai", FORBIDDEN_IN_DOMAIN | {"anthropic"}) == []


def test_application_depends_only_on_the_domain_and_its_ports() -> None:
    assert _violations("application", FORBIDDEN_IN_APPLICATION) == []


def test_core_stays_framework_free_apart_from_configuration() -> None:
    """``core`` may use pydantic for settings and structlog for logging — nothing else."""
    allowed = {"sqlalchemy", "fastapi", "starlette", "httpx", "redis"}
    assert _violations("core", allowed) == []


@pytest.mark.parametrize(
    "package", ["domain", "application", "core", "infrastructure", "interfaces", "ai"]
)
def test_every_package_is_importable(package: str) -> None:
    """A module that only imports under some conditions is a runtime failure waiting."""
    import importlib

    importlib.import_module(f"app.{package}")


def test_models_package_registers_every_table() -> None:
    """Any model file not re-exported would be missing from every migration."""
    import importlib
    import pkgutil

    from app.infrastructure.db import models
    from app.infrastructure.db.base import Base

    registered_before = set(Base.metadata.tables)
    for module in pkgutil.iter_modules(models.__path__):
        importlib.import_module(f"app.infrastructure.db.models.{module.name}")

    assert set(Base.metadata.tables) == registered_before
