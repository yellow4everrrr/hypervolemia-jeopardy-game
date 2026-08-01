"""The skill interval — the gate that separates a lucky model from a good one."""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

from app.ml.skill import MIN_SESSIONS_FOR_INTERVAL, assess_skill
from app.ml.validation import Prediction

START = date(2025, 4, 1)


def _predictions(
    pairs_by_session: list[list[tuple[str, str, bool]]],
) -> list[Prediction]:
    items: list[Prediction] = []
    for index, session_pairs in enumerate(pairs_by_session):
        session = START + timedelta(days=index)
        for order, (predicted, actual, won) in enumerate(session_pairs):
            items.append(
                Prediction(
                    trade_id=f"{index}-{order}",
                    fold=1,
                    predicted=Decimal(predicted),
                    actual=Decimal(actual),
                    won=won,
                    session=session,
                )
            )
    return items


def _noise(sessions: int, seed: int, per_session: int = 4) -> list[Prediction]:
    """Predictions carrying no information: a constant guess against random outcomes."""
    rng = random.Random(seed)
    grouped: list[list[tuple[str, str, bool]]] = []
    for _ in range(sessions):
        session_pairs = []
        for _ in range(per_session):
            won = rng.random() < 0.45
            session_pairs.append(("0.45", "1" if won else "0", won))
        grouped.append(session_pairs)
    return _predictions(grouped)


class TestTheInterval:
    def test_a_useless_model_does_not_establish_skill(self) -> None:
        evidence = assess_skill(_noise(40, seed=1), draws=300)

        assert evidence.value is not None
        assert not evidence.established

    def test_a_genuinely_skilful_model_establishes_it(self) -> None:
        """Predictions that track outcomes closely, spread evenly across sessions."""
        rng = random.Random(2)
        grouped: list[list[tuple[str, str, bool]]] = []
        for _ in range(40):
            session_pairs = []
            for _ in range(4):
                won = rng.random() < 0.5
                session_pairs.append(("0.85" if won else "0.15", "1" if won else "0", won))
            grouped.append(session_pairs)

        evidence = assess_skill(_predictions(grouped), draws=300)

        assert evidence.value is not None and evidence.value > Decimal("0.5")
        assert evidence.established
        assert evidence.interval is not None and evidence.interval.low > 0

    def test_skill_concentrated_in_one_session_does_not_survive_resampling(self) -> None:
        """The failure mode the block bootstrap exists to catch.

        Thirty-nine sessions of useless predictions plus one session where the model was
        perfect. The point estimate is positive; the interval must not be, because most
        resamples either omit that session or draw it repeatedly, and the score swings
        wildly between them.
        """
        rng = random.Random(3)
        grouped: list[list[tuple[str, str, bool]]] = []
        for _ in range(39):
            session_pairs = []
            for _ in range(4):
                won = rng.random() < 0.5
                session_pairs.append(("0.5", "1" if won else "0", won))
            grouped.append(session_pairs)
        # One spectacular day.
        grouped.append([("0.99", "1", True)] * 20)

        evidence = assess_skill(_predictions(grouped), draws=400)

        assert evidence.value is not None and evidence.value > 0
        assert not evidence.established

    def test_it_resamples_sessions_not_trades(self) -> None:
        """The consequence of block resampling, made observable.

        The same predictions arranged as many small sessions carry more independent
        evidence than as a few large ones, so the interval must be narrower in the first
        arrangement. If trades were resampled individually the two would be identical.
        """
        rng = random.Random(4)
        flat = [
            ("0.8" if won else "0.2", "1" if won else "0", won)
            for won in (rng.random() < 0.5 for _ in range(240))
        ]

        # Both arrangements hold the same 240 predictions and clear the session floor,
        # so the only thing that differs is how many independent blocks they form.
        many_sessions = _predictions([flat[i : i + 2] for i in range(0, 240, 2)])
        few_sessions = _predictions([flat[i : i + 20] for i in range(0, 240, 20)])

        wide = assess_skill(few_sessions, draws=400)
        narrow = assess_skill(many_sessions, draws=400)

        assert wide.interval is not None and narrow.interval is not None
        assert narrow.interval.width < wide.interval.width

    def test_too_few_sessions_yields_no_interval_and_no_claim(self) -> None:
        evidence = assess_skill(_noise(MIN_SESSIONS_FOR_INTERVAL - 1, seed=5), draws=200)

        assert evidence.interval is None
        assert not evidence.established

    def test_a_positive_estimate_without_an_interval_is_never_established(self) -> None:
        """Missing evidence must not read as favourable evidence."""
        perfect = _predictions([[("1", "1", True), ("0", "0", False)] for _ in range(4)])
        evidence = assess_skill(perfect, draws=200)

        assert evidence.interval is None
        assert not evidence.established

    def test_it_is_deterministic_for_a_fixed_seed(self) -> None:
        predictions = _noise(30, seed=6)
        first = assess_skill(predictions, draws=200, seed=42)
        second = assess_skill(predictions, draws=200, seed=42)

        assert first.interval is not None and second.interval is not None
        assert first.interval.low == second.interval.low
        assert first.interval.high == second.interval.high

    def test_the_payload_states_the_caveat_it_cannot_fix(self) -> None:
        """The interval holds the fitted models fixed, so it is somewhat optimistic.

        That limitation rides in the payload rather than being left for a reader to
        deduce.
        """
        payload = assess_skill(_noise(20, seed=7), draws=200).to_payload()

        assert "optimistic" in payload["caveat"]
        assert payload["established"] is False
