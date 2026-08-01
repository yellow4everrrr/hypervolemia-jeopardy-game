"""Rendering a candle chart to a PNG, from bars the replay engine already produces.

The screenshots this feeds are the one part of a trade record that is *not* regenerable
from first principles — which is exactly why generating them is the right call here. The
alternative reading of "automatic screenshots" is a headless browser photographing the
frontend, and that buys a chart with the trader's own indicators at the cost of a Chromium
in the API image, a render farm's worth of latency, and a picture whose contents nobody
can test. This draws the same bars the replay endpoint serves, deterministically, in
milliseconds, with assertions possible on individual pixels.

**What the image is allowed to contain.** Bars that exist, markers at prices that were
recorded, and labels derived from both. Nothing is interpolated between bars — the same
rule the replay engine follows (ADR 0013) — so a gap in the data is a gap in the picture
rather than a straight line drawn through it.

Everything is a pure function of its arguments. No I/O, no clock, no randomness: the same
trade renders byte-identical every time, which is what makes the content-addressed storage
key in `app.infrastructure.storage.objects` meaningful rather than decorative.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.domain.marketdata.bars import Bar
from app.domain.marketdata.replay import Marker, MarkerKind
from app.rendering.png import (
    GLYPH_HEIGHT,
    Canvas,
    Colour,
    draw_text,
    text_width,
)

WIDTH = 960
HEIGHT = 540

#: Room on the right for price labels, and at the bottom for times.
PADDING_LEFT = 8
PADDING_RIGHT = 64
PADDING_TOP = 10
PADDING_BOTTOM = 18

BACKGROUND: Colour = (11, 16, 23)
GRID: Colour = (26, 34, 46)
AXIS_TEXT: Colour = (110, 125, 145)
UP: Colour = (52, 199, 138)
DOWN: Colour = (232, 84, 106)
WICK_UP: Colour = (38, 150, 105)
WICK_DOWN: Colour = (180, 62, 80)

MARKER_COLOURS: dict[MarkerKind, Colour] = {
    MarkerKind.ENTRY: (56, 160, 235),
    MarkerKind.EXIT: (233, 176, 60),
    MarkerKind.SCALE_IN: (56, 160, 235),
    MarkerKind.SCALE_OUT: (233, 176, 60),
    MarkerKind.STOP: (232, 84, 106),
    MarkerKind.TARGET: (52, 199, 138),
    MarkerKind.MAE: (150, 90, 100),
    MarkerKind.MFE: (90, 150, 120),
}

#: Below this the candle body is drawn as a single line. A one-pixel-wide "body" with a
#: border is just a smudge, and a doji should look like a doji.
MIN_BODY_HEIGHT = 1

HORIZONTAL_GRID_LINES = 4


@dataclass(frozen=True, slots=True)
class ChartRender:
    png: bytes
    width: int
    height: int
    bars: int


def _scale(value: Decimal, low: Decimal, high: Decimal, pixels: int) -> int:
    """Map a price onto a y coordinate, top-down, with exact arithmetic.

    Kept in `Decimal` until the final `int()`: a price scaled through a float can land a
    row off, and on a candle chart one row is the difference between a wick touching a
    level and stopping short of it.
    """
    span = high - low
    if span <= 0:
        return pixels // 2
    fraction = (high - value) / span
    return int(fraction * Decimal(pixels))


def render_chart(
    bars: list[Bar],
    *,
    markers: list[Marker] | None = None,
    highlight_at: datetime | None = None,
    width: int = WIDTH,
    height: int = HEIGHT,
) -> ChartRender:
    """Draw candles, level lines and execution markers.

    ``highlight_at`` draws a vertical rule at the moment the screenshot is *of* — the
    entry, the exit, whichever the caller is capturing. It is what makes six images of
    one trade distinguishable from six copies of the same chart.
    """
    canvas = Canvas.filled(width, height, BACKGROUND)
    markers = markers or []

    plot_left = PADDING_LEFT
    plot_right = width - PADDING_RIGHT
    plot_top = PADDING_TOP
    plot_bottom = height - PADDING_BOTTOM
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top

    if not bars or plot_width <= 0 or plot_height <= 0:
        # An empty window renders as an empty chart rather than raising. There is nothing
        # dishonest about a picture of no data, and a trade whose bars were never
        # backfilled should produce a blank frame rather than break the capture of the
        # other five.
        draw_text(canvas, plot_left, plot_top, "-", AXIS_TEXT)
        return ChartRender(canvas.to_png(), width, height, 0)

    # The price window spans the bars *and* every level drawn on them: a stop outside the
    # visible range would otherwise be clipped to an edge and read as a level that was
    # nearly touched.
    lows = [bar.low for bar in bars] + [marker.price for marker in markers]
    highs = [bar.high for bar in bars] + [marker.price for marker in markers]
    low, high = min(lows), max(highs)
    if high == low:
        # A flat window still needs a non-zero span or every candle collapses onto one row.
        high = high + Decimal("0.5")
        low = low - Decimal("0.5")

    def y_for(price: Decimal) -> int:
        return plot_top + _scale(price, low, high, plot_height)

    for index in range(HORIZONTAL_GRID_LINES + 1):
        y = plot_top + index * plot_height // HORIZONTAL_GRID_LINES
        canvas.hline(y, plot_left, plot_right, GRID)
        price = high - (high - low) * Decimal(index) / Decimal(HORIZONTAL_GRID_LINES)
        draw_text(
            canvas,
            plot_right + 6,
            max(y - GLYPH_HEIGHT // 2, 0),
            f"{price.quantize(Decimal('0.01'))}",
            AXIS_TEXT,
        )

    slot = max(plot_width // len(bars), 1)
    body_width = max(slot - 2, 1)

    for index, bar in enumerate(bars):
        x = plot_left + index * slot
        centre = x + body_width // 2
        rising = bar.close >= bar.open

        canvas.vline(centre, y_for(bar.high), y_for(bar.low), WICK_UP if rising else WICK_DOWN)

        top = y_for(max(bar.open, bar.close))
        bottom = y_for(min(bar.open, bar.close))
        canvas.rect(
            x, top, body_width, max(bottom - top, MIN_BODY_HEIGHT), UP if rising else DOWN
        )

    for marker in markers:
        colour = MARKER_COLOURS.get(marker.kind, AXIS_TEXT)
        y = y_for(marker.price)

        # `spans_trade` rather than a set of kinds enumerated here: the domain already
        # decides which overlays are levels in force for the life of the trade, and a
        # second list in the renderer would eventually disagree with it.
        if marker.spans_trade or marker.at is None:
            canvas.dashed_hline(y, plot_left, plot_right, colour)
            label = f"{marker.price.quantize(Decimal('0.01'))}"
            draw_text(canvas, plot_right + 6, max(y - GLYPH_HEIGHT // 2, 0), label, colour)
            continue

        # Executions are drawn at the bar containing them, with a solid rule across the
        # window so the level stays readable where candles are dense.
        canvas.hline(y, plot_left, plot_right, colour)
        at_bar = _bar_index(bars, marker.at)
        if at_bar is not None:
            x = plot_left + at_bar * slot + body_width // 2
            canvas.rect(x - 2, y - 2, 5, 5, colour)

    if highlight_at is not None:
        moment = _bar_index(bars, highlight_at)
        if moment is not None:
            canvas.vline(
                plot_left + moment * slot + body_width // 2, plot_top, plot_bottom, (90, 104, 126)
            )

    first, last = bars[0].ts, bars[-1].ts
    draw_text(canvas, plot_left, plot_bottom + 4, first.strftime("%H:%M"), AXIS_TEXT)
    end_label = last.strftime("%H:%M")
    draw_text(canvas, plot_right - text_width(end_label), plot_bottom + 4, end_label, AXIS_TEXT)

    return ChartRender(canvas.to_png(), width, height, len(bars))


def _bar_index(bars: list[Bar], at: datetime) -> int | None:
    """The bar whose interval contains ``at``, or None when it falls outside the window.

    Returns the last bar that opened at or before the moment — bars are open-stamped, so
    the containing bar is the newest one not in the future. `None` rather than clamping to
    an edge: a marker outside the window did not happen there, and drawing it at the
    boundary would put an execution on a bar it has nothing to do with.
    """
    if not bars or at < bars[0].ts:
        return None
    found = None
    for index, bar in enumerate(bars):
        if bar.ts <= at:
            found = index
        else:
            break
    return found
