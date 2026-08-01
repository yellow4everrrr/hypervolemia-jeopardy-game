"""Chart rendering, and the one thing a picture can leak.

Most of this file checks that a PNG is a PNG. One test checks something else: that the
*before entry* frame does not draw the exit.

That is the same failure the ML layer's entry-snapshot barrier exists to prevent, in
picture form. A trader reviewing "what did this look like before I took it?" against an
image with the exit already marked on it is not reviewing their decision — they are
reviewing a chart that knows the answer, and every judgement they form from it is
contaminated. It is also completely invisible: the image looks fine, the marker looks
correct, and nothing anywhere reports an error.
"""

from __future__ import annotations

import struct
import zlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.use_cases.capture_screenshots import frames_for
from app.domain.common.enums import ScreenshotKind, Timeframe
from app.domain.marketdata.bars import Bar
from app.domain.marketdata.replay import Marker, MarkerKind, ReplayWindow
from app.rendering.chart import BACKGROUND, render_chart
from app.rendering.png import Canvas, encode_png

START = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)


def bars(count: int = 40, *, step_minutes: int = 2) -> list[Bar]:
    """A deterministic ramp. Predictable enough to reason about pixels."""
    return [
        Bar(
            ts=START + timedelta(minutes=step_minutes * index),
            open=Decimal(5000 + index),
            high=Decimal(5000 + index) + Decimal("0.75"),
            low=Decimal(5000 + index) - Decimal("0.25"),
            close=Decimal(5000 + index) + Decimal("0.50"),
            volume=Decimal(100),
        )
        for index in range(count)
    ]


def decode_header(png: bytes) -> tuple[int, int]:
    """Width and height straight out of IHDR, without an imaging library."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    (length,) = struct.unpack(">I", png[8:12])
    assert png[12:16] == b"IHDR"
    width, height = struct.unpack(">II", png[16:24])
    assert length == 13
    return width, height


class TestEncoder:
    def test_a_buffer_encodes_to_a_valid_png(self) -> None:
        png = encode_png(4, 3, bytes([255, 0, 0]) * 12)

        assert decode_header(png) == (4, 3)
        # IEND is twelve bytes: a zero length, the tag, and the CRC of the tag.
        assert png[-12:] == struct.pack(">I", 0) + b"IEND" + struct.pack(
            ">I", zlib.crc32(b"IEND")
        )

    def test_the_pixels_survive_the_round_trip(self) -> None:
        """Decoded by hand, because the point is that the bytes are really in there."""
        red, blue = bytes([220, 20, 20]), bytes([20, 20, 220])
        png = encode_png(2, 1, red + blue)

        # Find IDAT and inflate it: filter byte, then the row.
        start = png.index(b"IDAT") + 4
        (length,) = struct.unpack(">I", png[start - 8 : start - 4])
        raw = zlib.decompress(png[start : start + length])

        assert raw == b"\x00" + red + blue

    def test_a_wrong_sized_buffer_is_refused(self) -> None:
        with pytest.raises(ValueError, match="expected 12 bytes"):
            encode_png(2, 2, b"\x00" * 5)

    def test_drawing_outside_the_canvas_clips_rather_than_raising(self) -> None:
        """A wick one pixel past the top is a rounding artefact, not a caller error."""
        canvas = Canvas.filled(10, 10, (0, 0, 0))

        canvas.rect(-5, -5, 3, 3, (255, 255, 255))
        canvas.vline(50, -10, 100, (255, 255, 255))
        canvas.set(999, 999, (255, 255, 255))

        assert bytes(canvas.pixels) == bytes(10 * 10 * 3)


class TestRenderer:
    def test_the_same_bars_render_byte_identically(self) -> None:
        """The storage key is a hash of the bytes; without this it would churn forever."""
        first = render_chart(bars())
        second = render_chart(bars())

        assert first.png == second.png

    def test_the_image_has_the_requested_dimensions(self) -> None:
        render = render_chart(bars(), width=640, height=360)

        assert decode_header(render.png) == (640, 360)
        assert (render.width, render.height) == (640, 360)

    def test_candles_are_actually_drawn(self) -> None:
        """Guards against a renderer that produces a valid, empty PNG.

        An all-background image is a legal PNG of the right size that passes every other
        assertion here — and is exactly what a broken scaling calculation produces.
        """
        render = render_chart(bars())

        start = render.png.index(b"IDAT") + 4
        (length,) = struct.unpack(">I", render.png[start - 8 : start - 4])
        raw = zlib.decompress(render.png[start : start + length])
        assert bytes(BACKGROUND) in raw
        distinct = {raw[i : i + 3] for i in range(1, len(raw) - 3, 3)}
        assert len(distinct) > 3, "the image is a single flat colour"

    def test_no_bars_renders_an_empty_chart_rather_than_raising(self) -> None:
        """A trade whose bars were never backfilled must not break the other frames."""
        render = render_chart([])

        assert render.bars == 0
        assert decode_header(render.png)

    def test_a_flat_window_does_not_divide_by_zero(self) -> None:
        flat = [
            Bar(
                ts=START + timedelta(minutes=index),
                open=Decimal(5000),
                high=Decimal(5000),
                low=Decimal(5000),
                close=Decimal(5000),
                volume=Decimal(1),
            )
            for index in range(5)
        ]

        assert render_chart(flat).bars == 5

    def test_a_level_outside_the_bars_widens_the_window(self) -> None:
        """A stop below every low must be visible, not clipped to the bottom edge.

        Clipping would draw it exactly where a stop that was nearly touched would sit —
        the most misleading possible position for it.
        """
        far_below = Marker(
            MarkerKind.STOP, None, Decimal(4000), "stop", spans_trade=True
        )

        with_level = render_chart(bars(), markers=[far_below])
        without = render_chart(bars())

        assert with_level.png != without.png


class TestFrames:
    def window(self) -> ReplayWindow:
        series = bars()
        return ReplayWindow(
            instrument_symbol="ESU6",
            primary_timeframe=Timeframe.M2,
            higher_timeframe=Timeframe.M30,
            window_start=series[0].ts,
            window_end=series[-1].ts,
            trade_start=series[10].ts,
            trade_end=series[20].ts,
            bars_before=10,
            bars_after=19,
            markers=(
                Marker(MarkerKind.ENTRY, series[10].ts, Decimal(5010), "entry"),
                Marker(MarkerKind.EXIT, series[20].ts, Decimal(5020), "exit"),
                Marker(MarkerKind.STOP, None, Decimal(5005), "stop", spans_trade=True),
            ),
        )

    def test_the_before_entry_frame_does_not_draw_the_exit(self) -> None:
        """**The test this file exists for.**

        A "what did it look like before I took it" image with the exit marked on it is a
        chart that knows the answer. Every judgement formed from it is contaminated, and
        nothing about the image looks wrong.
        """
        series = bars()
        frames = {
            frame.kind: frame
            for frame in frames_for(
                self.window(), series, series[::15], captured_at=START
            )
        }

        before = frames[ScreenshotKind.BEFORE_ENTRY]
        exit_frame = frames[ScreenshotKind.EXIT]

        # Rendered without the exit marker, the before-entry slice must be identical to
        # what the renderer produces from that slice alone. Any difference means an
        # overlay from the future reached it.
        expected = render_chart(
            series[: 11],
            markers=[
                Marker(MarkerKind.ENTRY, series[10].ts, Decimal(5010), "entry"),
                Marker(MarkerKind.STOP, None, Decimal(5005), "stop", spans_trade=True),
            ],
            highlight_at=series[10].ts,
        )
        assert before.png == expected.png
        assert before.png != exit_frame.png

    def test_all_six_kinds_are_produced(self) -> None:
        series = bars()

        frames = frames_for(self.window(), series, series[::15], captured_at=START)

        assert {frame.kind for frame in frames} == set(ScreenshotKind)

    def test_the_frames_are_not_six_copies_of_one_chart(self) -> None:
        """The failure that would make this feature pointless while looking complete."""
        series = bars()

        frames = frames_for(self.window(), series, series[::15], captured_at=START)

        assert len({frame.png for frame in frames}) == len(frames)

    def test_the_higher_timeframe_frame_is_labelled_as_such(self) -> None:
        series = bars()

        frames = {
            frame.kind: frame
            for frame in frames_for(
                self.window(), series, series[::15], captured_at=START
            )
        }

        assert frames[ScreenshotKind.HIGHER_TIMEFRAME].timeframe is Timeframe.M30
        assert frames[ScreenshotKind.EXECUTION_TIMEFRAME].timeframe is Timeframe.M2
