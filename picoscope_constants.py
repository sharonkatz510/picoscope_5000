from __future__ import annotations

import os
import json

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

# Status codes
PICO_OK = 0x00000000
PICO_POWER_SUPPLY_CONNECTED = 0x00000119
PICO_POWER_SUPPLY_NOT_CONNECTED = 0x0000011A
PICO_INVALID_HANDLE = 0x0000000C
PICO_INVALID_PARAMETER = 0x0000000D
PICO_INVALID_TIMEBASE = 0x0000000E

STATUS_TEXT: dict[int, str] = {
    PICO_OK: "OK",
    PICO_POWER_SUPPLY_NOT_CONNECTED: "Power supply not connected",
    PICO_POWER_SUPPLY_CONNECTED: "Power supply connected (change required)",
    PICO_INVALID_PARAMETER: "Invalid parameter",
    PICO_INVALID_TIMEBASE: "Invalid timebase",
    PICO_INVALID_HANDLE: "Invalid handle",
}


def _status_text(code: int) -> str:
    return STATUS_TEXT.get(int(code), f"Unknown status 0x{int(code):08X}")


def load_status_texts_from_file() -> None:
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

# Auto-load at import time
load_status_texts_from_file()
