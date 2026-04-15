# Zwoosh GUI Design Spec

## Overview

Add an interactive Windows desktop GUI to the Zwoosh BLE bridge using PySide6. The GUI provides a user-friendly way to connect/disconnect Zwift Click V2 devices, view live logs, edit configuration, and see device battery levels. Packaged as a standalone `.exe` via PyInstaller.

## Architecture

Refactor into three files with clean separation:

### `core.py` — BLE Engine

Extracted from current `zwoosh.py`. Contains `ZwooshCore` class that runs BLE operations on a background thread with its own asyncio event loop.

**Public API:**
- `start()` — Begin scanning and connecting (non-blocking, spins up background thread)
- `stop()` — Disconnect all devices and shut down the event loop
- `is_running` — Property indicating if the bridge is active

**Callbacks (set by consumer before calling `start()`):**
- `on_log(timestamp: str, level: str, source: str, message: str)` — All log output
- `on_device_found(name: str, address: str, rssi: int)` — Device discovered during scan
- `on_connected(label: str, address: str, battery: int | None)` — Device connected
- `on_disconnected(label: str)` — Device disconnected
- `on_shift(label: str, direction: str, gear: int)` — Button press translated to shift
- `on_state_changed(state: str)` — One of: "disconnected", "scanning", "connected"

The core loads config from `config.json` via the existing `load_config()`. A `reload_config(cfg: dict)` method allows the GUI to push updated settings and persist them.

### `gui.py` — PySide6 Desktop Application

Main window (`ZwooshWindow`) with:

**Status bar (always visible at top):**
- Colored dot: green (connected), red (disconnected), pulsing orange (scanning)
- Text: "Connected — 2 devices" / "Disconnected" / "Scanning..."

**Device cards (visible when devices are connected):**
- One card per connected device showing: name, BLE address, battery percentage
- Cards appear/disappear dynamically as devices connect/disconnect

**Action buttons:**
- Disconnected state: "Connect" button (primary orange)
- Scanning state: "Connect" disabled + "Cancel" button
- Connected state: "Disconnect" button (red) + "Clear Logs" button

**Tabbed panel:**

*Logs tab:*
- Scrollable monospace log panel with colored entries (timestamps grey, info blue, success green, warnings yellow, errors red, shift events orange bold)
- Auto-scrolls to bottom on new entries
- "Clear Logs" button clears the panel

*Settings tab:*
- **Key Mappings section:** Shift Up Key, Shift Down Key (text inputs)
- **Connection section:** Device Name (text), Scan Timeout in seconds (number), Reconnect Delay in seconds (number)
- **Auto Reconnect** toggle switch
- "Save" button persists to `config.json` and reloads core config
- "Reset Defaults" button restores `DEFAULT_CONFIG` values into the form

**Thread safety:** Core callbacks arrive from the asyncio background thread. The GUI uses a Qt signal bridge — a `QObject` subclass with signals matching each callback. Core callbacks emit these signals, and the GUI connects slots to them. All UI updates happen on the Qt main thread.

### `zwoosh.py` — CLI Entry Point (preserved)

Retains current CLI behavior by importing `ZwooshCore` from `core.py`. Existing `--scan`, `--test`, `--debug`, `--help` flags continue to work. The `ZwooshBridge` class is removed; CLI uses `ZwooshCore` with print-based callbacks.

## System Tray

- Closing the window (X button) minimizes to system tray instead of quitting
- Tray icon indicates connection state (different icons or colored overlay)
- **Right-click tray menu:** "Show Window", "Connect" / "Disconnect" (toggles based on state), separator, "Quit"
- Double-click tray icon restores the window
- "Quit" from tray menu fully exits the application

## Visual Style

- Dark theme matching Zwift/MyWhoosh aesthetic
- Background: dark navy (#1a1a2e), panels: darker (#0a0a1a), cards: (#0f3460)
- Accent color: orange (#ff6b35) for active tabs, primary buttons, shift events
- Applied via Qt stylesheet on the application level
- Fixed window size (no need for complex responsive layout)

## Connection States

| State | Status Dot | Status Text | Buttons | Device Cards |
|-------|-----------|-------------|---------|--------------|
| Disconnected | Red | "Disconnected" | Connect | Hidden |
| Scanning | Pulsing orange | "Scanning..." | Connect (disabled) + Cancel | Hidden |
| Connected | Green | "Connected — N devices" | Disconnect + Clear Logs | Shown |

## Config Persistence

Settings tab edits are written to `config.json` on Save. The core's `reload_config()` is called so changes take effect on the next connection cycle. No restart required.

## Packaging

- **PyInstaller** single-file `.exe` with `--windowed` (no console)
- `build.bat` script: `pyinstaller --onefile --windowed --name Zwoosh --icon=assets/icon.ico gui.py`
- App icon stored in `assets/icon.ico`
- Dependencies added to `requirements.txt`: `PySide6`

## File Structure After Implementation

```
zwoosh/
  core.py          # BLE engine (extracted from zwoosh.py)
  gui.py           # PySide6 desktop application
  zwoosh.py        # CLI entry point (imports core.py)
  config.json      # User configuration
  requirements.txt # bleak, pynput, PySide6
  build.bat        # PyInstaller build script
  assets/
    icon.ico       # App icon for window + tray + exe
```
