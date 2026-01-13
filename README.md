
# PicoScope 5000B Streaming Viewer (Python, Windows 11)

This app connects to PicoScope 5000 series hardware using the `ps5000a` driver (PicoSDK), streams Channels A and B, and plots live data in a PyQt5 GUI with an embedded Matplotlib plot. It uses the `ps5000aRunStreaming` + `ps5000aGetStreamingLatestValues` callback pattern.

## Overview

- Opens the scope, configures Channels A and B, and starts streaming automatically on launch.
- Requests a sampling interval in nanoseconds (ns). Default UI selection is 1 µs (1,000 ns). The code automatically adjusts to the device’s minimum supported timebase when needed.
- Keeps a rolling window buffer (default 20 ms) and refreshes the plot every 20 ms.
- Normalizes the plotted amplitude by the selected full-scale range for each channel (y-axis spans ±0.5 by default).

## Modules

- [picoscope_5000.py](picoscope_5000.py): Main UI/controller. Builds the PyQt5 interface, wires callbacks, formats the time axis, updates the plot, and manages cursor readouts via the plot widget. Delegates hardware actions to the streamer.
- [picoscope_driver.py](picoscope_driver.py): Hardware streaming driver wrapper over PicoSDK (`ps5000a.dll`). Opens/closes the device, configures channels/ranges, applies trigger, and streams data into ring buffers. Exposes `StreamConfig` and `PicoScopeStreamer`.
- [plotter.py](plotter.py): Plotting and cursor management. Embeds Matplotlib in a Qt widget, renders channels A/B, and provides two X cursors and two Y cursors with movement and readouts.
- [picoscope_constants.py](picoscope_constants.py): Centralized PicoSDK enums, range maps/labels, and status codes. Also loads optional status text overrides from JSON.
- [pico_status_dict.json](pico_status_dict.json): Optional map of Pico status codes to human-readable strings; merged into the defaults on startup.
- [requirements.txt](requirements.txt): Python dependencies for the app.

## Requirements

### PicoSDK and DLLs

Install Pico Technology software that provides the `ps5000a` driver.

Needed:
- `ps5000a.dll` (device driver)
- PicoScope USB drivers (installed with PicoScope/PicoSDK)
- Some installations also require `picoipp.dll` to be discoverable via PATH or in the same directory.

The program loads `ps5000a.dll` via `ctypes.WinDLL()`. It tries common install locations and respects an environment variable override.

Common DLL locations the app checks:
- `C:\Program Files\Pico Technology\SDK\lib\ps5000a.dll`
- `C:\Program Files\Pico Technology\PicoScope 7 T&M Stable\ps5000a.dll`
- `C:\Program Files\Pico Technology\PicoScope 6\ps5000a.dll`
- 32-bit variants under `C:\Program Files (x86)\...`

Override via environment variable (PowerShell):
```powershell
setx PICO_PS5000A_DLL "C:\full\path\to\ps5000a.dll"
```
Restart the terminal after setting.

### Python

- Python 3.10+ (64-bit) on Windows 11

### Python packages

- numpy
- PyQt5
- matplotlib

Install with:
```powershell
python -m pip install -r requirements.txt
```

## Run

```powershell
python picoscope_5000.py
```

On startup the app attempts to open the first available scope, configures it, and starts streaming.

## UI Controls

- Channel ranges: Two combo boxes set `A` and `B` voltage ranges from 10 mV to 20 V.
- Sampling rate: A combo box (50 ns … 10 µs) plus “Apply Rate” button. The actual interval may adjust to the device minimum; the status bar shows the active ns value.
- Trigger: Checkbox enables/disables a simple rising-edge trigger. Enter level in volts; it converts to device counts based on the selected range.
- Timebase window: Buttons `−` and `+` step the rolling window through predefined durations (10 µs … 10 ms). A spin box allows precise window control (0.010 ms … 10.000 ms).

## Configuration (Driver `StreamConfig`)

Edit `StreamConfig` in [picoscope_driver.py](picoscope_driver.py) for defaults:
- `sample_interval_ns`: requested sample interval in ns (default 1000 ns = 1 µs)
- `plot_refresh_ms`: GUI refresh period (default 20 ms)
- `plot_window_ms`: rolling window duration (default 20.0 ms)
- `plot_max_points`: decimation target for plotting (default 6000)
- `range_a`, `range_b`: channel ranges (default 2 V)
- `coupling`: `AC` or `DC` (default DC)
- `resolution`: device resolution enum (default 8-bit)
- `driver_buffer_size`: driver overview buffer size (default 200,000 samples)
- `connect_delay_ms`: delay after open before acquisitions (default 1000 ms)
- `simple_trigger_enabled`: off by default; see Trigger controls
- `trigger_source`, `trigger_threshold_pct`, `trigger_direction`: trigger configuration

## Internal Behavior

- Uses `ps5000aSetDataBuffer` (raw ADC counts) and converts to volts on the fly using the selected range.
- Maintains ring buffers per channel sized to the current window, and decimates to `plot_max_points` for efficient plotting.
- Automatically checks minimum achievable timebase via `ps5000aGetMinimumTimebaseStateless` and adjusts if a requested interval is too fast.
- Attempts to preload `picoipp.dll` from the DLL directory; also adds common PicoSDK folders via `os.add_dll_directory` when available.

## Troubleshooting

- DLL not found: ensure `ps5000a.dll` is installed and matches Python bitness (use 64-bit DLL with 64-bit Python). Set `PICO_PS5000A_DLL` if installed to a non-standard path.
- Missing `picoipp.dll`: install PicoSDK and ensure its `lib` folders are on PATH or in the same directory as `ps5000a.dll`.
- Power source messages: some models require acknowledging power status. The app handles `ps5000aChangePowerSource` based on the code returned by `ps5000aOpenUnit`.

## License

Example code for lab/engineering usage.
