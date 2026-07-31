"""Is the model's skill real, or is it the best of the noise?

A positive skill score is not evidence of skill. This is the same mistake the what-if
simulator made before ADR 0008 and the same one the pattern scanner made before ADR 0006:
comparing a point estimate against a threshold, with nothing said about how much that
estimate would move on a different sample.

It was found here the same way — by probing rather than by reasoning. Ten walk-forward
runs on histories whose outcomes were **independent of every feature** produced skill
scores scattered around zero, and one of them landed at +0.014. Under a bare ``skill > 0``
gate that model was deployable: a trader would have been shown per-trade win
probabilities derived from pure noise, and the number carried no marking to say so. One
in ten is not a rare accident, it is a design defect.

**Sessions are the resampling unit, not trades.** :mod:`app.ml.validation` refuses to
split a fold inside a session on the grounds that trades sharing a day share a market, a
news cycle and a trader's mood, and are therefore not independent draws. That argument
does not stop being true here. Resampling individual trades would treat four correlated
trades as four independent pieces of evidence and produce an interval roughly half the
width it should be — which would have hidden exactly the false positive this module
exists to catch.

**What the interval does not capture.** The predictions are held fixed while the sample
is resampled, so this measures the uncertainty in *evaluating* the models the folds
produced, not the additional uncertainty in *which* models the folds would have produced
on different data. The interval is therefore somewhat optimistic. Fixing that properly
means re-running the entire walk-forward inside every bootstrap draw — hundreds of fits
per draw — which is not affordable here. The limitation is stated in the payload rather
than left for a reader to work out.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.analytics.montecarlo import DEFAULT_SEED
from app.analytics.statistics import divide, mean
from app.analytics.types import Interval
from app.ml.validation import Prediction

#: Bootstrap draws. Matches the analytics engine's default so intervals across the
#: product are built from the same amount of resampling.
DEFAULT_DRAWS = 2_000

#: Sessions needed before an interval is attempted. A block bootstrap over four sessions
#: resamples four things and produces an interval shaped by which of four days got drawn.
MIN_SESSIONS_FOR_INTERVAL = 10


@dataclass(frozen=True, slots=True)
class SkillEvidence:
    """A skill score with the interval that decides whether it means anything.

    Attributes:
        established: Whether the interval lies entirely above zero. This — not the point
            estimate — is what the deployability gate reads.
    """

    value: Decimal | None
    interval: Interval | None
    sessions: int
    predictions: int

    @property
    def established(self) -> bool:
        return (
            self.value is not None
            and self.value > 0
            and self.interval is not None
            and self.interval.low > 0
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "value": str(self.value) if self.value is not None else None,
            "interval": (
                [str(self.interval.low), str(self.interval.high)]
                if self.interval is not None
                else None
            ),
            "sessions": self.sessions,
            "predictions": self.predictions,
            "established": self.established,
            "caveat": (
                "The interval resamples whole trading sessions, because trades within a "
                "session are not independent. It holds the fitted models fixed, so it "
                "measures uncertainty in evaluating them rather than in which models "
                "different data would have produced — it is somewhat optimistic."
            ),
        }


def _brier_skill(pairs: Sequence[tuple[Decimal, Decimal]]) -> Decimal | None:
    """``1 - MSE(model) / MSE(baseline)`` where the baseline is the sample mean.

    One function serves both heads. For the classifier the actuals are 1/0 and the mean
    is the base rate, which makes this the Brier skill score exactly; for the regressor
    the actuals are R multiples and this is out-of-sample R². The comparison being made
    is identical in both cases — beat the single number that needed no model.
    """
    if len(pairs) < 2:
        return None
    actuals = [actual for _, actual in pairs]
    baseline_value = mean(actuals)
    if baseline_value is None:
        return None

    model_error = sum(((p - a) ** 2 for p, a in pairs), Decimal(0))
    baseline_error = sum(((baseline_value - a) ** 2 for a in actuals), Decimal(0))
    ratio = divide(model_error, baseline_error)
    if ratio is None:
        return None
    return Decimal(1) - ratio


def assess_skill(
    predictions: Sequence[Prediction],
    *,
    draws: int = DEFAULT_DRAWS,
    seed: int = DEFAULT_SEED,
    confidence: Decimal = Decimal("0.95"),
) -> SkillEvidence:
    """Skill score with a session-block bootstrap interval.

    Draws whole sessions with replacement, recomputes the skill score on the resampled
    history, and takes the percentile interval. A model whose apparent skill comes from
    one exceptional week loses it as soon as that week is sometimes not drawn; one whose
    skill is spread across the history keeps it.
    """
    pairs = [(item.predicted, item.actual) for item in predictions]
    observed = _brier_skill(pairs)

    grouped: dict[date | None, list[tuple[Decimal, Decimal]]] = {}
    for item in predictions:
        grouped.setdefault(item.session, []).append((item.predicted, item.actual))
    blocks = list(grouped.values())

    if observed is None or len(blocks) < MIN_SESSIONS_FOR_INTERVAL:
        return SkillEvidence(observed, None, len(blocks), len(predictions))

    rng = random.Random(seed)
    resampled: list[Decimal] = []
    for _ in range(draws):
        drawn: list[tuple[Decimal, Decimal]] = []
        for _ in range(len(blocks)):
            drawn.extend(blocks[rng.randrange(len(blocks))])
        value = _brier_skill(drawn)
        if value is not None:
            resampled.append(value)

    if len(resampled) < draws // 2:
        # Too many draws were degenerate — typically every drawn session had the same
        # outcome, leaving the baseline error at zero. No interval is better than one
        # built from the half of the draws that happened to be well behaved.
        return SkillEvidence(observed, None, len(blocks), len(predictions))

    resampled.sort()
    tail = (Decimal(1) - confidence) / Decimal(2)
    low = resampled[_index_at(tail, len(resampled))]
    high = resampled[_index_at(Decimal(1) - tail, len(resampled))]

    return SkillEvidence(
        value=observed,
        interval=Interval(
            low=low,
            high=high,
            confidence=confidence,
            method="session_block_bootstrap",
            iterations=len(resampled),
        ),
        sessions=len(blocks),
        predictions=len(predictions),
    )


def _index_at(fraction: Decimal, size: int) -> int:
    index = int(fraction * Decimal(size))
    if index >= size:
        return size - 1
    if index < 0:
        return 0
    return index
