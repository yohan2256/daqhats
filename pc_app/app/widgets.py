"""Hand-drawn meter widgets — QPainter only, no charting library.

Staying inside PySide6-Essentials keeps deployment simple (QtCharts lives in
Addons and would be an extra dependency).
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

def channel_label(index: int) -> str:
    """Display name for a channel.

    The protocol numbers channels from 0, but operators count receiver
    positions from 1, so everything on screen is shifted by one. The stored
    index stays 0-based to match the wire format.
    """
    return f"Ch{index + 1}"


BACKGROUND = QtGui.QColor(28, 30, 34)
GRID = QtGui.QColor(60, 64, 70)
TEXT = QtGui.QColor(210, 214, 220)
BAR = QtGui.QColor(86, 156, 214)
BAR_HOT = QtGui.QColor(224, 108, 87)
PEAK = QtGui.QColor(240, 200, 90)
REFERENCE = QtGui.QColor(150, 200, 130)


class LevelBars(QtWidgets.QWidget):
    """Broadband level bars per channel, with peak hold."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(130)
        self._levels: dict[int, float] = {}
        self._peaks: dict[int, float] = {}
        #: Channels to draw. Empty means "whatever arrives" — but once the
        #: session says which channels are in use, the unused ones are hidden
        #: rather than shown as dead bars competing for width.
        self._channels: tuple[int, ...] = ()
        self.floor = 10.0
        self.ceiling = 110.0
        self.alarm = 100.0

    def set_channels(self, channels) -> None:
        self._channels = tuple(sorted(channels))
        self.update()

    def set_levels(self, levels: dict[int, float]) -> None:
        self._levels = levels
        for channel, value in levels.items():
            self._peaks[channel] = max(self._peaks.get(channel, value), value)
        self.update()

    def reset_peaks(self) -> None:
        self._peaks.clear()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, False)
        rect = self.rect()
        painter.fillRect(rect, BACKGROUND)

        if not self._levels:
            painter.setPen(TEXT)
            painter.drawText(rect, QtCore.Qt.AlignCenter, "Waiting for level frames")
            return

        channels = [c for c in sorted(self._levels)
                    if not self._channels or c in self._channels]
        if not channels:
            return
        margin_left, margin_bottom, margin_top = 46, 20, 8
        plot = rect.adjusted(margin_left, margin_top, -8, -margin_bottom)
        span = self.ceiling - self.floor

        painter.setPen(GRID)
        for db in range(int(self.floor), int(self.ceiling) + 1, 20):
            y = plot.bottom() - (db - self.floor) / span * plot.height()
            painter.drawLine(plot.left(), int(y), plot.right(), int(y))
            painter.setPen(TEXT)
            painter.drawText(2, int(y) + 4, f"{db:>3d}")
            painter.setPen(GRID)

        width = plot.width() / max(1, len(channels))
        for index, channel in enumerate(channels):
            value = self._levels[channel]
            clamped = max(self.floor, min(self.ceiling, value))
            height = (clamped - self.floor) / span * plot.height()
            x = plot.left() + index * width + width * 0.2
            bar_width = width * 0.6
            color = BAR_HOT if value >= self.alarm else BAR
            painter.fillRect(
                QtCore.QRectF(x, plot.bottom() - height, bar_width, height), color
            )

            peak = self._peaks.get(channel)
            if peak is not None:
                py = plot.bottom() - (max(self.floor, min(self.ceiling, peak)) - self.floor) / span * plot.height()
                painter.setPen(QtGui.QPen(PEAK, 2))
                painter.drawLine(QtCore.QPointF(x, py), QtCore.QPointF(x + bar_width, py))

            painter.setPen(TEXT)
            painter.drawText(
                QtCore.QRectF(x - width * 0.2, plot.bottom() + 2, width, 18),
                QtCore.Qt.AlignCenter,
                channel_label(channel),
            )
            painter.drawText(
                QtCore.QRectF(x - width * 0.2, plot.bottom() - height - 18, width, 16),
                QtCore.Qt.AlignCenter,
                f"{value:.1f}",
            )


#: One colour per channel (six channels)
CHANNEL_COLORS = (
    QtGui.QColor(86, 156, 214),   # blue
    QtGui.QColor(106, 190, 130),  # green
    QtGui.QColor(224, 160, 87),   # orange
    QtGui.QColor(190, 130, 210),  # purple
    QtGui.QColor(224, 108, 118),  # red
    QtGui.QColor(120, 200, 200),  # teal
)


class SpectrumBars(QtWidgets.QWidget):
    """1/3-octave bars, grouped by channel.

    One excitation records every receiver position at once, so each band gets
    one bar per channel side by side. The vertical axis spells out what is
    being shown (Lp,Leq or Lp,Fmax) — without that label the numbers are
    ambiguous once the session is over.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(240)
        #: channel -> {nominal frequency: level}
        self._series: dict[int, dict[float, float]] = {}
        self._captured: dict[int, dict[float, float]] = {}
        self._reference: dict[float, float] = {}
        self.floor = 0.0
        self.ceiling = 90.0
        self.quantity = "Lp"
        self.weighting = "Z"

    def set_series(self, series: dict[int, dict[float, float]]) -> None:
        self._series = series
        self.update()

    def set_captured(self, captured: dict[int, dict[float, float]]) -> None:
        self._captured = captured
        self.update()

    def set_reference(self, reference: dict[float, float]) -> None:
        self._reference = reference
        self.update()

    def set_quantity(self, quantity: str, weighting: str = "Z") -> None:
        self.quantity = quantity
        self.weighting = weighting
        self.update()

    def _all_bands(self) -> list[float]:
        bands: set[float] = set(self._reference)
        for table in list(self._series.values()) + list(self._captured.values()):
            bands |= set(table)
        return sorted(bands)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, BACKGROUND)

        bands = self._all_bands()
        if not bands:
            painter.setPen(TEXT)
            painter.drawText(rect, QtCore.Qt.AlignCenter, "Waiting for band level frames")
            return

        margin_left, margin_bottom, margin_top = 44, 40, 22
        plot = rect.adjusted(margin_left, margin_top, -8, -margin_bottom)
        span = self.ceiling - self.floor

        def y_of(db: float) -> float:
            clamped = max(self.floor, min(self.ceiling, db))
            return plot.bottom() - (clamped - self.floor) / span * plot.height()

        # Vertical axis title — what is actually being shown
        painter.setPen(TEXT)
        title_font = painter.font()
        title_font.setPointSize(9)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.drawText(
            QtCore.QRectF(margin_left, 2, plot.width(), 18),
            QtCore.Qt.AlignLeft,
            f"{self.quantity} [{self.weighting}], dB   ·  1/3 octave",
        )
        title_font.setBold(False)
        painter.setFont(title_font)

        painter.setPen(GRID)
        for db in range(int(self.floor), int(self.ceiling) + 1, 10):
            y = y_of(db)
            painter.drawLine(plot.left(), int(y), plot.right(), int(y))
            painter.setPen(TEXT)
            painter.drawText(2, int(y) + 4, f"{db:>3d}")
            painter.setPen(GRID)

        channels = sorted(set(self._series) | set(self._captured))
        if not channels:
            channels = [0]
        slot = plot.width() / len(bands)
        bar_width = slot * 0.8 / len(channels)

        font = painter.font()
        font.setPointSize(7 if len(bands) > 10 else 8)
        painter.setFont(font)

        for band_index, band in enumerate(bands):
            base_x = plot.left() + band_index * slot + slot * 0.1
            for slot_index, channel in enumerate(channels):
                color = CHANNEL_COLORS[channel % len(CHANNEL_COLORS)]
                x = base_x + slot_index * bar_width
                value = self._series.get(channel, {}).get(band)
                if value is not None:
                    painter.fillRect(
                        QtCore.QRectF(x, y_of(value), bar_width * 0.9,
                                      plot.bottom() - y_of(value)),
                        color,
                    )
                captured = self._captured.get(channel, {}).get(band)
                if captured is not None:
                    pen = QtGui.QPen(color.lighter(150), 2)
                    painter.setPen(pen)
                    painter.drawLine(
                        QtCore.QPointF(x, y_of(captured)),
                        QtCore.QPointF(x + bar_width * 0.9, y_of(captured)),
                    )
            painter.setPen(TEXT)
            label = f"{band:g}" if band < 1000 else f"{band / 1000:g}k"
            painter.drawText(
                QtCore.QRectF(plot.left() + band_index * slot, plot.bottom() + 2, slot, 16),
                QtCore.Qt.AlignCenter,
                label,
            )

        # Legend
        legend_x = plot.left()
        for channel in channels:
            color = CHANNEL_COLORS[channel % len(CHANNEL_COLORS)]
            painter.fillRect(QtCore.QRectF(legend_x, plot.bottom() + 20, 10, 10), color)
            painter.setPen(TEXT)
            painter.drawText(
                QtCore.QRectF(legend_x + 13, plot.bottom() + 19, 34, 12),
                QtCore.Qt.AlignLeft,
                channel_label(channel),
            )
            legend_x += 50

        if self._reference:
            painter.setPen(QtGui.QPen(REFERENCE, 2, QtCore.Qt.DashLine))
            points = [
                QtCore.QPointF(plot.left() + bands.index(b) * slot + slot / 2,
                               y_of(self._reference[b]))
                for b in bands
                if b in self._reference
            ]
            if len(points) > 1:
                painter.drawPolyline(QtGui.QPolygonF(points))


class SpectrumGrid(QtWidgets.QWidget):
    """One 1/3-octave panel per channel, laid out as a grid.

    Grouping 16 bands × 6 channels into side-by-side bars produced 96 bars in
    one axis, which is unreadable. A panel per channel keeps each spectrum
    legible and makes differences between receiver positions obvious.

    The API matches SpectrumBars so it can be dropped in place of it.
    """

    def __init__(self, columns: int = 3, rows: int = 2, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(260)
        self.columns = columns
        self.rows = rows
        self._series: dict[int, dict[float, float]] = {}
        self._captured: dict[int, dict[float, float]] = {}
        self._reference: dict[float, float] = {}
        self._channels: list[int] = list(range(columns * rows))
        self.floor = 0.0
        self.ceiling = 90.0
        self.quantity = "Lp"
        self.weighting = "Z"

    # ── Same API as SpectrumBars ──
    def set_series(self, series: dict[int, dict[float, float]]) -> None:
        self._series = series
        self.update()

    def set_captured(self, captured: dict[int, dict[float, float]]) -> None:
        self._captured = captured
        self.update()

    def set_reference(self, reference: dict[float, float]) -> None:
        self._reference = reference
        self.update()

    def set_quantity(self, quantity: str, weighting: str = "Z") -> None:
        self.quantity = quantity
        self.weighting = weighting
        self.update()

    def set_channels(self, channels) -> None:
        """Which channels get a panel, and in what order."""
        self._channels = list(channels) or list(range(self.columns * self.rows))
        self.update()

    def _bands_for(self, channel: int) -> list[float]:
        bands = set(self._series.get(channel, {})) | set(self._captured.get(channel, {}))
        return sorted(bands or set(self._reference))

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, BACKGROUND)

        painter.setPen(TEXT)
        header = painter.font()
        header.setPointSize(9)
        header.setBold(True)
        painter.setFont(header)
        painter.drawText(
            QtCore.QRectF(6, 2, rect.width() - 12, 16),
            QtCore.Qt.AlignLeft,
            f"{self.quantity} [{self.weighting}], dB   ·  1/3 octave per channel",
        )
        header.setBold(False)
        painter.setFont(header)

        top = 20
        channels = self._channels[: self.columns * self.rows]
        if not channels:
            painter.drawText(rect, QtCore.Qt.AlignCenter, "No channels selected")
            return

        cell_w = rect.width() / self.columns
        cell_h = (rect.height() - top) / self.rows
        for position, channel in enumerate(channels):
            row, col = divmod(position, self.columns)
            cell = QtCore.QRectF(
                col * cell_w, top + row * cell_h, cell_w - 4, cell_h - 4
            )
            self._paint_cell(painter, cell, channel)

    def _paint_cell(self, painter: QtGui.QPainter, cell: QtCore.QRectF, channel: int) -> None:
        color = CHANNEL_COLORS[channel % len(CHANNEL_COLORS)]
        painter.setPen(QtGui.QPen(GRID, 1))
        painter.drawRect(cell)

        label_h = 14
        axis_w = 22
        plot = QtCore.QRectF(
            cell.left() + axis_w,
            cell.top() + label_h,
            cell.width() - axis_w - 4,
            cell.height() - label_h - 12,
        )
        span = self.ceiling - self.floor

        def y_of(db: float) -> float:
            clamped = max(self.floor, min(self.ceiling, db))
            return plot.bottom() - (clamped - self.floor) / span * plot.height()

        small = painter.font()
        small.setPointSize(7)
        painter.setFont(small)

        painter.setPen(color)
        painter.drawText(
            QtCore.QRectF(cell.left() + 3, cell.top() + 1, cell.width(), label_h),
            QtCore.Qt.AlignLeft,
            channel_label(channel),
        )

        painter.setPen(GRID)
        for db in range(int(self.floor), int(self.ceiling) + 1, 20):
            y = y_of(db)
            painter.drawLine(QtCore.QPointF(plot.left(), y), QtCore.QPointF(plot.right(), y))
            painter.setPen(TEXT)
            painter.drawText(QtCore.QRectF(cell.left(), y - 6, axis_w - 2, 12),
                             QtCore.Qt.AlignRight, f"{db:d}")
            painter.setPen(GRID)

        bands = self._bands_for(channel)
        levels = self._series.get(channel, {})
        captured = self._captured.get(channel, {})
        if not bands or (not levels and not captured):
            painter.setPen(TEXT)
            painter.drawText(plot, QtCore.Qt.AlignCenter, "no data")
            return

        slot = plot.width() / len(bands)
        for index, band in enumerate(bands):
            x = plot.left() + index * slot
            value = levels.get(band)
            if value is not None:
                painter.fillRect(
                    QtCore.QRectF(x + slot * 0.12, y_of(value), slot * 0.76,
                                  plot.bottom() - y_of(value)),
                    color,
                )
            held = captured.get(band)
            if held is not None:
                painter.setPen(QtGui.QPen(PEAK, 2))
                painter.drawLine(
                    QtCore.QPointF(x + slot * 0.05, y_of(held)),
                    QtCore.QPointF(x + slot * 0.95, y_of(held)),
                )

        # Only label the ends, so narrow panels stay readable
        painter.setPen(TEXT)
        for index in (0, len(bands) - 1):
            band = bands[index]
            text = f"{band:g}" if band < 1000 else f"{band / 1000:g}k"
            painter.drawText(
                QtCore.QRectF(plot.left() + index * slot - slot, plot.bottom() + 1,
                              slot * 3, 11),
                QtCore.Qt.AlignCenter,
                text,
            )


class StatusLight(QtWidgets.QLabel):
    """Connection / status indicator."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.set_state("idle", "Not connected")

    def set_state(self, state: str, text: str) -> None:
        colors = {
            "idle": "#6b7280",
            "ok": "#4ade80",
            "busy": "#fbbf24",
            "error": "#f87171",
        }
        color = colors.get(state, "#6b7280")
        self.setText(f"● {text}")
        self.setStyleSheet(f"color: {color}; font-weight: 600;")
