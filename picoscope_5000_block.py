"""
PicoScope 5000B rapid block viewer for Channels A and B.
- Targets 10 MHz sampling (100 ns) default
- Uses autotrigger ~= plot refresh to simulate free-running
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
            from picoscope_constants import RANGE_LABELS
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
        self.a_range_lbl = QtWidgets.QLabel("")
        self.a_range_combo = QtWidgets.QComboBox()
        for code in RANGE_CODES:
            self.a_range_combo.addItem(RANGE_LABELS[code], userData=code)
        self.a_range_combo.currentIndexChanged.connect(self._on_a_range_changed)
        hbox.addWidget(QtWidgets.QLabel("A Range:"))
        hbox.addWidget(self.a_range_combo)

        hbox.addSpacing(10)
        self.b_range_combo = QtWidgets.QComboBox()
        for code in RANGE_CODES:
            self.b_range_combo.addItem(RANGE_LABELS[code], userData=code)
        self.b_range_combo.currentIndexChanged.connect(self._on_b_range_changed)
        hbox.addWidget(QtWidgets.QLabel("B Range:"))
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

        # Autotrigger-based free running: expose trigger checkbox
        hbox.addSpacing(20)
        self.trigger_chk = QtWidgets.QCheckBox("Trigger (auto)")
        self.trigger_chk.setChecked(True)
        self.trigger_chk.toggled.connect(self._apply_trigger_ui)
        hbox.addWidget(self.trigger_chk)

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

        vbox.addWidget(ctrl)

        self.plotter = PlotterWidget(self)
        vbox.addWidget(self.plotter, 1)
        self.setCentralWidget(central)

        self.block: DriverPicoScopeRapidBlock | None = None

        try:
            self.block = DriverPicoScopeRapidBlock(self.cfg)
            self.block.open()
            self.block.start()
            actual_ns = self.block.cfg.sample_interval_ns
            self.status_lbl.setText(f"Status: Rapid Block @ {actual_ns} ns")
            self._apply_time_axis_format(self.cfg.plot_window_ms * 1e-3)
        except Exception as e:
            self.block = None
            self.status_lbl.setText(f"Status: Error — {e}")

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(self.cfg.plot_refresh_ms)
        self.timer.timeout.connect(self.update_plot)
        self.timer.start()

        self.resize(900, 500)

    def update_plot(self) -> None:
        if self.block and self.block._running:
            with self.block._lock:
                ya = self.block._y_a.copy()
                yb = self.block._y_b.copy()
                tt = self.block._t.copy()
            fs_a = RANGE_TO_VOLTS.get(self.block.cfg.range_a, 1.0)
            fs_b = RANGE_TO_VOLTS.get(self.block.cfg.range_b, 1.0)
            ya_n = ya / fs_a if fs_a else ya
            yb_n = yb / fs_b if fs_b else yb
            self.plotter.update_series(tt, ya_n, yb_n, self.cfg.plot_max_points)

    def _on_a_range_changed(self, idx: int) -> None:
        code = self.a_range_combo.itemData(idx)
        if code is None or self.block is None:
            return
        try:
            self.block.stop()
            self.block.set_range(PS5000A_CHANNEL_A, int(code))
            self.block.start()
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

    def _apply_trigger_ui(self) -> None:
        if not self.block:
            return
        enabled = self.trigger_chk.isChecked()
        try:
            self.block.stop()
            self.block.apply_trigger(enabled, self.block.cfg.trigger_threshold_pct)
            self.block.start()
            state = "ON" if enabled else "OFF"
            self.status_lbl.setText(f"Status: Trigger {state} (auto {self.cfg.plot_refresh_ms} ms)")
        except Exception as e:
            self.status_lbl.setText(f"Status: Trigger apply failed — {e}")

    def _apply_time_axis_format(self, window_s: float) -> None:
        self.plotter.apply_time_axis_format(window_s)

    def closeEvent(self, a0) -> None:
        try:
            if self.block:
                self.block.close()
        finally:
            super().closeEvent(a0)


def picoscope_5000_block() -> int:
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(picoscope_5000_block())
