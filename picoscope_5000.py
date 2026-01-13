"""
PicoScope 5000B (ps5000a driver) streaming viewer for Channels A and B.

- Windows 11
- Uses ctypes to call ps5000a.dll (PicoSDK)
- Streams data at 20 MHz (requested sample interval 50 ns)
- Keeps a rolling 20 ms buffer (400,000 samples/channel at 20 MHz)
- Plots live data in a PyQtGraph GUI, refreshing every 20 ms

Tested approach: follows the ps5000aRunStreaming + ps5000aGetStreamingLatestValues callback pattern.
"""

from __future__ import annotations

import sys

import numpy as np

from PyQt5 import QtCore, QtWidgets, QtGui
from plotter import PlotterWidget
from picoscope_constants import (
    RANGE_CODES,
    RANGE_LABELS,
    RANGE_TO_VOLTS,
    PS5000A_CHANNEL_A,
    PS5000A_CHANNEL_B,
)
from picoscope_driver import (
    StreamConfig as DriverStreamConfig,
    PicoScopeStreamer as DriverPicoScopeStreamer,
)


## Constants moved to picoscope_constants.py


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PicoScope 5000B Streaming Viewer")

        self.cfg = DriverStreamConfig()
        # Build UI: controls row + plot
        central = QtWidgets.QWidget()
        vbox = QtWidgets.QVBoxLayout(central)
        ctrl = QtWidgets.QWidget()
        hbox = QtWidgets.QHBoxLayout(ctrl)

        self.status_lbl = QtWidgets.QLabel("Status: Initializing…")
        hbox.addWidget(self.status_lbl)

        hbox.addSpacing(20)
        self.a_range_lbl = QtWidgets.QLabel(f"A Range: {RANGE_LABELS[self.cfg.range_a]}")
        self.a_range_combo = QtWidgets.QComboBox()
        for code in RANGE_CODES:
            self.a_range_combo.addItem(RANGE_LABELS[code], userData=code)
        try:
            a_idx = RANGE_CODES.index(self.cfg.range_a)
            self.a_range_combo.setCurrentIndex(a_idx)
        except ValueError:
            pass
        self.a_range_combo.currentIndexChanged.connect(self._on_a_range_changed)
        hbox.addWidget(self.a_range_lbl)
        hbox.addWidget(self.a_range_combo)

        hbox.addSpacing(10)
        self.b_range_lbl = QtWidgets.QLabel(f"B Range: {RANGE_LABELS[self.cfg.range_b]}")
        self.b_range_combo = QtWidgets.QComboBox()
        for code in RANGE_CODES:
            self.b_range_combo.addItem(RANGE_LABELS[code], userData=code)
        try:
            b_idx = RANGE_CODES.index(self.cfg.range_b)
            self.b_range_combo.setCurrentIndex(b_idx)
        except ValueError:
            pass
        self.b_range_combo.currentIndexChanged.connect(self._on_b_range_changed)
        hbox.addWidget(self.b_range_lbl)
        hbox.addWidget(self.b_range_combo)

        hbox.addSpacing(20)
        self.rate_lbl = QtWidgets.QLabel("Rate:")
        self.rate_combo = QtWidgets.QComboBox()
        self.rate_combo.addItems([
            "50 ns", "100 ns", "200 ns", "500 ns", "1 us", "2 us", "5 us", "10 us"
        ])
        # Set default selection to 1 us
        self.rate_combo.setCurrentIndex(4)
        self.apply_rate_btn = QtWidgets.QPushButton("Apply Rate")
        self.apply_rate_btn.clicked.connect(self._apply_rate)
        hbox.addWidget(self.rate_lbl)
        hbox.addWidget(self.rate_combo)
        hbox.addWidget(self.apply_rate_btn)

        # Trigger controls
        hbox.addSpacing(20)
        self.trigger_chk = QtWidgets.QCheckBox("Trigger")
        self.trigger_chk.setChecked(self.cfg.simple_trigger_enabled)
        hbox.addWidget(self.trigger_chk)
        self.trigger_lbl = QtWidgets.QLabel("Level (V):")
        hbox.addWidget(self.trigger_lbl)
        self.trigger_edit = QtWidgets.QLineEdit()
        self.trigger_edit.setFixedWidth(80)
        self.trigger_edit.setPlaceholderText("e.g. 0.100")
        # Initialize from current config (assume trigger source A by default)
        init_rng = self.cfg.range_a if self.cfg.trigger_source == PS5000A_CHANNEL_A else self.cfg.range_b
        init_v = self.cfg.trigger_threshold_pct * RANGE_TO_VOLTS.get(init_rng, 2.0)
        self.trigger_edit.setText(f"{init_v:.3f}")
        hbox.addWidget(self.trigger_edit)
        # Wire events
        self.trigger_chk.toggled.connect(self._apply_trigger_ui)
        self.trigger_edit.editingFinished.connect(self._apply_trigger_ui)

        # Timebase +/- buttons (discrete window sizes)
        hbox.addSpacing(20)
        self.timebase_dec_btn = QtWidgets.QPushButton("−")
        self.timebase_inc_btn = QtWidgets.QPushButton("+")
        self.timebase_dec_btn.setFixedWidth(30)
        self.timebase_inc_btn.setFixedWidth(30)
        self.timebase_dec_btn.setToolTip("Decrease timebase (shorter window)")
        self.timebase_inc_btn.setToolTip("Increase timebase (longer window)")
        self.timebase_dec_btn.clicked.connect(self._on_timebase_dec)
        self.timebase_inc_btn.clicked.connect(self._on_timebase_inc)
        hbox.addWidget(QtWidgets.QLabel("Timebase:"))
        hbox.addWidget(self.timebase_dec_btn)
        hbox.addWidget(self.timebase_inc_btn)

        # Explicit window control (ms)
        hbox.addSpacing(20)
        self.window_lbl = QtWidgets.QLabel("Window (ms):")
        hbox.addWidget(self.window_lbl)
        self.window_spin = QtWidgets.QDoubleSpinBox()
        self.window_spin.setDecimals(3)
        self.window_spin.setMinimum(0.010)  # 10 µs
        self.window_spin.setMaximum(10.000) # 10 ms
        self.window_spin.setSingleStep(0.010)
        self.window_spin.setValue(float(self.cfg.plot_window_ms))
        self.window_spin.valueChanged.connect(self._on_window_changed)
        hbox.addWidget(self.window_spin)

        vbox.addWidget(ctrl)

        # Plot widget + right-side panel for cursor readouts
        self.plotter = PlotterWidget(self)
        content = QtWidgets.QWidget()
        content_h = QtWidgets.QHBoxLayout(content)
        content_h.setContentsMargins(0, 0, 0, 0)
        content_h.addWidget(self.plotter, 1)

        self.sidebar = QtWidgets.QWidget()
        side_v = QtWidgets.QVBoxLayout(self.sidebar)
        side_v.setContentsMargins(10, 5, 10, 5)
        self.sidebar.setFixedWidth(200)
        title = QtWidgets.QLabel("Cursors")
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        side_v.addWidget(title)
        side_v.addSpacing(4)

        # X cursor readouts
        side_v.addWidget(QtWidgets.QLabel("X (time):"))
        self.lbl_v1 = QtWidgets.QLabel("X1: —")
        self.lbl_v2 = QtWidgets.QLabel("X2: —")
        self.lbl_dx = QtWidgets.QLabel("Δx: —")
        side_v.addWidget(self.lbl_v1)
        side_v.addWidget(self.lbl_v2)
        side_v.addWidget(self.lbl_dx)

        side_v.addSpacing(8)
        # Y cursor readouts
        side_v.addWidget(QtWidgets.QLabel("Y (amplitude):"))
        self.lbl_h1 = QtWidgets.QLabel("Y1: —")
        self.lbl_h2 = QtWidgets.QLabel("Y2: —")
        self.lbl_dy = QtWidgets.QLabel("Δy: —")
        side_v.addWidget(self.lbl_h1)
        side_v.addWidget(self.lbl_h2)
        side_v.addWidget(self.lbl_dy)
        side_v.addStretch(1)

        content_h.addWidget(self.sidebar, 0)
        vbox.addWidget(content, 1)
        self.setCentralWidget(central)

        window_s = self.cfg.plot_window_ms * 1e-3
        self.dt = self.cfg.sample_interval_ns * 1e-9
        self.t = np.linspace(0.0, window_s - self.dt, int(round(window_s / self.dt)), dtype=np.float64)

        self.streamer: DriverPicoScopeStreamer | None = None
        self._synthetic_phase = 0.0
        # Base rate tracking
        self._rate_base_ns: int = int(self.cfg.sample_interval_ns)
        # Discrete timebase steps in seconds (10 µs .. 10 ms)
        self._timebase_steps_s = [
            10e-6, 20e-6, 50e-6,
            100e-6, 200e-6, 500e-6,
            1e-3, 2e-3, 5e-3, 10e-3,
        ]

        # Cursor controls (2 vertical + 2 horizontal) and UI
        hbox.addSpacing(20)
        hbox.addWidget(QtWidgets.QLabel("Cursor:"))
        self.cursor_select = QtWidgets.QComboBox()
        # Order matters: used by logic
        self.cursor_select.addItem("X1", userData=("v", 0))
        self.cursor_select.addItem("X2", userData=("v", 1))
        self.cursor_select.addItem("Y1", userData=("h", 0))
        self.cursor_select.addItem("Y2", userData=("h", 1))
        hbox.addWidget(self.cursor_select)
        self.cursor_dec_btn = QtWidgets.QPushButton("◀ / ▼")
        self.cursor_inc_btn = QtWidgets.QPushButton("▶ / ▲")
        self.cursor_dec_btn.setFixedWidth(70)
        self.cursor_inc_btn.setFixedWidth(70)
        self.cursor_dec_btn.setToolTip("Move left (X) / down (Y)")
        self.cursor_inc_btn.setToolTip("Move right (X) / up (Y)")
        self.cursor_dec_btn.clicked.connect(lambda: self._on_cursor_move(-1))
        self.cursor_inc_btn.clicked.connect(lambda: self._on_cursor_move(1))
        hbox.addWidget(self.cursor_dec_btn)
        hbox.addWidget(self.cursor_inc_btn)

        self._refresh_cursor_readouts()

        try:
            self.streamer = DriverPicoScopeStreamer(self.cfg)
            self.streamer.open()
            self.streamer.start()
            actual_ns = self.streamer.cfg.sample_interval_ns
            self._rate_base_ns = int(actual_ns)
            self._update_time_axis(actual_ns)
            # Ensure axis formatter matches current window size
            self._apply_time_axis_format(self.cfg.plot_window_ms * 1e-3)
            self.status_lbl.setText(f"Status: Hardware @ {actual_ns} ns; Res {self.streamer.cfg.resolution}")
        except Exception as e:
            print(f"Error during initialization: {e}")
            self.streamer = None
            self.status_lbl.setText(f"Status: Error — {e}")

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(self.cfg.plot_refresh_ms)
        self.timer.timeout.connect(self.update_plot)
        self.timer.start()

        self.resize(900, 500)
        # Keyboard shortcuts for cursor movement (Left/Right for X, Up/Down for Y)
        self._short_left = QtWidgets.QShortcut(QtGui.QKeySequence('Left'), self)
        self._short_left.activated.connect(lambda: self._on_key_move('v', -1))
        self._short_right = QtWidgets.QShortcut(QtGui.QKeySequence('Right'), self)
        self._short_right.activated.connect(lambda: self._on_key_move('v', 1))
        self._short_up = QtWidgets.QShortcut(QtGui.QKeySequence('Up'), self)
        self._short_up.activated.connect(lambda: self._on_key_move('h', 1))
        self._short_down = QtWidgets.QShortcut(QtGui.QKeySequence('Down'), self)
        self._short_down.activated.connect(lambda: self._on_key_move('h', -1))

    def update_plot(self) -> None:
        if self.streamer and self.streamer._running:
            with self.streamer._lock:
                ya = self.streamer._y_a.copy()
                yb = self.streamer._y_b.copy()
                tt = self.streamer._t.copy()
            # Normalize each channel by its selected full-scale voltage range
            fs_a = RANGE_TO_VOLTS.get(self.streamer.cfg.range_a, 1.0)
            fs_b = RANGE_TO_VOLTS.get(self.streamer.cfg.range_b, 1.0)
            ya_n = ya / fs_a if fs_a else ya
            yb_n = yb / fs_b if fs_b else yb

            # Delegate plotting and cursor rendering to PlotterWidget
            self.plotter.update_series(tt, ya_n, yb_n, self.cfg.plot_max_points)
        else:
            # No hardware or not running: do not draw synthetic data
            return

    def _on_a_range_changed(self, idx: int) -> None:
        code = self.a_range_combo.itemData(idx)
        if code is None:
            return
        self._apply_range(PS5000A_CHANNEL_A, int(code))

    def _on_b_range_changed(self, idx: int) -> None:
        code = self.b_range_combo.itemData(idx)
        if code is None:
            return
        self._apply_range(PS5000A_CHANNEL_B, int(code))

    def _apply_range(self, channel: int, new_range: int) -> None:
        # Update hardware if available; restart streaming to avoid device freeze
        if self.streamer:
            try:
                was_running = self.streamer._running
                if was_running:
                    self.streamer.stop()
                self.streamer.set_range(channel, new_range)
                if was_running:
                    self.streamer.start()
            except Exception as e:
                self.status_lbl.setText(f"Status: Range change failed — {e}")
                return
        else:
            # No hardware: update config only
            if channel == PS5000A_CHANNEL_A:
                self.cfg.range_a = new_range
            else:
                self.cfg.range_b = new_range
        # Update labels
        if channel == PS5000A_CHANNEL_A:
            self.a_range_lbl.setText(f"A Range: {RANGE_LABELS[new_range]}")
        else:
            self.b_range_lbl.setText(f"B Range: {RANGE_LABELS[new_range]}")
    def _cycle_range(self, channel: int) -> None:
        # Determine current range and cycle to next
        current = self.cfg.range_a if channel == PS5000A_CHANNEL_A else self.cfg.range_b
        try:
            idx = RANGE_CODES.index(current)
        except ValueError:
            idx = 0
        new_idx = (idx + 1) % len(RANGE_CODES)
        new_range = RANGE_CODES[new_idx]

        if self.streamer and self.streamer._running:
            try:
                self.streamer.set_range(channel, new_range)
            except Exception as e:
                self.status_lbl.setText(f"Status: Range change failed — {e}")
                return
        else:
            # No hardware: update config so labels stay in sync
            if channel == PS5000A_CHANNEL_A:
                self.cfg.range_a = new_range
            else:
                self.cfg.range_b = new_range

        if channel == PS5000A_CHANNEL_A:
            self.a_range_lbl.setText(f"A Range: {RANGE_LABELS[new_range]}")
        else:
            self.b_range_lbl.setText(f"B Range: {RANGE_LABELS[new_range]}")

    def _apply_rate(self) -> None:
        # Parse selected rate and restart streaming (hardware if present; else update synthetic axis)
        text = self.rate_combo.currentText()
        mapping = {
            "50 ns": 50,
            "100 ns": 100,
            "200 ns": 200,
            "500 ns": 500,
            "1 us": 1000,
            "2 us": 2000,
            "5 us": 5000,
            "10 us": 10_000,
        }
        base_ns = mapping.get(text, 1000)
        self._rate_base_ns = int(base_ns)
        target_ns = int(base_ns)
        self.cfg.sample_interval_ns = target_ns

        if self.streamer:
            try:
                self.streamer.stop()
                # Reconfigure acquisition timebase; window remains unchanged
                actual_ns = int(self.streamer.reconfigure_timebase(target_ns))
                self.streamer.start()
                self._update_time_axis(actual_ns)
                self.status_lbl.setText(f"Status: Hardware streaming @ {actual_ns} ns")
            except Exception as e:
                self.status_lbl.setText(f"Status: Rate apply failed — {e}")
        else:
            print("Rate change requested but no hardware is connected.")
            self.status_lbl.setText("Status: No hardware — rate unchanged")

    def _apply_trigger_ui(self) -> None:
        enabled = self.trigger_chk.isChecked()
        # Parse volts from textbox
        text = self.trigger_edit.text().strip()
        try:
            level_v = float(text) if text else 0.0
        except ValueError:
            self.status_lbl.setText("Status: Invalid trigger level; enter a number in volts")
            return
        # Apply to hardware if running; else update config only
        if self.streamer:
            try:
                # Restart streaming to ensure settings take effect cleanly
                was_running = self.streamer._running
                if was_running:
                    self.streamer.stop()
                self.streamer.apply_trigger(enabled, level_v)
                if was_running:
                    self.streamer.start()
                state = "ON" if enabled else "OFF"
                self.status_lbl.setText(f"Status: Trigger {state} @ {level_v:.3f} V (rising)")
                print(f"[UI] Trigger {state} @ {level_v:.6f} V (rising)")
            except Exception as e:
                self.status_lbl.setText(f"Status: Trigger apply failed — {e}")
        else:
            # No hardware connected: update config so it's applied on next open
            self.cfg.simple_trigger_enabled = enabled
            rng = self.cfg.range_a if self.cfg.trigger_source == PS5000A_CHANNEL_A else self.cfg.range_b
            full_scale_v = RANGE_TO_VOLTS.get(rng, 2.0)
            self.cfg.trigger_threshold_pct = float(level_v) / float(full_scale_v) if full_scale_v else 0.0
            state = "ON" if enabled else "OFF"
            print(f"[UI] Trigger {state} (no hardware) @ {level_v:.6f} V (rising)")

    def _update_time_axis(self, sample_ns: int) -> None:
        self.dt = sample_ns * 1e-9
        window_s = self.cfg.plot_window_ms * 1e-3
        self.t = np.linspace(0.0, window_s - self.dt, int(round(window_s / self.dt)), dtype=np.float64)

        # Let plotter handle axis formatting
        self._apply_time_axis_format(window_s)

    def _apply_time_axis_format(self, window_s: float) -> None:
        # Delegate to plotter; it will also keep cursor artists in sync
        self.plotter.apply_time_axis_format(window_s)
        self._refresh_cursor_readouts()

    def _set_window_ms(self, window_ms: float) -> None:
        if self.streamer:
            try:
                actual_ms = float(self.streamer.reconfigure_window_ms(float(window_ms)))
                # Sync spin without recursion
                self.window_spin.blockSignals(True)
                self.window_spin.setValue(actual_ms)
                self.window_spin.blockSignals(False)
                self._update_time_axis(self.streamer.cfg.sample_interval_ns)
                self._apply_time_axis_format(actual_ms * 1e-3)
                self.status_lbl.setText(f"Status: Window {actual_ms:.3f} ms")
            except Exception as e:
                self.status_lbl.setText(f"Status: Window change failed — {e}")
        self.update_plot()

    def _on_timebase_inc(self) -> None:
        # Increase window to next larger step
        cur_ms = float(self.cfg.plot_window_ms)
        cur_s = cur_ms * 1e-3
        steps = self._timebase_steps_s
        # Find next greater-or-equal step
        for s in steps:
            if s > cur_s:
                self._set_window_ms(s * 1e3)
                return
        # At max, clamp to last
        self._set_window_ms(steps[-1] * 1e3)

    def _on_timebase_dec(self) -> None:
        # Decrease window to next smaller step
        cur_ms = float(self.cfg.plot_window_ms)
        cur_s = cur_ms * 1e-3
        steps = self._timebase_steps_s
        prev = steps[0]
        for s in steps:
            if s >= cur_s:
                self._set_window_ms(prev * 1e3)
                return
            prev = s
        # Already smaller than first, clamp to first
        self._set_window_ms(steps[0] * 1e3)

    def _on_window_changed(self, value: float) -> None:
        # User explicitly sets window in ms (supports µs by decimals)
        if self.streamer:
            try:
                actual_ms = float(self.streamer.reconfigure_window_ms(float(value)))
                self._update_time_axis(self.streamer.cfg.sample_interval_ns)
                self._apply_time_axis_format(actual_ms * 1e-3)
                self.status_lbl.setText(f"Status: Window set to {actual_ms:.3f} ms")
            except Exception as e:
                self.status_lbl.setText(f"Status: Window change failed — {e}")
        self.update_plot()


    def _on_cursor_move(self, direction: int) -> None:
        # direction: -1 for left/down, +1 for right/up
        data = self.cursor_select.currentData()
        if not data or not isinstance(data, tuple):
            return
        kind, idx = data  # kind: 'v' or 'h', idx: 0 or 1
        self.plotter.move_cursor(kind, idx, direction)
        self._refresh_cursor_readouts()

    def _format_time(self, seconds: float) -> str:
        # Follow axis convention: <1 ms -> µs, else ms
        if (self.cfg.plot_window_ms * 1e-3) < 1e-3:
            return f"{seconds * 1e6:.3f} µs"
        return f"{seconds * 1e3:.3f} ms"

    def _refresh_cursor_readouts(self) -> None:
        try:
            x1, x2, y1, y2, dx, dy = self.plotter.get_cursor_values()
        except Exception:
            return
        # Time readouts formatted to axis units
        self.lbl_v1.setText(f"X1: {self._format_time(x1)}")
        self.lbl_v2.setText(f"X2: {self._format_time(x2)}")
        self.lbl_dx.setText(f"Δx: {self._format_time(dx)}")
        # Amplitude readouts use normalized units (plot is normalized)
        self.lbl_h1.setText(f"Y1: {y1:+.3f}")
        self.lbl_h2.setText(f"Y2: {y2:+.3f}")
        self.lbl_dy.setText(f"Δy: {dy:.3f}")


    def closeEvent(self, a0) -> None:
        try:
            if self.streamer:
                self.streamer.close()
        finally:
            super().closeEvent(a0)

    def _on_key_move(self, kind: str, direction: int) -> None:
        data = self.cursor_select.currentData()
        if not isinstance(data, tuple):
            return
        sel_kind, idx = data
        if sel_kind != kind:
            return
        self.plotter.move_cursor(kind, idx, direction)
        self._refresh_cursor_readouts()


def picoscope_5000() -> int:
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(picoscope_5000())
