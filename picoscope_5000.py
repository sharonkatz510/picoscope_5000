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

import os
import sys
import threading
import time
import ctypes
import json
from ctypes import (
    byref, c_int16, c_int32, c_uint32, c_float, c_double, c_int8, c_void_p,
    POINTER, WINFUNCTYPE
)
from ctypes.util import find_library
from dataclasses import dataclass

import numpy as np

from PyQt5 import QtCore, QtWidgets
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib import ticker as mticker
from matplotlib.figure import Figure


# -----------------------------
# PicoSDK / ps5000a constants
# -----------------------------

# Channels (enum order from PS5000A_CHANNEL)
PS5000A_CHANNEL_A = 0
PS5000A_CHANNEL_B = 1

# Coupling (enum order from PS5000A_COUPLING)
PS5000A_AC = 0
PS5000A_DC = 1

# Ranges (enum order from PS5000A_RANGE)
PS5000A_10MV = 0
PS5000A_20MV = 1
PS5000A_50MV = 2
PS5000A_100MV = 3
PS5000A_200MV = 4
PS5000A_500MV = 5
PS5000A_1V = 6
PS5000A_2V = 7
PS5000A_5V = 8
PS5000A_10V = 9
PS5000A_20V = 10

RANGE_TO_VOLTS = {
    PS5000A_10MV: 0.010,
    PS5000A_20MV: 0.020,
    PS5000A_50MV: 0.050,
    PS5000A_100MV: 0.100,
    PS5000A_200MV: 0.200,
    PS5000A_500MV: 0.500,
    PS5000A_1V: 1.0,
    PS5000A_2V: 2.0,
    PS5000A_5V: 5.0,
    PS5000A_10V: 10.0,
    PS5000A_20V: 20.0,
}

# Ordered list of ranges for cycling via GUI
RANGE_CODES = [
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
]

RANGE_LABELS = {
    PS5000A_10MV: "10 mV",
    PS5000A_20MV: "20 mV",
    PS5000A_50MV: "50 mV",
    PS5000A_100MV: "100 mV",
    PS5000A_200MV: "200 mV",
    PS5000A_500MV: "500 mV",
    PS5000A_1V: "1 V",
    PS5000A_2V: "2 V",
    PS5000A_5V: "5 V",
    PS5000A_10V: "10 V",
    PS5000A_20V: "20 V",
}

# Time units (enum order from PS5000A_TIME_UNITS)
PS5000A_FS = 0
PS5000A_PS = 1
PS5000A_NS = 2
PS5000A_US = 3
PS5000A_MS = 4
PS5000A_S = 5

# Ratio modes (enum order from PS5000A_RATIO_MODE)
PS5000A_RATIO_MODE_NONE = 0
PS5000A_RATIO_MODE_AGGREGATE = 1
PS5000A_RATIO_MODE_DECIMATE = 2
PS5000A_RATIO_MODE_AVERAGE = 4

# Resolution (enum order from PS5000A_DEVICE_RESOLUTION)
PS5000A_DR_8BIT = 0
PS5000A_DR_12BIT = 1
PS5000A_DR_14BIT = 2
PS5000A_DR_15BIT = 3
PS5000A_DR_16BIT = 4

# Status codes: PicoStatus.h defines many.
PICO_OK = 0x00000000
# Selected codes from PicoStatus.h (exact values):
# Power status codes returned by ps5000aOpenUnit
PICO_POWER_SUPPLY_CONNECTED = 0x00000119
PICO_POWER_SUPPLY_NOT_CONNECTED = 0x0000011A
# Common error codes
PICO_INVALID_HANDLE = 0x0000000C
PICO_INVALID_PARAMETER = 0x0000000D
PICO_INVALID_TIMEBASE = 0x0000000E

STATUS_TEXT = {
    PICO_OK: "OK",
    PICO_POWER_SUPPLY_NOT_CONNECTED: "Power supply not connected",
    PICO_POWER_SUPPLY_CONNECTED: "Power supply connected (change required)",
    PICO_INVALID_PARAMETER: "Invalid parameter",
    PICO_INVALID_TIMEBASE: "Invalid timebase",
    PICO_INVALID_HANDLE: "Invalid handle",
}

def _status_text(code: int) -> str:
    return STATUS_TEXT.get(int(code), f"Unknown status 0x{int(code):08X}")

def _load_status_texts_from_file() -> None:
    """Load additional status messages from pico_status_dict.json in the same folder."""
    try:
        here = os.path.dirname(__file__)
        path = os.path.join(here, "pico_status_dict.json")
        if not os.path.isfile(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            try:
                if isinstance(k, str) and k.lower().startswith("0x"):
                    ik = int(k, 16)
                else:
                    ik = int(k)
                STATUS_TEXT[ik] = str(v)
            except Exception:
                continue
    except Exception:
        # Non-fatal: keep built-in minimal map
        pass

_load_status_texts_from_file()


def _find_ps5000a_dll() -> str:
    """
    Find ps5000a.dll.
    Priority:
    1) PICO_PS5000A_DLL environment variable (full path)
    2) common PicoSDK install locations
    3) PATH search / find_library
    """
    env_path = os.environ.get("PICO_PS5000A_DLL", "").strip()
    if env_path and os.path.isfile(env_path):
        return env_path

    candidates = [
        r"C:\Program Files\Pico Technology\SDK\lib\ps5000a.dll",
        r"C:\Program Files\Pico Technology\PicoScope 7 T&M Stable\ps5000a.dll",
        r"C:\Program Files\Pico Technology\PicoScope 6\ps5000a.dll",
        r"C:\Program Files\Pico Technology\PicoScope6\ps5000a.dll",
        r"C:\Program Files (x86)\Pico Technology\SDK\lib\ps5000a.dll",
        r"C:\Program Files (x86)\Pico Technology\PicoScope 6\ps5000a.dll",
        r"C:\Program Files (x86)\Pico Technology\PicoScope6\ps5000a.dll",
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


# -----------------------------
# PicoScope streaming wrapper
# -----------------------------

@dataclass
class StreamConfig:
    # 20 MHz sampling -> 50 ns sample interval
    sample_interval_ns: int = 1000

    # Plot refresh period and plot window
    plot_refresh_ms: int = 20          # GUI refresh
    plot_window_ms: float = 20.0       # rolling buffer shown in plot (ms)

    # Display decimation (keep full buffer internally, plot fewer points)
    plot_max_points: int = 6000

    range_a: int = PS5000A_2V
    range_b: int = PS5000A_2V
    coupling: int = PS5000A_DC
    resolution: int = PS5000A_DR_8BIT

    # Driver overview buffer size - this is the chunk size the driver fills per callback
    # Make it large enough to reduce callback overhead at high rates.
    driver_buffer_size: int = 200_000
    # Delay after opening scope for USB/device to settle (ms)
    connect_delay_ms: int = 1000
    # Trigger configuration (simple trigger); disabled by default
    simple_trigger_enabled: bool = False
    trigger_source: int = PS5000A_CHANNEL_A
    trigger_threshold_pct: float = 0.1  # fraction of full scale
    trigger_direction: int = 2  # rising (SDK enum typical)


class PicoScopeStreamer:
    def __init__(self, cfg: StreamConfig):
        self.cfg = cfg
        self._dll_path = _find_ps5000a_dll()
        # Ensure Windows can locate dependent PicoSDK DLLs (e.g., picoipp.dll)
        self._dll_dir_ctx = None
        try:
            if hasattr(os, "add_dll_directory"):
                # Add the directory containing ps5000a.dll
                self._dll_dir_ctx = os.add_dll_directory(os.path.dirname(self._dll_path))
                # Add common PicoSDK lib directories for dependencies like picoipp.dll
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
        # Preload picoipp.dll from the same directory if present
        try:
            sdk_dir = os.path.dirname(self._dll_path)
            picoipp_path = os.path.join(sdk_dir, "picoipp.dll")
            if os.path.isfile(picoipp_path):
                ctypes.WinDLL(picoipp_path)
        except Exception:
            # If preload fails, WinDLL(ps5000a.dll) may still succeed if PATH is set
            pass
        self.ps = ctypes.WinDLL(self._dll_path)

        self.handle = c_int16(0)
        self.max_adc = c_int16(0)

        # Driver buffers (raw ADC counts)
        self._buf_a = (c_int16 * cfg.driver_buffer_size)()
        self._buf_b = (c_int16 * cfg.driver_buffer_size)()

        # Ring buffers (volts) for plotting - 20 ms rolling window at 20 MHz = 400k samples
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

        # Precompute time axis for the rolling window
        window_s = cfg.plot_window_ms * 1e-3
        self._t = np.linspace(0.0, window_s - self._dt_s, self._ring_len, dtype=np.float64)

        # Callbacks and event
        # Block-ready for block mode
        self._block_ready_type = WINFUNCTYPE(None, c_int16, c_uint32, c_void_p)
        self._block_ready_cb = self._block_ready_type(self._on_block_ready)
        # Streaming-ready for streaming mode
        self._streaming_ready_type = WINFUNCTYPE(None, c_int16, c_int32, c_uint32, c_int16, c_uint32, c_int16, c_int16, c_void_p)
        self._streaming_cb = self._streaming_ready_type(self._on_streaming_ready)
        self._ready_evt = threading.Event()

        self._bind_functions()

    def apply_trigger(self, enabled: bool, threshold_volts: float) -> None:
        # Configure simple trigger for streaming/block per current settings
        self.cfg.simple_trigger_enabled = bool(enabled)
        # Assume trigger on channel A for now (cfg.trigger_source)
        ch = self.cfg.trigger_source
        rng = self.cfg.range_a if ch == PS5000A_CHANNEL_A else self.cfg.range_b
        full_scale_v = RANGE_TO_VOLTS.get(rng, 2.0)
        max_adc = float(self.max_adc.value if self.max_adc.value != 0 else 32767)
        # Store as fraction of full scale for consistency
        self.cfg.trigger_threshold_pct = float(threshold_volts) / float(full_scale_v) if full_scale_v else 0.0
        # Convert volts to ADC counts (signed), clamp to device limits
        counts = int(round((threshold_volts / full_scale_v) * max_adc)) if full_scale_v else 0
        if counts > int(max_adc):
            counts = int(max_adc)
        if counts < -int(max_adc):
            counts = -int(max_adc)
        print(f"Trigger {'ENABLED' if enabled else 'DISABLED'}: {threshold_volts:.6f} V (rising), counts {counts}")
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

    def _bind_functions(self) -> None:
        # ps5000aOpenUnit(handle*, serial*, resolution)
        # serial is int8_t* (C char*); use c_char_p for NULL or ASCII serial
        from ctypes import c_char_p
        self.ps.ps5000aOpenUnit.argtypes = [POINTER(c_int16), c_char_p, c_int32]
        self.ps.ps5000aOpenUnit.restype = c_int32

        # ps5000aOpenUnitProgress(handle*, serial*, progress*, complete*, resolution)
        # progress/complete are int16* that indicate connection status
        self.ps.ps5000aOpenUnitProgress.argtypes = [POINTER(c_int16), c_char_p, POINTER(c_int16), POINTER(c_int16), c_int32]
        self.ps.ps5000aOpenUnitProgress.restype = c_int32

        # ps5000aCloseUnit(handle)
        self.ps.ps5000aCloseUnit.argtypes = [c_int16]
        self.ps.ps5000aCloseUnit.restype = c_int32

        # ps5000aSetChannel(handle, channel, enabled, type, range, analogueOffset)
        self.ps.ps5000aSetChannel.argtypes = [c_int16, c_int32, c_int16, c_int32, c_int32, c_float]
        self.ps.ps5000aSetChannel.restype = c_int32

        # ps5000aSetDataBuffers(handle, channel, bufferMax, bufferMin, bufferLth, segmentIndex, ratioMode)
        self.ps.ps5000aSetDataBuffers.argtypes = [c_int16, c_int32, POINTER(c_int16), POINTER(c_int16), c_int32, c_uint32, c_int32]
        self.ps.ps5000aSetDataBuffers.restype = c_int32
        # ps5000aSetDataBuffer(handle, channel, buffer, bufferLth, segmentIndex, ratioMode)
        self.ps.ps5000aSetDataBuffer.argtypes = [c_int16, c_int32, POINTER(c_int16), c_int32, c_uint32, c_int32]
        self.ps.ps5000aSetDataBuffer.restype = c_int32

        # ps5000aRunStreaming(handle, sampleInterval*, timeUnits, maxPre, maxPost, autoStop, downSampleRatio, ratioMode, overviewBufferSize)
        self.ps.ps5000aRunStreaming.argtypes = [
            c_int16, POINTER(c_uint32), c_int32, c_uint32, c_uint32, c_int16, c_uint32, c_int32, c_uint32
        ]
        self.ps.ps5000aRunStreaming.restype = c_int32

        # ps5000aGetStreamingLatestValues(handle, callback, pParameter)
        self.ps.ps5000aGetStreamingLatestValues.argtypes = [c_int16, c_void_p, c_void_p]
        self.ps.ps5000aGetStreamingLatestValues.restype = c_int32

        # ps5000aStop(handle)
        self.ps.ps5000aStop.argtypes = [c_int16]
        self.ps.ps5000aStop.restype = c_int32

        # ps5000aMaximumValue(handle, value*)
        self.ps.ps5000aMaximumValue.argtypes = [c_int16, POINTER(c_int16)]
        self.ps.ps5000aMaximumValue.restype = c_int32

        # Memory segments (ensure a valid segment index exists)
        # ps5000aMemorySegments(handle, nSegments, maxSamples*)
        self.ps.ps5000aMemorySegments.argtypes = [c_int16, c_uint32, POINTER(c_uint32)]
        self.ps.ps5000aMemorySegments.restype = c_int32

        # Rapid block captures count (ensure a valid captures setting)
        # ps5000aSetNoOfCaptures(handle, nCaptures)
        self.ps.ps5000aSetNoOfCaptures.argtypes = [c_int16, c_uint32]
        self.ps.ps5000aSetNoOfCaptures.restype = c_int32

        # ps5000aChangePowerSource(handle, powerStatus)
        self.ps.ps5000aChangePowerSource.argtypes = [c_int16, c_uint32]
        self.ps.ps5000aChangePowerSource.restype = c_int32
        # ps5000aCurrentPowerSource(handle)
        self.ps.ps5000aCurrentPowerSource.argtypes = [c_int16]
        self.ps.ps5000aCurrentPowerSource.restype = c_int32

        # Block-mode APIs
        self.ps.ps5000aGetTimebase2.argtypes = [c_int16, c_uint32, c_int32, POINTER(c_float), c_int16, c_uint32]
        self.ps.ps5000aGetTimebase2.restype = c_int32
        self.ps.ps5000aRunBlock.argtypes = [c_int16, c_int32, c_int32, c_uint32, c_int16, POINTER(c_int32), c_int32, c_void_p, c_void_p]
        self.ps.ps5000aRunBlock.restype = c_int32
        self.ps.ps5000aGetValues.argtypes = [c_int16, c_uint32, POINTER(c_uint32), c_uint32, c_int32, c_uint32, POINTER(c_int16)]
        self.ps.ps5000aGetValues.restype = c_int32

        self._max_samples_per_segment = c_uint32(0)

        # Simple trigger (optional)
        self.ps.ps5000aSetSimpleTrigger.argtypes = [c_int16, c_int16, c_int32, c_int16, c_int32, c_int32, c_int32]
        self.ps.ps5000aSetSimpleTrigger.restype = c_int32

        # Poll (unused)
        self.ps.ps5000aIsReady.argtypes = [c_int16, POINTER(c_int16)]
        self.ps.ps5000aIsReady.restype = c_int32

        # Minimum timebase (stateless): helps detect too-high requested sampling
        # ps5000aGetMinimumTimebaseStateless(handle, enabledChannelOrPortFlags, timebase*, timeInterval*, resolution)
        self.ps.ps5000aGetMinimumTimebaseStateless.argtypes = [c_int16, c_uint32, POINTER(c_uint32), POINTER(c_double), c_uint32]
        self.ps.ps5000aGetMinimumTimebaseStateless.restype = c_int32

        # Block-mode APIs
        # ps5000aGetTimebase2(handle, timebase, noSamples, timeIntervalNanoseconds*, oversample, segmentIndex)
        self.ps.ps5000aGetTimebase2.argtypes = [c_int16, c_uint32, c_int32, POINTER(c_float), c_int16, c_uint32]
        self.ps.ps5000aGetTimebase2.restype = c_int32

        # ps5000aRunBlock(handle, preTrig, postTrig, timebase, oversample, timeIndisposedMs*, segmentIndex, lpReady, pParameter)
        self.ps.ps5000aRunBlock.argtypes = [c_int16, c_int32, c_int32, c_uint32, c_int16, POINTER(c_int32), c_uint32, c_void_p, c_void_p]
        self.ps.ps5000aRunBlock.restype = c_int32

        # ps5000aGetValues(handle, startIndex, noOfSamples*, downSampleRatio, ratioMode, segmentIndex, overflow*)
        self.ps.ps5000aGetValues.argtypes = [c_int16, c_uint32, POINTER(c_uint32), c_uint32, c_int32, c_uint32, POINTER(c_int16)]
        self.ps.ps5000aGetValues.restype = c_int32

    def open(self) -> None:
        print("Step 1: Opening unit (OpenUnit)...")
        serial_p = ctypes.c_char_p(None)  # open first scope found

        # Try preferred then fallback resolutions
        res = PS5000A_DR_8BIT
        last_err: int | None = None
        status = self.ps.ps5000aOpenUnit(byref(self.handle), serial_p, c_int32(res))
        code = int(status)
        if code == PICO_OK:
            self.cfg.resolution = res
            print(f"Step 1: Unit opened at resolution {res}")
        if code in (PICO_POWER_SUPPLY_NOT_CONNECTED, PICO_POWER_SUPPLY_CONNECTED):
            print(f"Step 1: { _status_text(code) }; changing power source...")
            # Inform device of current supply state per returned code
            ps = self.ps.ps5000aChangePowerSource(self.handle, c_uint32(code))
            ps_code = int(ps)
            if ps_code != PICO_OK:
                last_err = ps_code
                print(f"Step 1: ChangePowerSource failed: { _status_text(ps_code) }")
            else:
                self.cfg.resolution = res
                print(f"Step 1: Unit opened after power change at resolution {res}")
        status = self.ps.ps5000aMaximumValue(self.handle, byref(self.max_adc))
        _check_status(status, "ps5000aMaximumValue")

        # In standard block mode, segmentation is not required. Some variants allow calling
        # MemorySegments for rapid block; we skip it here to avoid mode conflicts.
        self._max_samples_per_segment = c_uint32(0)

        # Configure channels A and B
        print("Step 2: Configuring channels and coupling (SetChannel)...")
        status = self.ps.ps5000aSetChannel(self.handle, PS5000A_CHANNEL_A, 1, self.cfg.coupling, self.cfg.range_a, c_float(0.0))
        _check_status(status, "ps5000aSetChannel(A)")
        status = self.ps.ps5000aSetChannel(self.handle, PS5000A_CHANNEL_B, 1, self.cfg.coupling, self.cfg.range_b, c_float(0.0))
        _check_status(status, "ps5000aSetChannel(B)")

        # Ensure a valid memory segment exists for block-mode acquisitions
        print("Step 3: Initializing memory segments (1 segment)...")
        self._max_samples_per_segment = c_uint32(0)
        status = self.ps.ps5000aMemorySegments(self.handle, c_uint32(1), byref(self._max_samples_per_segment))
        _check_status(status, "ps5000aMemorySegments(1)")
        status = self.ps.ps5000aSetNoOfCaptures(self.handle, c_uint32(1))
        _check_status(status, "ps5000aSetNoOfCaptures(1)")

        # Step 4: Simple trigger setup (optional)
        if self.cfg.simple_trigger_enabled:
            print("Step 4: Setting simple trigger (SetSimpleTrigger)...")
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
        else:
            print("Step 4: Simple trigger disabled")

        # Step 7: Set data buffers (outside acquisition loop)
        print("Step 7: Setting data buffers (SetDataBuffer, raw mode)...")
        # Use SetDataBuffer for raw ADC samples (no aggregation)
        status = self.ps.ps5000aSetDataBuffer(
            self.handle, PS5000A_CHANNEL_A, self._buf_a, self.cfg.driver_buffer_size, 0, PS5000A_RATIO_MODE_NONE
        )
        _check_status(status, "ps5000aSetDataBuffer(A)")
        status = self.ps.ps5000aSetDataBuffer(
            self.handle, PS5000A_CHANNEL_B, self._buf_b, self.cfg.driver_buffer_size, 0, PS5000A_RATIO_MODE_NONE
        )
        _check_status(status, "ps5000aSetDataBuffer(B)")
        # Allow some time for device/USB to settle before acquisitions
        time.sleep(self.cfg.connect_delay_ms / 1000.0)

    def set_range(self, channel: int, new_range: int) -> None:
        # Update device channel range and local config
        enabled = 1
        status = self.ps.ps5000aSetChannel(
            self.handle, channel, enabled, self.cfg.coupling, new_range, c_float(0.0)
        )
        _check_status(status, f"ps5000aSetChannel({'A' if channel==PS5000A_CHANNEL_A else 'B'})")
        if channel == PS5000A_CHANNEL_A:
            self.cfg.range_a = new_range
        else:
            self.cfg.range_b = new_range
        # If trigger is enabled and tied to this channel, re-apply with same volts
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
            # Configure and start streaming
            sample_interval = c_uint32(max(1, self.cfg.sample_interval_ns))
            time_units = PS5000A_NS
            max_pre = c_uint32(0)
            max_post = c_uint32(0)
            auto_stop = c_int16(0)
            downsample = c_uint32(1)
            ratio_mode = PS5000A_RATIO_MODE_NONE
            overview = c_uint32(self.cfg.driver_buffer_size)
            print("Step 5: Starting streaming (RunStreaming)...")
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
                # Fetch latest values via callback; driver fills our buffers
                st2 = self.ps.ps5000aGetStreamingLatestValues(self.handle, self._streaming_cb, None)
                if int(st2) != PICO_OK:
                    # Non-fatal; continue
                    pass
                time.sleep(self.cfg.plot_refresh_ms / 1000.0)

        self._thread = threading.Thread(target=_stream_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        print("Step 11: Stopping oscilloscope (Stop)...")
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
        # Convert newly received samples to volts and update rolling window
        n = int(noOfSamples)
        if n <= 0:
            return
        idx = int(startIndex)
        max_adc = float(self.max_adc.value if self.max_adc.value != 0 else 32767)
        scale_a = RANGE_TO_VOLTS.get(self.cfg.range_a, 2.0) / max_adc
        scale_b = RANGE_TO_VOLTS.get(self.cfg.range_b, 2.0) / max_adc
        # Ensure indices are within our driver buffer
        end = min(idx + n, self.cfg.driver_buffer_size)
        cnt = max(0, end - idx)
        if cnt <= 0:
            return
        with self._lock:
            a = np.frombuffer(self._buf_a, dtype=np.int16, count=cnt, offset=idx * 2) * scale_a
            b = np.frombuffer(self._buf_b, dtype=np.int16, count=cnt, offset=idx * 2) * scale_b
            # Write into ring buffer; if cnt > ring length, keep the last portion
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
        """Check the minimum achievable timebase using stateless API.
        If the requested sampling interval is lower (i.e., faster) than supported,
        adjust `self.cfg.sample_interval_ns` upward and log diagnostics.
        """
        tb_out = c_uint32(0)
        ti_out = c_double(0.0)
        # Enable flags for A and B channels (bitmask): assume bit per channel id
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
        # Resolve units via GetTimebase2 using returned minimum timebase to get nanoseconds
        tmp_dt = c_float(0.0)
        st2 = self.ps.ps5000aGetTimebase2(self.handle, tb_out, c_int32(self._ring_len), byref(tmp_dt), c_int16(0), c_uint32(0))
        _check_status(st2, "ps5000aGetTimebase2(min)")
        min_ns = float(tmp_dt.value)
        req_ns = float(self.cfg.sample_interval_ns)
        if req_ns < min_ns:
            print(f"Step 3a: Requested {req_ns:.2f} ns too fast; adjusting to {min_ns:.2f} ns (tb {int(tb_out.value)})")
            self.cfg.sample_interval_ns = int(round(min_ns))
            # Update internal dt and time axis to reflect new rate
            self._dt_s = self.cfg.sample_interval_ns * 1e-9
            window_s = self.cfg.plot_window_ms * 1e-3
            with self._lock:
                self._t = np.linspace(0.0, window_s - self._dt_s, self._ring_len, dtype=np.float64)

    def _find_timebase(self, target_ns: int, samples: int) -> int:
        best_tb = 1
        best_err = float('inf')
        tmp_dt = c_float(0.0)
        for tb in range(1, 100):
            st = self.ps.ps5000aGetTimebase2(self.handle, c_uint32(tb), c_int32(samples), byref(tmp_dt), c_int16(0), c_uint32(0))
            if int(st) != PICO_OK:
                continue
            dt_ns = float(tmp_dt.value)
            err = abs(dt_ns - target_ns)
            if err < best_err:
                best_err = err
                best_tb = tb
                self._dt_s = dt_ns * 1e-9
                window_s = self.cfg.plot_window_ms * 1e-3
                with self._lock:
                    self._t = np.linspace(0.0, window_s - self._dt_s, self._ring_len, dtype=np.float64)
                print(f"Step 3: Timebase {tb} -> {dt_ns:.2f} ns/sample")
            if best_err < 1e-6:
                break
        return best_tb

    def reconfigure_timebase(self, new_sample_interval_ns: int) -> int:
        """Update the requested sample interval, rebuild ring buffers/time axis.
        Caller should stop streaming before calling and restart after.
        Returns the actual sample interval (ns) after constraints.
        """
        # Update request
        self.cfg.sample_interval_ns = int(max(1, new_sample_interval_ns))
        # Ensure within device limits and update dt
        try:
            self._ensure_sample_interval_supported()
        except Exception:
            pass
        self._dt_s = self.cfg.sample_interval_ns * 1e-9
        window_s = self.cfg.plot_window_ms * 1e-3
        # Recompute ring length to preserve time window duration
        new_ring_len = int(max(10, round(window_s / self._dt_s)))
        with self._lock:
            self._ring_len = new_ring_len
            self._y_a = np.zeros(self._ring_len, dtype=np.float32)
            self._y_b = np.zeros(self._ring_len, dtype=np.float32)
            self._write_idx = 0
            self._t = np.linspace(0.0, window_s - self._dt_s, self._ring_len, dtype=np.float64)
        return int(self.cfg.sample_interval_ns)

    def reconfigure_window_ms(self, new_window_ms: float) -> float:
        """Resize rolling window duration (ms) and rebuild ring buffers/time axis.
        Does not change device sampling interval; safe while streaming.
        Returns the actual window (ms).
        """
        # Allow sub-millisecond windows (down to 0.01 ms = 10 µs)
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


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PicoScope 5000B Streaming Viewer")

        self.cfg = StreamConfig()
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
        
        # Matplotlib plot (only backend)
        self.mpl_canvas = FigureCanvas(Figure(figsize=(6, 3), dpi=100))
        self.mpl_ax = self.mpl_canvas.figure.add_subplot(111)
        self.mpl_ax.grid(True)
        self.mpl_ax.set_ylim(-0.5, 0.5)
        # Time axis label/formatter set based on current window
        (self.mpl_line_a,) = self.mpl_ax.plot([], [], color='c', linewidth=1, label='Channel A')
        (self.mpl_line_b,) = self.mpl_ax.plot([], [], color='m', linewidth=1, label='Channel B')
        self.mpl_ax.legend(loc='upper right')
        vbox.addWidget(self.mpl_canvas, 1)
        self.setCentralWidget(central)

        window_s = self.cfg.plot_window_ms * 1e-3
        self.dt = self.cfg.sample_interval_ns * 1e-9
        self.t = np.linspace(0.0, window_s - self.dt, int(round(window_s / self.dt)), dtype=np.float64)

        self.streamer: PicoScopeStreamer | None = None
        self._synthetic_phase = 0.0
        # Base rate tracking
        self._rate_base_ns: int = int(self.cfg.sample_interval_ns)
        # Discrete timebase steps in seconds (10 µs .. 10 ms)
        self._timebase_steps_s = [
            10e-6, 20e-6, 50e-6,
            100e-6, 200e-6, 500e-6,
            1e-3, 2e-3, 5e-3, 10e-3,
        ]

        try:
            self.streamer = PicoScopeStreamer(self.cfg)
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

            # Matplotlib with decimation for performance
            tt_d, ya_d = self._decimate(tt, ya_n, self.cfg.plot_max_points)
            _, yb_d = self._decimate(tt, yb_n, self.cfg.plot_max_points)
            self.mpl_line_a.set_data(tt_d, ya_d)
            self.mpl_line_b.set_data(tt_d, yb_d)
            if len(tt_d) > 1:
                # Show the full available window without extra zoom to avoid empty areas
                self.mpl_ax.set_xlim(float(tt_d[0]), float(tt_d[-1]))
            self.mpl_canvas.draw_idle()
        else:
            # No hardware or not running: do not draw synthetic data
            return

    def _decimate(self, x: np.ndarray, y: np.ndarray, max_points: int) -> tuple[np.ndarray, np.ndarray]:
        n = len(x)
        if n <= max_points:
            return x, y
        idx = np.linspace(0, n - 1, max_points, dtype=np.int32)
        return x[idx], y[idx]

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

        # Update Matplotlib x-limits to match time window if active
        if hasattr(self, 'mpl_ax'):
            self.mpl_ax.set_ylim(-0.5, 0.5)
            self._apply_time_axis_format(window_s)
            # x-limits will be updated on next draw with data

    def _apply_time_axis_format(self, window_s: float) -> None:
        # Choose units based on window size
        if window_s < 1e-3:
            # microseconds
            self.mpl_ax.set_xlabel("Time (µs)")
            self.mpl_ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, pos: f"{x * 1e6:.1f}"))
        else:
            # milliseconds
            self.mpl_ax.set_xlabel("Time (ms)")
            self.mpl_ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, pos: f"{x * 1e3:.1f}"))

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


    def closeEvent(self, a0) -> None:
        try:
            if self.streamer:
                self.streamer.close()
        finally:
            super().closeEvent(a0)


def picoscope_5000() -> int:
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(picoscope_5000())
