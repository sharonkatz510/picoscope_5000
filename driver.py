from __future__ import annotations

import os
import time
import threading
import ctypes
from dataclasses import dataclass
from ctypes import (
    byref, c_int16, c_int32, c_uint32, c_float, c_double, c_void_p,
    POINTER, WINFUNCTYPE
)
from ctypes.util import find_library

import numpy as np
from picoscope_constants import (
    PS5000A_CHANNEL_A,
    PS5000A_CHANNEL_B,
    PS5000A_AC,
    PS5000A_DC,
    RANGE_TO_VOLTS,
    PS5000A_RATIO_MODE_NONE,
    PS5000A_DR_8BIT,
    PICO_OK,
    PICO_POWER_SUPPLY_CONNECTED,
    PICO_POWER_SUPPLY_NOT_CONNECTED,
    _status_text,
)
from picoscope_driver import PicoSDKError, _check_status  # reuse helpers


def _find_ps5000a_dll() -> str:
    env_path = os.environ.get("PICO_PS5000A_DLL", "").strip()
    if env_path and os.path.isfile(env_path):
        return env_path
    candidates = [
        r"C:\\Program Files\\Pico Technology\\SDK\\lib\\ps5000a.dll",
        r"C:\\Program Files\\Pico Technology\\PicoScope 7 T&M Stable\\ps5000a.dll",
        r"C:\\Program Files\\Pico Technology\\PicoScope 6\\ps5000a.dll",
        r"C:\\Program Files\\Pico Technology\\PicoScope6\\ps5000a.dll",
        r"C:\\Program Files (x86)\\Pico Technology\\SDK\\lib\\ps5000a.dll",
        r"C:\\Program Files (x86)\\Pico Technology\\PicoScope 6\\ps5000a.dll",
        r"C:\\Program Files (x86)\\Pico Technology\\PicoScope6\\ps5000a.dll",
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    lib = find_library("ps5000a")
    if lib:
        return lib
    raise FileNotFoundError(
        "Could not find ps5000a.dll. Install PicoSDK, or set env var PICO_PS5000A_DLL to the full path."
    )


@dataclass
class BlockConfig:
    sample_interval_ns: int = 100  # 10 MHz
    plot_refresh_ms: int = 20
    plot_window_ms: float = 10.0
    plot_max_points: int = 5000 # this is the maximum value applicable in the official PicoScope software for 2 channels at 500MS/s rate

    range_a: int = 0  # set by UI
    range_b: int = 0
    coupling: int = PS5000A_AC
    resolution: int = PS5000A_DR_8BIT

    connect_delay_ms: int = 500
    driver_buffer_size: int = 1_000_000

    simple_trigger_enabled: bool = True
    trigger_source: int = PS5000A_CHANNEL_A
    trigger_threshold_pct: float = 0.0
    trigger_direction: int = 2  # rising


class PicoScopeRapidBlock:
    def __init__(self, cfg: BlockConfig):
        self.cfg = cfg
        self._dll_path = _find_ps5000a_dll()
        try:
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(os.path.dirname(self._dll_path))
        except Exception:
            pass
        self.ps = ctypes.WinDLL(self._dll_path)

        self.handle = c_int16(0)
        self.max_adc = c_int16(32767)

        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

        self._dt_s = self.cfg.sample_interval_ns * 1e-9
        self._n_samples = self.cfg.plot_max_points
        self._window_s = self._n_samples * self._dt_s
        self._buf_a = (c_int16 * self._n_samples)()
        self._buf_b = (c_int16 * self._n_samples)()

        self._y_a = np.zeros(self._n_samples, dtype=np.float32)
        self._y_b = np.zeros(self._n_samples, dtype=np.float32)
        self._t = np.linspace(0.0, self._window_s - self._dt_s, self._n_samples, dtype=np.float64)

        self._bind_functions()
        self._timebase = c_uint32(0)
        self._max_samples_per_segment = c_uint32(0)

    def _bind_functions(self) -> None:
        from ctypes import c_char_p
        self.ps.ps5000aOpenUnit.argtypes = [POINTER(c_int16), c_char_p, c_int32]
        self.ps.ps5000aOpenUnit.restype = c_int32
        self.ps.ps5000aCloseUnit.argtypes = [c_int16]
        self.ps.ps5000aCloseUnit.restype = c_int32
        self.ps.ps5000aSetChannel.argtypes = [c_int16, c_int32, c_int16, c_int32, c_int32, c_float]
        self.ps.ps5000aSetChannel.restype = c_int32
        self.ps.ps5000aSetDataBuffer.argtypes = [c_int16, c_int32, POINTER(c_int16), c_int32, c_uint32, c_int32]
        self.ps.ps5000aSetDataBuffer.restype = c_int32
        self.ps.ps5000aRunBlock.argtypes = [c_int16, c_int32, c_int32, c_uint32, POINTER(c_int32), c_uint32, c_void_p, c_void_p]
        self.ps.ps5000aRunBlock.restype = c_int32
        self.ps.ps5000aIsReady.argtypes = [c_int16, POINTER(c_int16)]
        self.ps.ps5000aIsReady.restype = c_int32
        self.ps.ps5000aGetValues.argtypes = [c_int16, c_uint32, POINTER(c_uint32), c_uint32, c_int32, c_uint32, POINTER(c_int16)]
        self.ps.ps5000aGetValues.restype = c_int32
        self.ps.ps5000aMaximumValue.argtypes = [c_int16, POINTER(c_int16)]
        self.ps.ps5000aMaximumValue.restype = c_int32
        self.ps.ps5000aGetTimebase2.argtypes = [c_int16, c_uint32, c_int32, POINTER(c_float), c_int16, c_uint32]
        self.ps.ps5000aGetTimebase2.restype = c_int32
        self.ps.ps5000aMemorySegments.argtypes = [c_int16, c_uint32, POINTER(c_uint32)]
        self.ps.ps5000aMemorySegments.restype = c_int32
        self.ps.ps5000aSetNoOfCaptures.argtypes = [c_int16, c_uint32]
        self.ps.ps5000aSetNoOfCaptures.restype = c_int32
        self.ps.ps5000aSetSimpleTrigger.argtypes = [c_int16, c_int16, c_int32, c_int16, c_int32, c_int32, c_int32]
        self.ps.ps5000aSetSimpleTrigger.restype = c_int32

    def open(self) -> None:
        from ctypes import c_char_p
        status = self.ps.ps5000aOpenUnit(byref(self.handle), c_char_p(None), c_int32(self.cfg.resolution))
        code = int(status)
        if code in (PICO_POWER_SUPPLY_NOT_CONNECTED, PICO_POWER_SUPPLY_CONNECTED):
            self.ps.ps5000aChangePowerSource(self.handle, c_uint32(code))
        self.ps.ps5000aMaximumValue(self.handle, byref(self.max_adc))
        # Channels
        st = self.ps.ps5000aSetChannel(self.handle, PS5000A_CHANNEL_A, 1, self.cfg.coupling, self.cfg.range_a, c_float(0.0))
        _check_status(st, "ps5000aSetChannel(A)")
        st = self.ps.ps5000aSetChannel(self.handle, PS5000A_CHANNEL_B, 1, self.cfg.coupling, self.cfg.range_b, c_float(0.0))
        _check_status(st, "ps5000aSetChannel(B)")
        # Segments: single segment; fetch max samples per segment and clamp request
        max_samples = c_uint32(0)
        st = self.ps.ps5000aMemorySegments(self.handle, c_uint32(1), byref(max_samples))
        _check_status(st, "ps5000aMemorySegments")
        self._max_samples_per_segment = max_samples
        # Clamp requested samples to device limit
        max_allowed = int(self._max_samples_per_segment.value)
        if self._n_samples > max_allowed:
            self._n_samples = max_allowed
            # Rebuild buffers to clamped size
            self._buf_a = (c_int16 * self._n_samples)()
            self._buf_b = (c_int16 * self._n_samples)()
            self._y_a = np.zeros(self._n_samples, dtype=np.float32)
            self._y_b = np.zeros(self._n_samples, dtype=np.float32)
            self._t = np.linspace(0.0, self._window_s - self._dt_s, self._n_samples, dtype=np.float64)
        time.sleep(self.cfg.connect_delay_ms/1000.0)
        # Trigger setup: autotrigger equals plot refresh to simulate free running
        self.apply_trigger(self.cfg.simple_trigger_enabled, self.cfg.trigger_threshold_pct)
        # Compute timebase for desired dt
        self._timebase = c_uint32(self._find_timebase(self._n_samples))

    def close(self) -> None:
        try:
            self.stop()
        finally:
            try:
                self.ps.ps5000aCloseUnit(self.handle)
            except Exception:
                pass

    def _find_timebase(self, num_samples: int) -> int:
        # Walk timebase indices until interval >= requested dt
        desired_dt = float(self.cfg.sample_interval_ns)
        tb = 0
        tmp_dt = c_float(0.0)
        while tb < 50_000:
            st = self.ps.ps5000aGetTimebase2(self.handle, c_uint32(tb), c_int32(num_samples), byref(tmp_dt), c_int16(0), c_uint32(0))
            if int(st) == PICO_OK and (tmp_dt.value >= desired_dt or tb > 0 and tmp_dt.value > 0):
                self._dt_s = float(tmp_dt.value) * 1e-9
                return tb
            tb += 1
        # Fallback: keep previous timebase
        return tb

    def apply_trigger(self, enabled: bool, threshold_pct: float) -> None:
        self.cfg.simple_trigger_enabled = bool(enabled)
        self.cfg.trigger_threshold_pct = float(threshold_pct)
        ch = self.cfg.trigger_source
        rng = self.cfg.range_a if ch == PS5000A_CHANNEL_A else self.cfg.range_b
        fs_v = float(RANGE_TO_VOLTS.get(rng, 2.0))
        max_adc = float(self.max_adc.value if self.max_adc.value != 0 else 32767.0)
        counts = int(round(threshold_pct * max_adc))
        # Disable autotrigger: require an actual trigger event
        auto_ms = 0
        if enabled:
            st = self.ps.ps5000aSetSimpleTrigger(
                self.handle,
                c_int16(1),
                c_int32(ch),
                c_int16(counts),
                c_int32(self.cfg.trigger_direction),
                c_int32(0),
                c_int32(auto_ms),
            )
            _check_status(st, "ps5000aSetSimpleTrigger(enable)")
        else:
            # Disable trigger; set autotrigger to minimal to free-run
            st = self.ps.ps5000aSetSimpleTrigger(
                self.handle,
                c_int16(0),
                c_int32(ch),
                c_int16(0),
                c_int32(self.cfg.trigger_direction),
                c_int32(0),
                c_int32(0),
            )
            _check_status(st, "ps5000aSetSimpleTrigger(disable)")

    def set_range(self, channel: int, new_range: int) -> None:
        st = self.ps.ps5000aSetChannel(self.handle, channel, 1, self.cfg.coupling, new_range, c_float(0.0))
        _check_status(st, f"ps5000aSetChannel({'A' if channel==PS5000A_CHANNEL_A else 'B'})")
        if channel == PS5000A_CHANNEL_A:
            self.cfg.range_a = new_range
        else:
            self.cfg.range_b = new_range

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        def _loop():
            while self._running:
                try:
                    # Prepare buffers for this capture
                    st = self.ps.ps5000aSetDataBuffer(self.handle, PS5000A_CHANNEL_A, self._buf_a, self._n_samples, 0, PS5000A_RATIO_MODE_NONE)
                    _check_status(st, "ps5000aSetDataBuffer(A)")
                    st = self.ps.ps5000aSetDataBuffer(self.handle, PS5000A_CHANNEL_B, self._buf_b, self._n_samples, 0, PS5000A_RATIO_MODE_NONE)
                    _check_status(st, "ps5000aSetDataBuffer(B)")
                    # Run block capture: pre=0, post=n
                    time_indisposed = c_int32(0)
                    st = self.ps.ps5000aRunBlock(self.handle, c_int32(0), c_int32(self._n_samples), self._timebase, byref(time_indisposed), c_uint32(0), None, None)
                    if int(st) != PICO_OK:
                        # Fatal: don't retry; break loop and close device
                        try:
                            msg = _status_text(int(st))
                        except Exception:
                            msg = str(int(st))
                        print(f"[PicoSDK][ERROR] ps5000aRunBlock failed: {msg}")
                        self._running = False
                        # Close device from thread without joining self
                        try:
                            self.ps.ps5000aCloseUnit(self.handle)
                        except Exception:
                            pass
                        break
                    # Poll readiness
                    ready = c_int16(0)
                    while self._running and int(ready.value) == 0:
                        self.ps.ps5000aIsReady(self.handle, byref(ready))
                        # Avoid busy spin
                        time.sleep(0.001)
                    if not self._running:
                        break
                    # Retrieve data
                    n_samps = c_uint32(self._n_samples)
                    overflow = c_int16(0)
                    st = self.ps.ps5000aGetValues(self.handle, c_uint32(0), byref(n_samps), c_uint32(1), c_int32(0), c_uint32(0), byref(overflow))
                    _check_status(st, "ps5000aGetValues")

                    cnt = int(n_samps.value)
                    if cnt <= 0:
                        continue
                    max_adc = float(self.max_adc.value if self.max_adc.value != 0 else 32767)
                    scale_a = RANGE_TO_VOLTS.get(self.cfg.range_a, 2.0) / max_adc
                    scale_b = RANGE_TO_VOLTS.get(self.cfg.range_b, 2.0) / max_adc
                    with self._lock:
                        a = np.frombuffer(self._buf_a, dtype=np.int16, count=cnt, offset=0) * scale_a
                        b = np.frombuffer(self._buf_b, dtype=np.int16, count=cnt, offset=0) * scale_b
                        # Resize time axis if needed
                        if cnt != len(self._t):
                            self._t = np.linspace(0.0, self._window_s - self._dt_s, cnt, dtype=np.float64)
                        self._y_a = a.astype(np.float32, copy=True)
                        self._y_b = b.astype(np.float32, copy=True)
                except Exception:
                    # Keep loop alive; next iteration will retry
                    pass
                # Pace captures roughly to UI refresh; autotrigger also enforces cadence
                time.sleep(max(0.0, self.cfg.plot_refresh_ms/1000.0 - 0.001))

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            # No explicit stop for block; ensure thread stops
            pass
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self._thread = None

    def reconfigure_timebase(self, new_sample_interval_ns: int) -> int:
        self.cfg.sample_interval_ns = int(max(1, new_sample_interval_ns))
        self._dt_s = self.cfg.sample_interval_ns * 1e-9
        self._window_s = self.cfg.plot_window_ms * 1e-3
        self._n_samples = int(max(10, round(self._window_s / self._dt_s)))
        # Clamp to max samples per segment if known
        max_allowed = int(self._max_samples_per_segment.value) if self._max_samples_per_segment.value else None
        if max_allowed is not None and self._n_samples > max_allowed:
            self._n_samples = max_allowed
        with self._lock:
            self._buf_a = (c_int16 * self._n_samples)()
            self._buf_b = (c_int16 * self._n_samples)()
            self._y_a = np.zeros(self._n_samples, dtype=np.float32)
            self._y_b = np.zeros(self._n_samples, dtype=np.float32)
            self._t = np.linspace(0.0, self._window_s - self._dt_s, self._n_samples, dtype=np.float64)
        # Recompute timebase for new dt
        self._timebase = c_uint32(self._find_timebase(self._n_samples))
        return int(self.cfg.sample_interval_ns)

    def reconfigure_window_ms(self, new_window_ms: float) -> float:
        self.cfg.plot_window_ms = float(max(0.01, new_window_ms))
        self._window_s = self.cfg.plot_window_ms * 1e-3
        self._n_samples = int(max(10, round(self._window_s / self._dt_s)))
        # Clamp to max samples per segment if known
        max_allowed = int(self._max_samples_per_segment.value) if self._max_samples_per_segment.value else None
        if max_allowed is not None and self._n_samples > max_allowed:
            self._n_samples = max_allowed
        with self._lock:
            self._buf_a = (c_int16 * self._n_samples)()
            self._buf_b = (c_int16 * self._n_samples)()
            self._y_a = np.zeros(self._n_samples, dtype=np.float32)
            self._y_b = np.zeros(self._n_samples, dtype=np.float32)
            self._t = np.linspace(0.0, self._window_s - self._dt_s, self._n_samples, dtype=np.float64)
        # Update timebase samples depth
        self._timebase = c_uint32(self._find_timebase(self._n_samples))
        return float(self.cfg.plot_window_ms)
