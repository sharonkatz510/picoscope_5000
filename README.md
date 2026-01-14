
# PicoScope 5000B Rapid Block Viewer (Python, Windows 11)

This app connects to PicoScope 5000 series hardware using the `ps5000a` driver (PicoSDK), acquires rapid block captures for Channels A and B, and renders them in a PyQt5 GUI with an embedded Matplotlib plot. It provides trigger level control, timebase window steps, cursors, and a file recording feature.

## Overview

- Opens the scope, configures Channels A and B, and starts rapid block acquisition automatically on launch.
- Requests a sampling interval in nanoseconds (ns). Default is 100 ns (10 MHz). The code automatically adjusts to the device’s minimum supported timebase when needed.
- Uses a fixed capture length derived from the window and sampling interval; refreshes the plot every 20 ms.
- Normalizes the plotted amplitude by the selected full-scale range for each channel (y-axis spans ±0.5 by default) and shows a trigger indicator line.

## Modules

- [main.py](main.py): Main UI/controller. Builds the PyQt5 interface, wires callbacks, formats the time axis, updates the plot, manages cursor readouts via the plot widget, and includes recording controls.
- [driver.py](driver.py): Hardware rapid block driver wrapper over PicoSDK (`ps5000a.dll`). Opens/closes the device, configures channels/ranges, applies trigger, and acquires block captures for plotting and recording. Exposes `BlockConfig` and `PicoScopeRapidBlock`.
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
python main.py
```

On startup the app attempts to open the first available scope, configures it, and starts rapid block acquisition.

## UI Controls

- Channel ranges: Two combo boxes set `A` and `B` voltage ranges from 10 mV to 20 V.
- Sampling rate: A combo box (100 ns … 5 µs) plus “Apply Rate” button. The actual interval may adjust to the device minimum; the status bar shows the active ns value.
- Trigger: Always enabled; controlled by the trigger level (in volts). A dashed line indicates the current level on the plot.
- Timebase window: Buttons `−` and `+` step the window through predefined durations (10 µs … 10 ms). A spin box allows precise control (0.010 ms … 50.000 ms).
- Cursors: Two vertical (time) and two horizontal (amplitude) cursors with delta readouts; keyboard arrows nudge the selected cursor.
- Recording: Buttons for “Choose Location”, “Start Rec”, and “Stop Rec”. While recording, the UI shows an overlay and pauses plot refresh; acquisitions are saved to disk.

## Configuration (Driver `BlockConfig`)

Edit `BlockConfig` in [driver.py](driver.py) for defaults:
- `sample_interval_ns`: requested sample interval in ns (default 100 ns)
- `plot_refresh_ms`: GUI refresh period (default 20 ms)
- `plot_window_ms`: window duration for block acquisitions (default 10.0 ms)
- `plot_max_points`: max points for plotting/decimation (default 5000)
- `range_a`, `range_b`: channel ranges (set by UI on startup)
- `coupling`: `AC` or `DC` (default AC)
- `resolution`: device resolution enum (default 8-bit)
- `driver_buffer_size`: driver buffer size for block captures
- `connect_delay_ms`: delay after open before first acquisition
- `simple_trigger_enabled`: trigger control (enabled by default)
- `trigger_source`, `trigger_threshold_pct`, `trigger_direction`: trigger configuration

## Internal Behavior

- Uses `ps5000aSetDataBuffer` (raw ADC counts) and converts to volts on the fly using the selected range.
- Acquires block captures sized to the current window and sampling interval; decimates for efficient plotting when needed.
- Checks achievable timebase via `ps5000aGetTimebase2` and adjusts if a requested interval is too fast.
- Attempts to preload `picoipp.dll` from the DLL directory; also adds common PicoSDK folders via `os.add_dll_directory` when available.

## Recording

- Choose a destination folder, press “Start Rec” to begin saving acquisitions; press “Stop Rec” to finish.
- While recording, the plot pauses and shows a dark overlay “Recording in progress”.
- Files are named `acq_001.bin`, `acq_002.bin`, … in the chosen folder.
- Format: Channel A samples followed by Channel B samples; both as `float16` volts (older builds used `float32`).
- Metadata file `metadata.txt` is written on stop with: `started_at` (ISO), `sampling_frequency_hz`, `frame_rate_hz`, and `acquisitions_saved`.
- Load saved files in Python:
	```python
	from bin_reader import read_acq_bin
	a, b = read_acq_bin(r"C:\path\to\acq_001.bin")          # float16 by default
	a32, b32 = read_acq_bin(r"C:\path\to\old.bin", dtype="float32")
	```

## Troubleshooting

- DLL not found: ensure `ps5000a.dll` is installed and matches Python bitness (use 64-bit DLL with 64-bit Python). Set `PICO_PS5000A_DLL` if installed to a non-standard path.
- Missing `picoipp.dll`: install PicoSDK and ensure its `lib` folders are on PATH or in the same directory as `ps5000a.dll`.
- Power source messages: some models require acknowledging power status. The app handles `ps5000aChangePowerSource` based on the code returned by `ps5000aOpenUnit`.

## License

Example code for lab/engineering usage.
