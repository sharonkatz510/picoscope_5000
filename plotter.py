from __future__ import annotations

from typing import Tuple

import numpy as np
from PyQt5 import QtWidgets
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib import ticker as mticker


class PlotterWidget(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.canvas = FigureCanvas(Figure(figsize=(6, 3), dpi=100))
        self.ax = self.canvas.figure.add_subplot(111)
        self.ax.grid(True)
        self.ax.set_ylim(-0.5, 0.5)
        (self.line_a,) = self.ax.plot([], [], color='c', linewidth=1, label='Channel A')
        (self.line_b,) = self.ax.plot([], [], color='m', linewidth=1, label='Channel B')
        self.ax.legend(loc='upper right')
        layout.addWidget(self.canvas, 1)

        # Cursor state
        self._cursor_step_frac_x = 0.002
        self._cursor_step_frac_y = 0.008
        self._cursor_positions = {
            'v': [0.0, 0.0],  # x positions (X1, X2)
            'h': [-0.25, 0.25],  # y positions (Y1, Y2); initial default, will be reset
        }
        self._cursor_lines_v: list[Line2D] = []
        self._cursor_lines_h: list[Line2D] = []
        self._init_cursors()

    # ----- Public API -----
    def update_series(self, t: np.ndarray, ya_norm: np.ndarray, yb_norm: np.ndarray, max_points: int) -> None:
        tt_d, ya_d = self._decimate(t, ya_norm, max_points)
        _, yb_d = self._decimate(t, yb_norm, max_points)
        self.line_a.set_data(tt_d, ya_d)
        self.line_b.set_data(tt_d, yb_d)
        if len(tt_d) > 1:
            self.ax.set_xlim(float(tt_d[0]), float(tt_d[-1]))
        self.ax.set_ylim(-0.5, 0.5)
        self._update_cursor_artists()
        self.canvas.draw_idle()

    def apply_time_axis_format(self, window_s: float) -> None:
        if window_s < 1e-3:
            self.ax.set_xlabel("Time (µs)")
            self.ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, pos: f"{x * 1e6:.1f}"))
        else:
            self.ax.set_xlabel("Time (ms)")
            self.ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, pos: f"{x * 1e3:.1f}"))
        self._update_cursor_artists()
        self.canvas.draw_idle()

    def move_cursor(self, kind: str, idx: int, direction: int) -> None:
        xmin, xmax = self._current_xlim()
        ymin, ymax = self._current_ylim()
        if kind == 'v':
            step = self._cursor_step_frac_x * (xmax - xmin)
            newx = float(self._cursor_positions['v'][idx]) + float(direction) * step
            newx = min(max(newx, xmin), xmax)
            self._cursor_positions['v'][idx] = newx
        else:
            step = self._cursor_step_frac_y * (ymax - ymin)
            newy = float(self._cursor_positions['h'][idx]) + float(direction) * step
            newy = min(max(newy, ymin), ymax)
            self._cursor_positions['h'][idx] = newy
        self._update_cursor_artists()
        self.canvas.draw_idle()

    def get_cursor_values(self) -> Tuple[float, float, float, float, float, float]:
        x1 = float(self._cursor_positions['v'][0])
        x2 = float(self._cursor_positions['v'][1])
        y1 = float(self._cursor_positions['h'][0])
        y2 = float(self._cursor_positions['h'][1])
        dx = abs(x2 - x1)
        dy = abs(y2 - y1)
        return x1, x2, y1, y2, dx, dy

    # ----- Internals -----
    def _decimate(self, x: np.ndarray, y: np.ndarray, max_points: int):
        n = len(x)
        if n <= max_points:
            return x, y
        idx = np.linspace(0, n - 1, max_points, dtype=np.int32)
        return x[idx], y[idx]

    def _init_cursors(self) -> None:
        xmin, xmax = self._current_xlim()
        ymin, ymax = self._current_ylim()
        xr = xmax - xmin
        yr = ymax - ymin
        self._cursor_positions['v'][0] = xmin + 0.25 * xr
        self._cursor_positions['v'][1] = xmin + 0.75 * xr
        self._cursor_positions['h'][0] = ymin + 0.25 * yr
        self._cursor_positions['h'][1] = ymin + 0.75 * yr

        for color in ["#2ca02c", "#ff7f0e"]:
            line = Line2D([0, 0], [ymin, ymax], color=color, linestyle="--", linewidth=1.0, alpha=0.9)
            self.ax.add_line(line)
            self._cursor_lines_v.append(line)
        # Use neutral grays for Y cursors to avoid confusion with channel colors
        for color in ["#4d4d4d", "#7f7f7f"]:
            line = Line2D([xmin, xmax], [0, 0], color=color, linestyle=":", linewidth=1.0, alpha=0.9)
            self.ax.add_line(line)
            self._cursor_lines_h.append(line)
        self._update_cursor_artists()

    def _current_xlim(self) -> Tuple[float, float]:
        xmin, xmax = self.ax.get_xlim()
        if not np.isfinite(xmin) or not np.isfinite(xmax) or xmin == xmax:
            return 0.0, 1.0
        return float(xmin), float(xmax)

    def _current_ylim(self) -> Tuple[float, float]:
        ymin, ymax = self.ax.get_ylim()
        if not np.isfinite(ymin) or not np.isfinite(ymax) or ymin == ymax:
            return -0.5, 0.5
        return float(ymin), float(ymax)

    def _update_cursor_artists(self) -> None:
        xmin, xmax = self._current_xlim()
        ymin, ymax = self._current_ylim()
        for i, line in enumerate(self._cursor_lines_v):
            x = float(self._cursor_positions['v'][i])
            x = min(max(x, xmin), xmax)
            line.set_data([x, x], [ymin, ymax])
        for i, line in enumerate(self._cursor_lines_h):
            y = float(self._cursor_positions['h'][i])
            y = min(max(y, ymin), ymax)
            line.set_data([xmin, xmax], [y, y])
