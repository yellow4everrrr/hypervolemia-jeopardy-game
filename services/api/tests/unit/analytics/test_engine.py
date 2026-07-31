"""Tests for the analytics engine orchestration and its evidence payload.

The payload is the AI layer's entire evidence base (ADR 0002), so these tests treat its
shape as a contract: every number must be a string, every undefined value must carry a
reason, and ``metric_keys()`` must enumerate exactly what a model is permitted to cite.
"""

from __future__ import annotations

import json
from decimal import Decimal

from app.analytics.engine import ENGINE_VERSION, AnalyticsConfig, analyse
from app.analytics.types import Reliability, TradeRecord
from tests.unit.analytics.conftest import make_trades

FAST = AnalyticsConfig(
    bootstrap_iterations=300,
    monte_carlo_iterations=300,
    include_segmentation=False,
)


# --- Orchestration --------------------------------------------------------------


def test_engine_computes_a_full_report(many_trades: list[TradeRecord]) -> None:
    report = analyse(many_trades, FAST)

    assert report.sample_size == 200
    assert report.engine_version == ENGINE_VERSION
    assert report.core.counts.total == 200
    assert report.expectancy.value is not None
    assert report.drawdown.max_drawdown > 0


def test_engine_is_deterministic(many_trades: list[TradeRecord]) -> None:
    """Same trades, same config, same numbers — the basis of reproducible evidence."""
    first = analyse(many_trades, FAST)
    second = analyse(many_trades, FAST)

    assert first.expectancy.value == second.expectancy.value
    assert first.expectancy.interval is not None and second.expectancy.interval is not None
    assert first.expectancy.interval.low == second.expectancy.interval.low
    assert first.to_payload()["risk"] == second.to_payload()["risk"]


def test_engine_handles_an_empty_sample() -> None:
    """A brand-new user opens the dashboard; nothing may raise."""
    report = analyse([], FAST)

    assert report.sample_size == 0
    assert report.expectancy.value is None
    assert report.reliability is Reliability.INSUFFICIENT
    assert report.to_payload()["sample"]["trades"] == 0


def test_engine_handles_a_single_trade() -> None:
    report = analyse(make_trades(["100"]), FAST)
    assert report.sample_size == 1
    assert report.core.pnl.net_pnl == Decimal(100)


def test_risk_of_ruin_requires_a_starting_equity(many_trades: list[TradeRecord]) -> None:
    without = analyse(many_trades, FAST)
    assert without.risk_of_ruin.value is None
    assert "starting equity" in (without.risk_of_ruin.undefined_reason or "")

    with_equity = analyse(
        many_trades,
        AnalyticsConfig(
            starting_equity=Decimal(50_000),
            bootstrap_iterations=300,
            monte_carlo_iterations=300,
            ruin_horizon_trades=200,
            include_segmentation=False,
        ),
    )
    assert with_equity.risk_of_ruin.value is not None


def test_segmentation_can_be_switched_off_for_speed(
    many_trades: list[TradeRecord],
) -> None:
    assert analyse(many_trades, FAST).segments == {}

    with_segments = analyse(
        many_trades,
        AnalyticsConfig(
            bootstrap_iterations=200,
            monte_carlo_iterations=200,
            test_significance=False,
            segmentation_dimensions=("weekday",),
        ),
    )
    assert "weekday" in with_segments.segments


# --- Payload contract -----------------------------------------------------------


def test_payload_is_json_serialisable(many_trades: list[TradeRecord]) -> None:
    """It is stored as JSONB and sent to a model; it must survive a round trip."""
    payload = analyse(many_trades, FAST).to_payload()
    round_tripped = json.loads(json.dumps(payload))

    assert round_tripped["sample"]["trades"] == 200


def test_payload_contains_no_floats(many_trades: list[TradeRecord]) -> None:
    """The whole engine avoids floats; the payload must not reintroduce them.

    Numbers are strings so that a model reading them, and Postgres storing them, both
    see exactly the value that was computed.
    """
    payload = analyse(many_trades, FAST).to_payload()

    def assert_no_floats(node: object, path: str = "") -> None:
        if isinstance(node, float):
            raise AssertionError(f"float found at {path}")
        if isinstance(node, dict):
            for key, value in node.items():
                assert_no_floats(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                assert_no_floats(value, f"{path}[{index}]")

    assert_no_floats(payload)


def test_undefined_statistics_carry_a_reason(simple_trades: list[TradeRecord]) -> None:
    """A bare null invites a model to paper over it; a sentence can be quoted."""
    payload = analyse(simple_trades, FAST).to_payload()

    assert payload["core"]["expectancy_r"]["value"] is None
    assert payload["core"]["expectancy_r"]["undefined_reason"]
    assert payload["risk_adjusted"]["sharpe"]["value"] is None
    assert payload["risk_adjusted"]["sharpe"]["undefined_reason"]


def test_every_estimate_reports_its_sample_size(many_trades: list[TradeRecord]) -> None:
    payload = analyse(many_trades, FAST).to_payload()

    assert payload["core"]["expectancy"]["sample_size"] == 200
    assert payload["sample"]["reliability"] == Reliability.RELIABLE.value


def test_metric_keys_enumerate_the_citable_evidence(
    many_trades: list[TradeRecord],
) -> None:
    """The allow-list the AI layer validates claims against (ADR 0002)."""
    keys = analyse(many_trades, FAST).metric_keys()

    assert "core.win_rate" in keys
    assert "core.expectancy" in keys
    assert "drawdown.max" in keys
    assert "risk.risk_of_ruin" in keys
    assert "position_sizing.kelly_half" in keys
    assert "edge_test.has_demonstrable_edge" in keys
    # A statistic nobody computed must not be citable.
    assert "core.sharpe_per_trade" not in keys
    assert "core.lunar_correlation" not in keys


def test_ratio_basis_travels_with_the_number(many_trades: list[TradeRecord]) -> None:
    """So nobody mistakes a P&L-based Sharpe for a comparable one."""
    payload = analyse(many_trades, FAST).to_payload()
    sharpe = payload["risk_adjusted"]["sharpe"]

    assert sharpe["basis"] in {"pnl", "return"}
    assert "is_comparable" in sharpe


def test_kelly_warnings_reach_the_payload() -> None:
    """The caveats must travel with the number, not be left in a docstring."""
    trades = make_trades(["500"] * 8 + ["-100"] * 2)
    payload = analyse(trades, FAST).to_payload()

    assert payload["position_sizing"]["kelly_warnings"]


def test_edge_test_appears_in_the_payload(many_trades: list[TradeRecord]) -> None:
    payload = analyse(many_trades, FAST).to_payload()
    assert payload["edge_test"]["has_demonstrable_edge"] is True


def test_a_marginal_sample_reports_no_demonstrable_edge() -> None:
    trades = make_trades(["300", "-280", "250", "-240", "20", "-10"])
    report = analyse(trades, FAST)

    assert not report.has_demonstrable_edge
    assert report.to_payload()["edge_test"]["has_demonstrable_edge"] is False
