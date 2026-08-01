"""The demo history has to be possible, not just arithmetically legal.

Every screen in this application is read against the seeded history, and two engines read
the excursion columns directly. If those columns are shaped wrongly the engines are not
wrong — they faithfully report a property of a history that could not have happened, and
the result looks exactly like a finding.

`_excursions` has now been wrong in both directions:

* **Too generous.** ``mfe_r = abs(r) + uniform(0.05, 0.9)`` gave losers a favourable
  excursion up to 1.8R, and the what-if simulator reported a 1R target would have earned
  an extra $248,000, because under that rule most losers became winners.
* **Too mean.** The correction drew losers from ``uniform(0, 0.45)`` while winners got
  ``r + uniform(0.05, 0.60)``. Since the smallest winner was around 0.6R the ranges came
  out disjoint — across 1,447 seeded trades, winners' MFE spanned 0.68–2.99 and losers'
  0.00–0.45, with an empty gap between and **not one loser in 714 reaching 1R**. MFE alone
  classified win from loss perfectly, so the pattern engine's clustering split the sample
  into a 100%-winner group and a 6%-winner group and published them as established
  findings worth -$762,759 and +$687,586 a year. Those clusters were the win/loss label
  arriving through ``mae_r``/``mfe_r`` — the ``capture_efficiency`` leak of ADR 0015, in
  the data instead of in a feature.

The identities are asserted in the function itself. What is tested here is the thing an
assertion cannot express: that the two distributions **overlap**, so no single excursion
column can stand in for the outcome.
"""

from __future__ import annotations

import random
from decimal import Decimal
from itertools import pairwise

import pytest

from app.scripts.seed_demo import LOSER_MFE_BANDS, SEED, _excursions

TRIALS = 20_000


@pytest.fixture(scope="module")
def excursions() -> tuple[list[Decimal], list[Decimal]]:
    """MFE for many winners and many losers, drawn the way the seed draws them."""
    rng = random.Random(SEED)
    winners, losers = [], []
    for _ in range(TRIALS):
        winners.append(_excursions(rng, Decimal(str(round(rng.uniform(0.4, 2.5), 2))))[1])
        losers.append(_excursions(rng, Decimal(str(round(-rng.uniform(0.5, 1.2), 2))))[1])
    return winners, losers


def test_the_identities_hold(excursions: tuple[list[Decimal], list[Decimal]]) -> None:
    """Facts about what the words mean, not modelling choices."""
    rng = random.Random(SEED)
    for _ in range(2_000):
        r = Decimal(str(round(rng.uniform(-1.5, 2.5), 2)))
        mae, mfe = _excursions(rng, r)
        assert mfe >= max(r, Decimal(0)), "exited better than the best price offered"
        assert mae <= min(r, Decimal(0)), "exited worse than the worst price reached"


def test_losing_trades_sometimes_run_a_long_way_in_your_favour(
    excursions: tuple[list[Decimal], list[Decimal]],
) -> None:
    """**The test this file exists for.**

    A history in which no losing trade ever reached 1R in your favour is not a history,
    and it silently turns MFE into the win/loss label.
    """
    _, losers = excursions

    reached_1r = sum(1 for mfe in losers if mfe >= 1) / len(losers)

    assert reached_1r > 0.005, "no losing trade ever ran a full unit of risk in your favour"
    assert reached_1r < 0.10, "losers reaching 1R should be uncommon, not routine"


def test_the_two_distributions_overlap(
    excursions: tuple[list[Decimal], list[Decimal]],
) -> None:
    """Disjoint ranges make MFE a perfect classifier, which is what broke the clustering.

    Asserted as an overlap fraction rather than "the ranges touch", because ranges that
    merely abut still leave the bulk of each distribution cleanly separable.
    """
    winners, losers = excursions
    floor = min(winners)

    overlapping = sum(1 for mfe in losers if mfe >= floor) / len(losers)

    assert overlapping > 0.05, (
        f"only {overlapping:.1%} of losers reach the winners' MFE range (floor {floor}) — "
        "MFE alone separates win from loss, so any cluster built on it is the outcome"
    )


def test_most_losers_still_barely_moved(
    excursions: tuple[list[Decimal], list[Decimal]],
) -> None:
    """The opposite failure. Overlap must not become "losers usually ran a long way"."""
    _, losers = excursions

    ordered = sorted(losers)
    median = ordered[len(ordered) // 2]

    assert median < Decimal("0.5"), (
        f"median losing trade ran {median}R in your favour — that describes a trader who "
        "was up and chose to lose, which is what the original defect did"
    )


def test_the_bands_are_a_probability_distribution() -> None:
    shares = [share for share, _, _ in LOSER_MFE_BANDS]

    assert sum(shares) == pytest.approx(1.0), "bands must account for every draw"
    assert all(share > 0 for share in shares)
    # Ascending and contiguous, so the mixture has no gap of its own.
    for (_, _, high), (_, low, _) in pairwise(LOSER_MFE_BANDS):
        assert low == high, "the bands leave a hole in the distribution"


def test_a_winner_never_touched_a_full_unit_of_risk_against_it(
    excursions: tuple[list[Decimal], list[Decimal]],
) -> None:
    """A trade that reached -1R would have been stopped out and would not be a winner."""
    rng = random.Random(SEED)
    for _ in range(2_000):
        mae, _ = _excursions(rng, Decimal(str(round(rng.uniform(0.4, 2.5), 2))))
        assert mae > Decimal(-1), f"a winner sat at {mae}R, past where its stop was"
