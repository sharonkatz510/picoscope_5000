
# PicoScope 5000B real-time streaming viewer (Python, Windows 11)

This project connects to a PicoScope 5000A/5000B/5000D device using the **ps5000a** driver (PicoSDK), streams Channels A and B, and plots live data in a GUI.

The core API path is:
- `ps5000aOpenUnit` to open the scope
- `ps5000aSetChannel` to enable Channels A and B and set coupling and range
- `ps5000aRunStreaming` to start streaming mode
- `ps5000aGetStreamingLatestValues` to receive data via the `ps5000aStreamingReady` callback

These functions and the callback pattern are documented in Pico's 5000A API Programmer's Guide.  

## What it does

- Requests a sampling interval of **20 ms** (50 samples per second) from Channel A and Channel B
- Runs streaming continuously (FIFO mode)
- Updates the GUI plot every 20 ms and shows the latest ~5 seconds of data

If you need a faster sampling rate (many samples per 20 ms screen update), change `sample_interval_ms` and the buffer settings in `picoscope_5000.py`.

## Requirements

### 1) PicoSDK driver and DLLs

You must install Pico Technology's Windows software that includes the **ps5000a** driver.

You need:
- `ps5000a.dll` (the device driver DLL)
- PicoScope USB drivers (installed with PicoScope software / PicoSDK)

The program loads `ps5000a.dll` via `ctypes.WinDLL()`.

#### Where the DLL usually is

Common locations:
- `C:\Program Files\Pico Technology\PicoSDK\lib\ps5000a.dll`
- `C:\Program Files (x86)\Pico Technology\PicoSDK\lib\ps5000a.dll`

If your install is elsewhere, set an environment variable to the full path:

- `PICO_PS5000A_DLL=C:\full\path\to\ps5000a.dll`

Example (PowerShell):
```powershell
setx PICO_PS5000A_DLL "C:\Program Files\Pico Technology\PicoSDK\lib\ps5000a.dll"
```

Close and reopen the terminal after setting it.

### 2) Python

Recommended: Python 3.10+ (64-bit)

### 3) Python packages

Install:
- numpy
- PyQt5
- matplotlib

Install with:
```powershell
python -m pip install -r requirements.txt
```

## Install and run

1) Install PicoScope software or PicoSDK so `ps5000a.dll` exists on your machine.

2) Create and activate a virtual environment (recommended):
```powershell
python -m venv .venv
.venv\Scripts\activate
```

3) Install Python dependencies:
```powershell
python -m pip install -r requirements.txt
```

4) Run:
```powershell
python picoscope_5000.py
```

Click **Start** to connect and stream, and **Stop** to disconnect.

## Configuration

Edit `StreamConfig` in `picoscope_5000.py`:

- `sample_interval_ms`: requested time between samples (default 20)
- `range_a`, `range_b`: channel voltage ranges (default +/- 2 V)
- `coupling`: AC or DC (default DC)
- `resolution`: 8, 12, 14, 15, 16-bit (default 12-bit)
- `driver_buffer_size`: size of the driver overview buffers
- `ring_buffer_seconds`: seconds shown in the plot

## Notes and troubleshooting

- If you see a DLL load error, confirm `ps5000a.dll` is installed and matches your Python bitness (use 64-bit DLL with 64-bit Python).
- If the scope requires external power and it is not connected, the driver can return a power related error when opening the unit. Connect the PSU if your model requires it.
- This example uses only Channels A and B.

## License

This is an example script. You can adapt it for your lab usage.
