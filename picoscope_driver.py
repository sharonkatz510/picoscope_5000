from __future__ import annotations

import os
import time
import threading
import ctypes
import json
from dataclasses import dataclass
from ctypes import (
    byref, c_int16, c_int32, c_uint32, c_float, c_double, c_int8, c_void_p,
    POINTER, WINFUNCTYPE
)
from ctypes.util import find_library

import numpy as np
from picoscope_constants import (
    PS5000A_CHANNEL_A,
    PS5000A_CHANNEL_B,
    PS5000A_AC,
    PS5000A_DC,
    PS5000A_10MV,
    PS5000A_20MV,
    PS5000A_50MV,
    PS5000A_100MV,
    PS5000A_200MV,
    PS5000A_500MV,
    PS5000A_1V,
    PS5000A_2V,
    PS5000A_5V,
    PS5000A_10V,
    PS5000A_20V,
    RANGE_TO_VOLTS,
    RANGE_CODES,
    RANGE_LABELS,
    PS5000A_FS,
    PS5000A_PS,
    PS5000A_NS,
    PS5000A_US,
    PS5000A_MS,
    PS5000A_S,
    PS5000A_RATIO_MODE_NONE,
    PS5000A_RATIO_MODE_AGGREGATE,
    PS5000A_RATIO_MODE_DECIMATE,
    PS5000A_RATIO_MODE_AVERAGE,
    PS5000A_DR_8BIT,
    PS5000A_DR_12BIT,
    PS5000A_DR_14BIT,
    PS5000A_DR_15BIT,
    PS5000A_DR_16BIT,
    PICO_OK,
    PICO_POWER_SUPPLY_CONNECTED,
    PICO_POWER_SUPPLY_NOT_CONNECTED,
    PICO_INVALID_HANDLE,
    PICO_INVALID_PARAMETER,
    PICO_INVALID_TIMEBASE,
    _status_text,
)


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


class PicoSDKError(RuntimeError):
    pass


def _check_status(status: int, where: str) -> None:
    if int(status) != PICO_OK:
        raise PicoSDKError(f"{where} failed: {_status_text(status)}")


@dataclass
class StreamConfig:
    sample_interval_ns: int = 1000 # 1 µs between acquisitions
    plot_refresh_ms: int = 20   # Refresh plot every 20 ms
    plot_window_ms: float = 20.0    # Initial plot window size in ms
    plot_max_points: int = 6000   # Max points to plot

    range_a: int = PS5000A_2V   # Channel A initial range - 2 V
    range_b: int = PS5000A_2V   # Channel B initial range - 2 V
    coupling: int = PS5000A_AC   # AC coupling
    resolution: int = PS5000A_DR_8BIT   # 8-bit resolution

    driver_buffer_size: int = 200_000   # Driver buffer size - memory pre-allocated
    connect_delay_ms: int = 1000   # Delay from connecting to starting acquisition in ms
    simple_trigger_enabled: bool = False    # Simple trigger enabled
    trigger_source: int = PS5000A_CHANNEL_A  # Trigger source channel - Channel A
    trigger_threshold_pct: float = 0.1  # Trigger threshold as fraction of full scale (e.g., 0.1 = 10%)
    trigger_direction: int = 2  # Rising edge trigger


class PicoScopeStreamer:
    def __init__(self, cfg: StreamConfig):
        self.cfg = cfg
        self._dll_path = _find_ps5000a_dll()
        self._dll_dir_ctx = None
        try:
            if hasattr(os, "add_dll_directory"):
                self._dll_dir_ctx = os.add_dll_directory(os.path.dirname(self._dll_path))
                candidate_dirs = [
                    r"C:\\Program Files\\Pico Technology\\SDK\\lib",
                    r"C:\\Program Files\\Pico Technology\\SDK\\lib\\win64",
                    r"C:\\Program Files\\Pico Technology\\SDK\\lib\\win32",
                    r"C:\\Program Files (x86)\\Pico Technology\\SDK\\lib",
                    r"C:\\Program Files (x86)\\Pico Technology\\SDK\\lib\\win64",
                    r"C:\\Program Files (x86)\\Pico Technology\\SDK\\lib\\win32",
                ]
                for d in candidate_dirs:
                    if os.path.isdir(d):
                        os.add_dll_directory(d)
        except Exception:
            pass
        try:
            sdk_dir = os.path.dirname(self._dll_path)
            picoipp_path = os.path.join(sdk_dir, "picoipp.dll")
            if os.path.isfile(picoipp_path):
                ctypes.WinDLL(picoipp_path)
        except Exception:
            pass
        self.ps = ctypes.WinDLL(self._dll_path)

        self.handle = c_int16(0)
        self.max_adc = c_int16(0)

        self._buf_a = (c_int16 * cfg.driver_buffer_size)()
        self._buf_b = (c_int16 * cfg.driver_buffer_size)()

        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

        self._dt_s = cfg.sample_interval_ns * 1e-9
        self._ring_len = int(round((cfg.plot_window_ms * 1e-3) / self._dt_s))
        if self._ring_len < 10:
            self._ring_len = 10

        self._y_a = np.zeros(self._ring_len, dtype=np.float32)
        self._y_b = np.zeros(self._ring_len, dtype=np.float32)
        self._write_idx = 0

        window_s = cfg.plot_window_ms * 1e-3
        self._t = np.linspace(0.0, window_s - self._dt_s, self._ring_len, dtype=np.float64)

        self._block_ready_type = WINFUNCTYPE(None, c_int16, c_uint32, c_void_p)
        self._block_ready_cb = self._block_ready_type(self._on_block_ready)
        self._streaming_ready_type = WINFUNCTYPE(None, c_int16, c_int32, c_uint32, c_int16, c_uint32, c_int16, c_int16, c_void_p)
        self._streaming_cb = self._streaming_ready_type(self._on_streaming_ready)
        self._ready_evt = threading.Event()

        self._bind_functions()
        # Trigger gating state for streaming: when enabled, suppress updates until first trigger
        self._seen_trigger: bool = False

    def apply_trigger(self, enabled: bool, threshold_volts: float) -> None:
        self.cfg.simple_trigger_enabled = bool(enabled)
        ch = self.cfg.trigger_source
        rng = self.cfg.range_a if ch == PS5000A_CHANNEL_A else self.cfg.range_b
        full_scale_v = RANGE_TO_VOLTS.get(rng, 2.0)
        max_adc = float(self.max_adc.value if self.max_adc.value != 0 else 32767)
        self.cfg.trigger_threshold_pct = float(threshold_volts) / float(full_scale_v) if full_scale_v else 0.0
        counts = int(round((threshold_volts / full_scale_v) * max_adc)) if full_scale_v else 0
        if counts > int(max_adc):
            counts = int(max_adc)
        if counts < -int(max_adc):
            counts = -int(max_adc)
        if enabled:
            st = self.ps.ps5000aSetSimpleTrigger(
                self.handle,
                c_int16(1),
                c_int32(ch),
                c_int16(counts),
                c_int32(self.cfg.trigger_direction),
                c_int32(0),
                c_int32(0),
            )
            _check_status(st, "ps5000aSetSimpleTrigger(enable)")
        else:
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
        # Rearm trigger gating for streaming updates
        self._seen_trigger = False

    def _bind_functions(self) -> None:
        from ctypes import c_char_p
        self.ps.ps5000aOpenUnit.argtypes = [POINTER(c_int16), c_char_p, c_int32]
        self.ps.ps5000aOpenUnit.restype = c_int32
        self.ps.ps5000aOpenUnitProgress.argtypes = [POINTER(c_int16), c_char_p, POINTER(c_int16), POINTER(c_int16), c_int32]
        self.ps.ps5000aOpenUnitProgress.restype = c_int32
        self.ps.ps5000aCloseUnit.argtypes = [c_int16]
        self.ps.ps5000aCloseUnit.restype = c_int32
        self.ps.ps5000aSetChannel.argtypes = [c_int16, c_int32, c_int16, c_int32, c_int32, c_float]
        self.ps.ps5000aSetChannel.restype = c_int32
        self.ps.ps5000aSetDataBuffers.argtypes = [c_int16, c_int32, POINTER(c_int16), POINTER(c_int16), c_int32, c_uint32, c_int32]
        self.ps.ps5000aSetDataBuffers.restype = c_int32
        self.ps.ps5000aSetDataBuffer.argtypes = [c_int16, c_int32, POINTER(c_int16), c_int32, c_uint32, c_int32]
        self.ps.ps5000aSetDataBuffer.restype = c_int32
        self.ps.ps5000aRunStreaming.argtypes = [
            c_int16, POINTER(c_uint32), c_int32, c_uint32, c_uint32, c_int16, c_uint32, c_int32, c_uint32
        ]
        self.ps.ps5000aRunStreaming.restype = c_int32
        self.ps.ps5000aGetStreamingLatestValues.argtypes = [c_int16, c_void_p, c_void_p]
        self.ps.ps5000aGetStreamingLatestValues.restype = c_int32
        self.ps.ps5000aStop.argtypes = [c_int16]
        self.ps.ps5000aStop.restype = c_int32
        self.ps.ps5000aMaximumValue.argtypes = [c_int16, POINTER(c_int16)]
        self.ps.ps5000aMaximumValue.restype = c_int32
        self.ps.ps5000aMemorySegments.argtypes = [c_int16, c_uint32, POINTER(c_uint32)]
        self.ps.ps5000aMemorySegments.restype = c_int32
        self.ps.ps5000aSetNoOfCaptures.argtypes = [c_int16, c_uint32]
        self.ps.ps5000aSetNoOfCaptures.restype = c_int32
        self.ps.ps5000aChangePowerSource.argtypes = [c_int16, c_uint32]
        self.ps.ps5000aChangePowerSource.restype = c_int32
        self.ps.ps5000aCurrentPowerSource.argtypes = [c_int16]
        self.ps.ps5000aCurrentPowerSource.restype = c_int32
        self.ps.ps5000aGetTimebase2.argtypes = [c_int16, c_uint32, c_int32, POINTER(c_float), c_int16, c_uint32]
        self.ps.ps5000aGetTimebase2.restype = c_int32
        self.ps.ps5000aRunBlock.argtypes = [c_int16, c_int32, c_int32, c_uint32, c_int16, POINTER(c_int32), c_int32, c_void_p, c_void_p]
        self.ps.ps5000aRunBlock.restype = c_int32
        self.ps.ps5000aGetValues.argtypes = [c_int16, c_uint32, POINTER(c_uint32), c_uint32, c_int32, c_uint32, POINTER(c_int16)]
        self.ps.ps5000aGetValues.restype = c_int32
        self.ps.ps5000aSetSimpleTrigger.argtypes = [c_int16, c_int16, c_int32, c_int16, c_int32, c_int32, c_int32]
        self.ps.ps5000aSetSimpleTrigger.restype = c_int32
        self.ps.ps5000aIsReady.argtypes = [c_int16, POINTER(c_int16)]
        self.ps.ps5000aIsReady.restype = c_int32
        self.ps.ps5000aGetMinimumTimebaseStateless.argtypes = [c_int16, c_uint32, POINTER(c_uint32), POINTER(c_double), c_uint32]
        self.ps.ps5000aGetMinimumTimebaseStateless.restype = c_int32
        self.ps.ps5000aGetTimebase2.argtypes = [c_int16, c_uint32, c_int32, POINTER(c_float), c_int16, c_uint32]
        self.ps.ps5000aGetTimebase2.restype = c_int32
        self.ps.ps5000aRunBlock.argtypes = [c_int16, c_int32, c_int32, c_uint32, c_int16, POINTER(c_int32), c_uint32, c_void_p, c_void_p]
        self.ps.ps5000aRunBlock.restype = c_int32
        self.ps.ps5000aGetValues.argtypes = [c_int16, c_uint32, POINTER(c_uint32), c_uint32, c_int32, c_uint32, POINTER(c_int16)]
        self.ps.ps5000aGetValues.restype = c_int32

    def open(self) -> None:
        from ctypes import c_char_p
        serial_p = ctypes.c_char_p(None)
        res = PS5000A_DR_8BIT
        status = self.ps.ps5000aOpenUnit(byref(self.handle), serial_p, c_int32(res))
        code = int(status)
        if code in (PICO_POWER_SUPPLY_NOT_CONNECTED, PICO_POWER_SUPPLY_CONNECTED):
            ps = self.ps.ps5000aChangePowerSource(self.handle, c_uint32(code))
            if int(ps) != PICO_OK:
                pass
            else:
                self.cfg.resolution = res
        status = self.ps.ps5000aMaximumValue(self.handle, byref(self.max_adc))
        _check_status(status, "ps5000aMaximumValue")
        self._max_samples_per_segment = c_uint32(0)
        status = self.ps.ps5000aSetChannel(self.handle, PS5000A_CHANNEL_A, 1, self.cfg.coupling, self.cfg.range_a, c_float(0.0))
        _check_status(status, "ps5000aSetChannel(A)")
        status = self.ps.ps5000aSetChannel(self.handle, PS5000A_CHANNEL_B, 1, self.cfg.coupling, self.cfg.range_b, c_float(0.0))
        _check_status(status, "ps5000aSetChannel(B)")
        self._max_samples_per_segment = c_uint32(0)
        status = self.ps.ps5000aMemorySegments(self.handle, c_uint32(1), byref(self._max_samples_per_segment))
        _check_status(status, "ps5000aMemorySegments(1)")
        status = self.ps.ps5000aSetNoOfCaptures(self.handle, c_uint32(1))
        _check_status(status, "ps5000aSetNoOfCaptures(1)")
        if self.cfg.simple_trigger_enabled:
            thresh_counts = int(self.max_adc.value * self.cfg.trigger_threshold_pct)
            st = self.ps.ps5000aSetSimpleTrigger(
                self.handle,
                c_int16(1),
                c_int32(self.cfg.trigger_source),
                c_int16(thresh_counts),
                c_int32(self.cfg.trigger_direction),
                c_int32(0),
                c_int32(0)
            )
            _check_status(st, "ps5000aSetSimpleTrigger")
        status = self.ps.ps5000aSetDataBuffer(
            self.handle, PS5000A_CHANNEL_A, self._buf_a, self.cfg.driver_buffer_size, 0, PS5000A_RATIO_MODE_NONE
        )
        _check_status(status, "ps5000aSetDataBuffer(A)")
        status = self.ps.ps5000aSetDataBuffer(
            self.handle, PS5000A_CHANNEL_B, self._buf_b, self.cfg.driver_buffer_size, 0, PS5000A_RATIO_MODE_NONE
        )
        _check_status(status, "ps5000aSetDataBuffer(B)")
        time.sleep(self.cfg.connect_delay_ms / 1000.0)

    def set_range(self, channel: int, new_range: int) -> None:
        status = self.ps.ps5000aSetChannel(
            self.handle, channel, 1, self.cfg.coupling, new_range, c_float(0.0)
        )
        _check_status(status, f"ps5000aSetChannel({'A' if channel==PS5000A_CHANNEL_A else 'B'})")
        if channel == PS5000A_CHANNEL_A:
            self.cfg.range_a = new_range
        else:
            self.cfg.range_b = new_range
        if self.cfg.simple_trigger_enabled and channel == self.cfg.trigger_source:
            rng = new_range
            full_scale_v = RANGE_TO_VOLTS.get(rng, 2.0)
            volts = self.cfg.trigger_threshold_pct * full_scale_v
            try:
                self.apply_trigger(True, volts)
            except Exception:
                pass

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        def _stream_loop():
            sample_interval = c_uint32(max(1, self.cfg.sample_interval_ns))
            time_units = PS5000A_NS
            max_pre = c_uint32(0)
            max_post = c_uint32(0)
            auto_stop = c_int16(0)
            downsample = c_uint32(1)
            ratio_mode = PS5000A_RATIO_MODE_NONE
            overview = c_uint32(self.cfg.driver_buffer_size)
            st = self.ps.ps5000aRunStreaming(
                self.handle,
                byref(sample_interval),
                c_int32(time_units),
                max_pre,
                max_post,
                auto_stop,
                downsample,
                c_int32(ratio_mode),
                overview
            )
            _check_status(st, "ps5000aRunStreaming")

            while self._running:
                st2 = self.ps.ps5000aGetStreamingLatestValues(self.handle, self._streaming_cb, None)
                if int(st2) != PICO_OK:
                    pass
                time.sleep(self.cfg.plot_refresh_ms / 1000.0)

        self._thread = threading.Thread(target=_stream_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            self.ps.ps5000aStop(self.handle)
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self._thread = None

    def close(self) -> None:
        try:
            self.stop()
        finally:
            try:
                self.ps.ps5000aCloseUnit(self.handle)
            except Exception:
                pass

    def _on_block_ready(self, handle: int, status: int, pParameter: int) -> None:
        self._ready_evt.set()

    def _on_streaming_ready(self, handle: int, noOfSamples: int, startIndex: int, overflow: int, triggerAt: int, triggered: int, autoStop: int, pParameter: int) -> None:
        n = int(noOfSamples)
        if n <= 0:
            return
        # If simple trigger is enabled, freeze updates until the first trigger occurs
        if self.cfg.simple_trigger_enabled and not self._seen_trigger:
            if int(triggered) == 0:
                return
            # First trigger observed; allow updates from now on
            self._seen_trigger = True
        idx = int(startIndex)
        max_adc = float(self.max_adc.value if self.max_adc.value != 0 else 32767)
        scale_a = RANGE_TO_VOLTS.get(self.cfg.range_a, 2.0) / max_adc
        scale_b = RANGE_TO_VOLTS.get(self.cfg.range_b, 2.0) / max_adc
        end = min(idx + n, self.cfg.driver_buffer_size)
        cnt = max(0, end - idx)
        if cnt <= 0:
            return
        with self._lock:
            a = np.frombuffer(self._buf_a, dtype=np.int16, count=cnt, offset=idx * 2) * scale_a
            b = np.frombuffer(self._buf_b, dtype=np.int16, count=cnt, offset=idx * 2) * scale_b
            if cnt >= self._ring_len:
                self._y_a[:] = a[-self._ring_len:]
                self._y_b[:] = b[-self._ring_len:]
                self._write_idx = 0
            else:
                write_end = min(self._write_idx + cnt, self._ring_len)
                part1 = write_end - self._write_idx
                self._y_a[self._write_idx:write_end] = a[:part1]
                self._y_b[self._write_idx:write_end] = b[:part1]
                remaining = cnt - part1
                if remaining > 0:
                    self._y_a[0:remaining] = a[part1:part1+remaining]
                    self._y_b[0:remaining] = b[part1:part1+remaining]
                    self._write_idx = remaining
                else:
                    self._write_idx = write_end % self._ring_len

    def _ensure_sample_interval_supported(self) -> None:
        tb_out = c_uint32(0)
        ti_out = c_double(0.0)
        enabled_flags = (1 << PS5000A_CHANNEL_A) | (1 << PS5000A_CHANNEL_B)
        st = self.ps.ps5000aGetMinimumTimebaseStateless(
            self.handle,
            c_uint32(enabled_flags),
            byref(tb_out),
            byref(ti_out),
            c_uint32(self.cfg.resolution)
        )
        if int(st) != PICO_OK:
            _check_status(st, "ps5000aGetMinimumTimebaseStateless")
        tmp_dt = c_float(0.0)
        st2 = self.ps.ps5000aGetTimebase2(self.handle, tb_out, c_int32(self._ring_len), byref(tmp_dt), c_int16(0), c_uint32(0))
        _check_status(st2, "ps5000aGetTimebase2(min)")
        min_ns = float(tmp_dt.value)
        req_ns = float(self.cfg.sample_interval_ns)
        if req_ns < min_ns:
            self.cfg.sample_interval_ns = int(round(min_ns))
            self._dt_s = self.cfg.sample_interval_ns * 1e-9
            window_s = self.cfg.plot_window_ms * 1e-3
            with self._lock:
                self._t = np.linspace(0.0, window_s - self._dt_s, self._ring_len, dtype=np.float64)

    def reconfigure_timebase(self, new_sample_interval_ns: int) -> int:
        self.cfg.sample_interval_ns = int(max(1, new_sample_interval_ns))
        try:
            self._ensure_sample_interval_supported()
        except Exception:
            pass
        self._dt_s = self.cfg.sample_interval_ns * 1e-9
        window_s = self.cfg.plot_window_ms * 1e-3
        new_ring_len = int(max(10, round(window_s / self._dt_s)))
        with self._lock:
            self._ring_len = new_ring_len
            self._y_a = np.zeros(self._ring_len, dtype=np.float32)
            self._y_b = np.zeros(self._ring_len, dtype=np.float32)
            self._write_idx = 0
            self._t = np.linspace(0.0, window_s - self._dt_s, self._ring_len, dtype=np.float64)
        return int(self.cfg.sample_interval_ns)

    def reconfigure_window_ms(self, new_window_ms: float) -> float:
        new_window_ms = float(max(0.01, new_window_ms))
        self.cfg.plot_window_ms = float(new_window_ms)
        window_s = self.cfg.plot_window_ms * 1e-3
        dt_s = self._dt_s
        new_ring_len = int(max(10, round(window_s / dt_s)))
        with self._lock:
            self._ring_len = new_ring_len
            self._y_a = np.zeros(self._ring_len, dtype=np.float32)
            self._y_b = np.zeros(self._ring_len, dtype=np.float32)
            self._write_idx = 0
            self._t = np.linspace(0.0, window_s - dt_s, self._ring_len, dtype=np.float64)
        return float(self.cfg.plot_window_ms)
