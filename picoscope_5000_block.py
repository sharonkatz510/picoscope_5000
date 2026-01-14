"""
PicoScope 5000B rapid block viewer for Channels A and B.
- Targets 10 MHz sampling (100 ns) default
- Uses autotrigger ~= plot refresh to simulate free-running
"""
from __future__ import annotations

import sys
import numpy as np

from PyQt5 import QtCore, QtWidgets, QtGui
import os
import datetime
from plotter import PlotterWidget
from picoscope_constants import (
    RANGE_CODES,
    RANGE_LABELS,
    RANGE_TO_VOLTS,
    PS5000A_CHANNEL_A,
    PS5000A_CHANNEL_B,
)
from picoscope_driver_block import (
    BlockConfig as DriverBlockConfig,
    PicoScopeRapidBlock as DriverPicoScopeRapidBlock,
)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PicoScope 5000B Rapid Block Viewer")

        self.cfg = DriverBlockConfig()
        # Default ranges to 2V like streaming app
        try:
            # crude: find label '2 V'
            for code, label in RANGE_LABELS.items():
                if label == '±2 V' or label.endswith('2 V'):
                    self.cfg.range_a = code
                    self.cfg.range_b = code
                    break
        except Exception:
            pass

        central = QtWidgets.QWidget()
        vbox = QtWidgets.QVBoxLayout(central)
        ctrl = QtWidgets.QWidget()
        hbox = QtWidgets.QHBoxLayout(ctrl)

        self.status_lbl = QtWidgets.QLabel("Status: Initializing…")
        hbox.addWidget(self.status_lbl)

        hbox.addSpacing(20)
        self.a_range_lbl = QtWidgets.QLabel(f"A Range: {RANGE_LABELS.get(self.cfg.range_a, '')}")
        self.a_range_combo = QtWidgets.QComboBox()
        for code in RANGE_CODES:
            self.a_range_combo.addItem(RANGE_LABELS[code], userData=code)
        # Set combo to current config
        try:
            a_idx = RANGE_CODES.index(self.cfg.range_a)
            self.a_range_combo.setCurrentIndex(a_idx)
        except Exception:
            pass
        self.a_range_combo.currentIndexChanged.connect(self._on_a_range_changed)
        hbox.addWidget(self.a_range_lbl)
        hbox.addWidget(self.a_range_combo)

        hbox.addSpacing(10)
        self.b_range_lbl = QtWidgets.QLabel(f"B Range: {RANGE_LABELS.get(self.cfg.range_b, '')}")
        self.b_range_combo = QtWidgets.QComboBox()
        for code in RANGE_CODES:
            self.b_range_combo.addItem(RANGE_LABELS[code], userData=code)
        try:
            b_idx = RANGE_CODES.index(self.cfg.range_b)
            self.b_range_combo.setCurrentIndex(b_idx)
        except Exception:
            pass
        self.b_range_combo.currentIndexChanged.connect(self._on_b_range_changed)
        hbox.addWidget(self.b_range_lbl)
        hbox.addWidget(self.b_range_combo)

        hbox.addSpacing(20)
        self.rate_lbl = QtWidgets.QLabel("Rate:")
        self.rate_combo = QtWidgets.QComboBox()
        self.rate_combo.addItems([
            "100 ns", "200 ns", "500 ns", "1 us", "2 us", "5 us"
        ])
        self.rate_combo.setCurrentIndex(0)  # 100 ns default
        self.apply_rate_btn = QtWidgets.QPushButton("Apply Rate")
        self.apply_rate_btn.clicked.connect(self._apply_rate)
        hbox.addWidget(self.rate_lbl)
        hbox.addWidget(self.rate_combo)
        hbox.addWidget(self.apply_rate_btn)

        # Trigger: level control only (no autotrigger, always armed)
        # Default trigger level to 0 V
        hbox.addSpacing(20)
        init_rng = self.cfg.range_a if self.cfg.trigger_source == PS5000A_CHANNEL_A else self.cfg.range_b
        self._trigger_level_v = 0.0

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

        hbox.addSpacing(20)
        self.window_lbl = QtWidgets.QLabel("Window (ms):")
        hbox.addWidget(self.window_lbl)
        self.window_spin = QtWidgets.QDoubleSpinBox()
        self.window_spin.setDecimals(3)
        self.window_spin.setMinimum(0.010)
        self.window_spin.setMaximum(50.000)
        self.window_spin.setSingleStep(0.010)
        self.window_spin.setValue(float(self.cfg.plot_window_ms))
        self.window_spin.valueChanged.connect(self._on_window_changed)
        hbox.addWidget(self.window_spin)

        # Cursor controls (2 vertical + 2 horizontal) and UI
        hbox.addSpacing(20)
        hbox.addWidget(QtWidgets.QLabel("Cursor:"))
        self.cursor_select = QtWidgets.QComboBox()
        self.cursor_select.addItem("X1", userData=("v", 0))
        self.cursor_select.addItem("X2", userData=("v", 1))
        self.cursor_select.addItem("Y1", userData=("h", 0))
        self.cursor_select.addItem("Y2", userData=("h", 1))
        self.cursor_select.addItem("Trigger", userData=("t", 0))
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

        # Recording controls
        hbox.addSpacing(20)
        self.rec_folder_btn = QtWidgets.QPushButton("Choose Location")
        self.rec_start_btn = QtWidgets.QPushButton("Start Rec")
        self.rec_stop_btn = QtWidgets.QPushButton("Stop Rec")
        self.rec_status_lbl = QtWidgets.QLabel("Save: none")
        self.rec_status_lbl.setToolTip("Selected folder for recordings")
        self.rec_folder_btn.clicked.connect(self._on_choose_rec_folder)
        self.rec_start_btn.clicked.connect(self._on_start_rec)
        self.rec_stop_btn.clicked.connect(self._on_stop_rec)
        self.rec_stop_btn.setEnabled(False)
        hbox.addWidget(self.rec_folder_btn)
        hbox.addWidget(self.rec_start_btn)
        hbox.addWidget(self.rec_stop_btn)
        hbox.addWidget(self.rec_status_lbl)

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
        side_v.addSpacing(8)
        # Trigger readout
        side_v.addWidget(QtWidgets.QLabel("Trigger (V):"))
        self.lbl_trig = QtWidgets.QLabel("—")
        side_v.addWidget(self.lbl_trig)
        side_v.addStretch(1)

        content_h.addWidget(self.sidebar, 0)
        vbox.addWidget(content, 1)
        self.setCentralWidget(central)

        # Overlay message shown during recording
        self.rec_overlay_lbl = QtWidgets.QLabel("Recording in progress", self.plotter.canvas)
        self.rec_overlay_lbl.setAlignment(QtCore.Qt.AlignCenter)
        self.rec_overlay_lbl.setStyleSheet(
            "background-color: rgba(0, 0, 0, 120); color: white; padding: 6px;"
        )
        self.rec_overlay_lbl.setVisible(False)

        self.block: DriverPicoScopeRapidBlock | None = None

        try:
            self.block = DriverPicoScopeRapidBlock(self.cfg)
            self.block.open()
            self.block.start()
            actual_ns = self.block.cfg.sample_interval_ns
            self.status_lbl.setText(f"Status: Rapid Block @ {actual_ns} ns")
            # Apply initial trigger indicator line based on current level and range (0 V by default)
            rng = self.block.cfg.range_a if self.block.cfg.trigger_source == PS5000A_CHANNEL_A else self.block.cfg.range_b
            fs_v = RANGE_TO_VOLTS.get(rng, 1.0)
            norm_y = (self._trigger_level_v / fs_v) if fs_v else 0.0
            self.plotter.set_trigger_level_norm(norm_y)
            # Apply trigger to hardware (enabled, no autotrigger) at 0 V
            try:
                self.block.stop()
                threshold_pct = (self._trigger_level_v / fs_v) if fs_v else 0.0
                self.block.apply_trigger(True, threshold_pct)
                self.block.start()
                self.status_lbl.setText(f"Status: Rapid Block @ {actual_ns} ns — Trigger ON @ {self._trigger_level_v:.3f} V")
            except Exception:
                pass
            self._apply_time_axis_format(self.cfg.plot_window_ms * 1e-3)
        except Exception as e:
            self.block = None
            self.status_lbl.setText(f"Status: Error — {e}")

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(self.cfg.plot_refresh_ms)
        self.timer.timeout.connect(self.update_plot)
        self.timer.start()

        self.resize(900, 500)
        # Keyboard shortcuts for cursor movement
        self._short_left = QtWidgets.QShortcut(QtGui.QKeySequence('Left'), self)
        self._short_left.activated.connect(lambda: self._on_key_move('v', -1))
        self._short_right = QtWidgets.QShortcut(QtGui.QKeySequence('Right'), self)
        self._short_right.activated.connect(lambda: self._on_key_move('v', 1))
        self._short_up = QtWidgets.QShortcut(QtGui.QKeySequence('Up'), self)
        self._short_up.activated.connect(lambda: self._on_key_move('h', 1))
        self._short_down = QtWidgets.QShortcut(QtGui.QKeySequence('Down'), self)
        self._short_down.activated.connect(lambda: self._on_key_move('h', -1))
        # Base rate tracking & timebase steps
        self._rate_base_ns: int = int(self.cfg.sample_interval_ns)
        self._timebase_steps_s = [
            10e-6, 20e-6, 50e-6,
            100e-6, 200e-6, 500e-6,
            1e-3, 2e-3, 5e-3, 10e-3,
        ]
        self._refresh_cursor_readouts()
        # Recording state
        self._rec_dir: str | None = None
        self._rec_on: bool = False
        self._rec_count: int = 0
        self._rec_started_at: datetime.datetime | None = None
        self._rec_meta: dict[str, object] = {}

    def update_plot(self) -> None:
        if self.block and self.block._running:
            with self.block._lock:
                ya = self.block._y_a.copy()
                yb = self.block._y_b.copy()
                tt = self.block._t.copy()
            if not self._rec_on:
                # Normal UI refresh when not recording
                fs_a = RANGE_TO_VOLTS.get(self.block.cfg.range_a, 1.0)
                fs_b = RANGE_TO_VOLTS.get(self.block.cfg.range_b, 1.0)
                ya_n = ya / fs_a if fs_a else ya
                yb_n = yb / fs_b if fs_b else yb
                self.plotter.update_series(tt, ya_n, yb_n, self.cfg.plot_max_points)
                self._refresh_cursor_readouts()
                # Hide any overlay if previously shown
                if self.rec_overlay_lbl.isVisible():
                    self.rec_overlay_lbl.setVisible(False)
            else:
                # Recording mode: do not refresh plot, show overlay message
                self.rec_overlay_lbl.setGeometry(self.plotter.canvas.rect())
                if not self.rec_overlay_lbl.isVisible():
                    self.rec_overlay_lbl.setVisible(True)
            # Save current acquisition to disk if recording is active
            try:
                if self._rec_on and self._rec_dir and len(ya) and len(yb):
                    self._rec_count += 1
                    fname = f"acq_{self._rec_count:03d}.bin"
                    fpath = QtCore.QDir(self._rec_dir).filePath(fname)
                    # Write A then B as float16 raw binary
                    with open(fpath, "wb") as f:
                        ya.astype(np.float16, copy=False).tofile(f)
                        yb.astype(np.float16, copy=False).tofile(f)
            except Exception as e:
                # Non-fatal: update status and keep UI responsive
                self.status_lbl.setText(f"Status: Save failed — {e}")

    def _on_a_range_changed(self, idx: int) -> None:
        code = self.a_range_combo.itemData(idx)
        if code is None or self.block is None:
            return
        try:
            self.block.stop()
            self.block.set_range(PS5000A_CHANNEL_A, int(code))
            self.block.start()
            self.a_range_lbl.setText(f"A Range: {RANGE_LABELS[int(code)]}")
        except Exception as e:
            self.status_lbl.setText(f"Status: Range change failed — {e}")

    def _on_b_range_changed(self, idx: int) -> None:
        code = self.b_range_combo.itemData(idx)
        if code is None or self.block is None:
            return
        try:
            self.block.stop()
            self.block.set_range(PS5000A_CHANNEL_B, int(code))
            self.block.start()
            self.b_range_lbl.setText(f"B Range: {RANGE_LABELS[int(code)]}")
        except Exception as e:
            self.status_lbl.setText(f"Status: Range change failed — {e}")

    def _apply_rate(self) -> None:
        if not self.block:
            return
        text = self.rate_combo.currentText()
        mapping = {
            "100 ns": 100,
            "200 ns": 200,
            "500 ns": 500,
            "1 us": 1000,
            "2 us": 2000,
            "5 us": 5000,
        }
        target_ns = mapping.get(text, 100)
        try:
            self.block.stop()
            actual_ns = int(self.block.reconfigure_timebase(target_ns))
            self.block.start()
            self.status_lbl.setText(f"Status: Rapid Block @ {actual_ns} ns")
            self._apply_time_axis_format(self.cfg.plot_window_ms * 1e-3)
        except Exception as e:
            self.status_lbl.setText(f"Status: Rate apply failed — {e}")

    def _on_window_changed(self, value: float) -> None:
        if not self.block:
            return
        try:
            self.block.stop()
            actual_ms = float(self.block.reconfigure_window_ms(float(value)))
            self.block.start()
            self._apply_time_axis_format(actual_ms * 1e-3)
            self.status_lbl.setText(f"Status: Window {actual_ms:.3f} ms")
        except Exception as e:
            self.status_lbl.setText(f"Status: Window change failed — {e}")

    # No trigger checkbox; trigger is always enabled, controlled by level

    def _apply_time_axis_format(self, window_s: float) -> None:
        self.plotter.apply_time_axis_format(window_s)
        self._refresh_cursor_readouts()

    def _on_timebase_inc(self) -> None:
        # Increase window to next larger step
        cur_ms = float(self.cfg.plot_window_ms)
        cur_s = cur_ms * 1e-3
        steps = self._timebase_steps_s
        for s in steps:
            if s > cur_s:
                self._set_window_ms(s * 1e3)
                return
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
        self._set_window_ms(steps[0] * 1e3)

    def _set_window_ms(self, window_ms: float) -> None:
        if not self.block:
            return
        try:
            self.block.stop()
            actual_ms = float(self.block.reconfigure_window_ms(float(window_ms)))
            # Sync spin without recursion
            self.window_spin.blockSignals(True)
            self.window_spin.setValue(actual_ms)
            self.window_spin.blockSignals(False)
            self.block.start()
            self._apply_time_axis_format(actual_ms * 1e-3)
            self.status_lbl.setText(f"Status: Window {actual_ms:.3f} ms")
        except Exception as e:
            self.status_lbl.setText(f"Status: Window change failed — {e}")

    def _on_cursor_move(self, direction: int) -> None:
        data = self.cursor_select.currentData()
        if not data or not isinstance(data, tuple):
            return
        kind, idx = data
        if kind in ('v', 'h'):
            self.plotter.move_cursor(kind, idx, direction)
            self._refresh_cursor_readouts()
        elif kind == 't':
            self._nudge_trigger(direction)

    def _format_time(self, seconds: float) -> str:
        if (self.cfg.plot_window_ms * 1e-3) < 1e-3:
            return f"{seconds * 1e6:.3f} µs"
        return f"{seconds * 1e3:.3f} ms"

    def _refresh_cursor_readouts(self) -> None:
        try:
            x1, x2, y1, y2, dx, dy = self.plotter.get_cursor_values()
        except Exception:
            return
        self.lbl_v1.setText(f"X1: {self._format_time(x1)}")
        self.lbl_v2.setText(f"X2: {self._format_time(x2)}")
        self.lbl_dx.setText(f"Δx: {self._format_time(dx)}")
        self.lbl_h1.setText(f"Y1: {y1:+.3f}")
        self.lbl_h2.setText(f"Y2: {y2:+.3f}")
        self.lbl_dy.setText(f"Δy: {dy:.3f}")

    def _on_key_move(self, kind: str, direction: int) -> None:
        data = self.cursor_select.currentData()
        if not isinstance(data, tuple):
            return
        sel_kind, idx = data
        if sel_kind != kind:
            return
        if kind == 'h' and sel_kind == 't':
            self._nudge_trigger(direction)
            return
        self.plotter.move_cursor(kind, idx, direction)
        self._refresh_cursor_readouts()

    def _nudge_trigger(self, direction: int) -> None:
        try:
            self.plotter.move_trigger(direction)
            norm_y = self.plotter.get_trigger_level_norm()
            # Convert to volts based on trigger source channel range
            if self.block:
                trig_ch = self.block.cfg.trigger_source
                rng = self.block.cfg.range_a if trig_ch == PS5000A_CHANNEL_A else self.block.cfg.range_b
            else:
                trig_ch = self.cfg.trigger_source
                rng = self.cfg.range_a if trig_ch == PS5000A_CHANNEL_A else self.cfg.range_b
            fs_v = RANGE_TO_VOLTS.get(rng, 1.0)
            level_v = float(norm_y) * float(fs_v)
            self._trigger_level_v = float(level_v)
            self._refresh_trigger_readout()
            if self.block:
                was_running = self.block._running
                if was_running:
                    self.block.stop()
                threshold_pct = (level_v / fs_v) if fs_v else 0.0
                self.block.apply_trigger(True, threshold_pct)
                if was_running:
                    self.block.start()
            else:
                self.cfg.trigger_threshold_pct = (level_v / fs_v) if fs_v else 0.0
        except Exception as e:
            self.status_lbl.setText(f"Status: Trigger move failed — {e}")

    def _refresh_trigger_readout(self) -> None:
        try:
            self.lbl_trig.setText(f"{self._trigger_level_v:+.3f} V")
        except Exception:
            pass

    def closeEvent(self, a0) -> None:
        try:
            if self.block:
                self.block.close()
        finally:
            super().closeEvent(a0)

    # ----- Recording controls -----
    def _on_choose_rec_folder(self) -> None:
        start_dir = self._rec_dir or QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.DesktopLocation)
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose recording folder", start_dir)
        if folder:
            self._rec_dir = folder
            self.rec_status_lbl.setText(f"Save: {QtCore.QFileInfo(folder).fileName()}")
            self.rec_status_lbl.setToolTip(folder)

    def _on_start_rec(self) -> None:
        if not self._rec_dir:
            self._on_choose_rec_folder()
            if not self._rec_dir:
                return
        self._rec_on = True
        self._rec_count = 0
        # Capture start time and session metadata
        self._rec_started_at = datetime.datetime.now()
        # Sampling frequency (Hz)
        if self.block:
            si_ns = float(self.block.cfg.sample_interval_ns)
        else:
            si_ns = float(self.cfg.sample_interval_ns)
        samp_hz = 1e9 / si_ns if si_ns > 0 else 0.0
        # Frame rate tied to UI update interval (Hz)
        fr_ms = float(self.cfg.plot_refresh_ms)
        frame_hz = 1000.0 / fr_ms if fr_ms > 0 else 0.0
        self._rec_meta = {
            "sampling_frequency_hz": samp_hz,
            "frame_rate_hz": frame_hz,
            "started_at": self._rec_started_at.isoformat(timespec="seconds"),
        }
        self.rec_start_btn.setEnabled(False)
        self.rec_stop_btn.setEnabled(True)
        self.rec_folder_btn.setEnabled(False)
        self.status_lbl.setText(f"Status: Recording → {self._rec_dir}")

    def _on_stop_rec(self) -> None:
        self._rec_on = False
        self.rec_start_btn.setEnabled(True)
        self.rec_stop_btn.setEnabled(False)
        self.rec_folder_btn.setEnabled(True)
        # Write metadata file summarizing the session
        try:
            if self._rec_dir:
                meta_path = os.path.join(self._rec_dir, "metadata.txt")
                lines = []
                started = self._rec_meta.get("started_at", "")
                samp_hz = float(self._rec_meta.get("sampling_frequency_hz", 0.0))
                frame_hz = float(self._rec_meta.get("frame_rate_hz", 0.0))
                lines.append(f"started_at: {started}\n")
                lines.append(f"sampling_frequency_hz: {samp_hz:.6f}\n")
                lines.append(f"frame_rate_hz: {frame_hz:.3f}\n")
                lines.append(f"acquisitions_saved: {self._rec_count}\n")
                with open(meta_path, "w", encoding="utf-8") as mf:
                    mf.writelines(lines)
        except Exception as e:
            self.status_lbl.setText(f"Status: Metadata write failed — {e}")
        self.status_lbl.setText("Status: Recording stopped")


def picoscope_5000_block() -> int:
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(picoscope_5000_block())
