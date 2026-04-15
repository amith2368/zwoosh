# Zwoosh GUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a PySide6 desktop GUI to the Zwoosh BLE bridge, packaged as a standalone Windows .exe.

**Architecture:** Extract BLE logic from `zwoosh.py` into `core.py` with callback-based API. Build `gui.py` (PySide6) that drives the core from a background thread and updates UI via Qt signals. Preserve CLI in `zwoosh.py` by importing core.

**Tech Stack:** Python 3, PySide6, bleak, pynput, PyInstaller

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `core.py` | Create | BLE engine with callback API, asyncio on background thread |
| `gui.py` | Create | PySide6 main window, system tray, dark theme |
| `zwoosh.py` | Rewrite | CLI entry point, imports `core.py` |
| `requirements.txt` | Modify | Add PySide6, pyinstaller |
| `build.bat` | Create | PyInstaller build command |
| `assets/icon.ico` | Create | App icon (generated programmatically) |

---

### Task 1: Extract `core.py` — Config and Constants

Extract all shared constants, config loading, and key mapping from `zwoosh.py` into `core.py`.

**Files:**
- Create: `core.py`

- [ ] **Step 1: Create `core.py` with config and constants**

```python
#!/usr/bin/env python3
"""Zwoosh BLE core — Zwift Click V2 protocol engine with callback API."""

import asyncio
import json
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from pynput.keyboard import Controller, Key

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULT_CONFIG = {
    "device_name": "Zwift Click",
    "shift_up_key": "=",
    "shift_down_key": "-",
    "scan_timeout": 15,
    "reconnect_delay": 3,
    "auto_reconnect": True,
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        return {**DEFAULT_CONFIG, **cfg}
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=4)


# ---------------------------------------------------------------------------
# Key mapping helpers
# ---------------------------------------------------------------------------

SPECIAL_KEYS = {
    "up": Key.up,
    "down": Key.down,
    "left": Key.left,
    "right": Key.right,
    "space": Key.space,
    "enter": Key.enter,
    "tab": Key.tab,
    "page_up": Key.page_up,
    "page_down": Key.page_down,
}


def resolve_key(key_str: str):
    """Turn a config string into a pynput key."""
    lower = key_str.lower().strip()
    if lower in SPECIAL_KEYS:
        return SPECIAL_KEYS[lower]
    if len(key_str) == 1:
        return key_str
    raise ValueError(
        f"Unknown key '{key_str}'. Use a single character or one of: "
        f"{', '.join(SPECIAL_KEYS)}"
    )


# ---------------------------------------------------------------------------
# BLE - Zwift Click V2 protocol constants
# ---------------------------------------------------------------------------

ZWIFT_CLICK_BTN_UUID  = "00000002-19ca-4651-86e5-fa29dcdd09d1"
ZWIFT_CLICK_CMD_UUID  = "00000003-19ca-4651-86e5-fa29dcdd09d1"
ZWIFT_CLICK_INFO_UUID = "00000004-19ca-4651-86e5-fa29dcdd09d1"
ZWIFT_CLICK_CH0100    = "00000100-19ca-4651-86e5-fa29dcdd09d1"
ZWIFT_CLICK_CH0101    = "00000101-19ca-4651-86e5-fa29dcdd09d1"
ZWIFT_CLICK_CH0102    = "00000102-19ca-4651-86e5-fa29dcdd09d1"
BATTERY_LEVEL_UUID    = "00002a19-0000-1000-8000-00805f9b34fb"

RIDEON_CMD = b"RideOn"
UNLOCK_CMD = bytes([0x00, 0x09, 0x00])

BTN_MSG_TYPE = 0x23
BTN_MSG_LEN = 7
BTN_STATE_BYTE = 3
BTN_A_MASK = 0x20
BTN_B_MASK = 0x02
DEBOUNCE_SECS = 0.25
```

- [ ] **Step 2: Verify the file is syntactically valid**

Run: `python -c "import core; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add core.py
git commit -m "refactor: extract config, constants, and key helpers into core.py"
```

---

### Task 2: Extract `core.py` — ZwooshCore Class

Add the `ZwooshCore` class to `core.py` with callback-based API and background thread management.

**Files:**
- Modify: `core.py`

- [ ] **Step 1: Add `ZwooshCore` class to `core.py`**

Append the following after the constants section:

```python
# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------

class ZwooshCore:
    """BLE bridge engine with callback-based API.

    Runs BLE operations on a background thread with its own asyncio event loop.
    Set callback attributes before calling start().
    """

    def __init__(self, config: dict | None = None):
        self.cfg = config or load_config()
        self.kb = Controller()
        self.key_up = resolve_key(self.cfg["shift_up_key"])
        self.key_down = resolve_key(self.cfg["shift_down_key"])
        self._running = False
        self._gear = 0
        self._prev_buttons: dict[str, int] = {}
        self._last_press: dict[int, float] = {}
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        # Callbacks — set these before calling start()
        self.on_log: Callable[[str, str, str, str], None] | None = None
        self.on_device_found: Callable[[str, str, int], None] | None = None
        self.on_connected: Callable[[str, str, int | None], None] | None = None
        self.on_disconnected: Callable[[str], None] | None = None
        self.on_shift: Callable[[str, str, int], None] | None = None
        self.on_state_changed: Callable[[str], None] | None = None

    def _log(self, level: str, source: str, message: str):
        ts = time.strftime("%H:%M:%S")
        if self.on_log:
            self.on_log(ts, level, source, message)

    def _set_state(self, state: str):
        if self.on_state_changed:
            self.on_state_changed(state)

    @property
    def is_running(self) -> bool:
        return self._running

    def reload_config(self, cfg: dict):
        """Update config and persist to disk. Takes effect on next connection cycle."""
        self.cfg = {**DEFAULT_CONFIG, **cfg}
        save_config(self.cfg)
        try:
            self.key_up = resolve_key(self.cfg["shift_up_key"])
            self.key_down = resolve_key(self.cfg["shift_down_key"])
        except ValueError as e:
            self._log("error", "Config", str(e))

    # ---- keyboard output ----

    def _press_key(self, key, direction: str, device_label: str):
        self.kb.press(key)
        self.kb.release(key)
        if direction == "up":
            self._gear += 1
        else:
            self._gear -= 1
        self._log("shift", device_label, f"Shift {direction} (gear: {self._gear:+d})")
        if self.on_shift:
            self.on_shift(device_label, direction, self._gear)

    # ---- debounce ----

    def _debounced(self, mask: int) -> bool:
        now = time.monotonic()
        last = self._last_press.get(mask, 0.0)
        if now - last < DEBOUNCE_SECS:
            return False
        self._last_press[mask] = now
        return True

    # ---- button handler ----

    def _make_button_handler(self, device_label: str):
        self._prev_buttons[device_label] = BTN_A_MASK | BTN_B_MASK

        def on_notify(_char: BleakGATTCharacteristic, data: bytearray):
            if len(data) != BTN_MSG_LEN or data[0] != BTN_MSG_TYPE:
                return
            btn_byte = data[BTN_STATE_BYTE]
            prev = self._prev_buttons[device_label]
            newly_pressed = prev & ~btn_byte

            if newly_pressed & BTN_A_MASK and self._debounced(BTN_A_MASK):
                self._press_key(self.key_up, "up", device_label)
            if newly_pressed & BTN_B_MASK and self._debounced(BTN_B_MASK):
                self._press_key(self.key_down, "down", device_label)

            self._prev_buttons[device_label] = btn_byte

        return on_notify

    # ---- scan ----

    async def _find_devices(self) -> list:
        target = self.cfg["device_name"]
        timeout = self.cfg["scan_timeout"]
        self._log("info", "Scan", f"Scanning for '{target}' (timeout {timeout}s) ...")
        self._set_state("scanning")

        found = []
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)

        named = [(dev, adv) for dev, adv in discovered.values() if dev.name]
        for dev, adv in sorted(named, key=lambda x: x[1].rssi or -999, reverse=True):
            is_match = dev.name and target.lower() in dev.name.lower()
            if is_match:
                found.append(dev)
            if self.on_device_found and dev.name:
                self.on_device_found(dev.name, dev.address, adv.rssi or -999)

        if not found:
            for dev, adv in discovered.values():
                if dev.name and "zwift" in dev.name.lower() and "click" in dev.name.lower():
                    found.append(dev)

        self._log("info", "Scan", f"Found {len(found)} Zwift Click device(s)")
        return found

    # ---- handshake ----

    async def _handshake(self, client, label: str):
        noop = lambda _c, _d: None
        for uuid in [ZWIFT_CLICK_INFO_UUID, ZWIFT_CLICK_CH0100,
                     ZWIFT_CLICK_CH0101, ZWIFT_CLICK_CH0102]:
            try:
                await client.start_notify(uuid, noop)
            except Exception:
                pass

        try:
            await client.write_gatt_char(ZWIFT_CLICK_CMD_UUID, RIDEON_CMD, response=False)
            await asyncio.sleep(0.3)
        except Exception as exc:
            self._log("warn", label, f"RideOn write failed: {exc}")

        try:
            await client.write_gatt_char(ZWIFT_CLICK_CH0100, UNLOCK_CMD, response=False)
            await asyncio.sleep(0.5)
        except Exception as exc:
            self._log("warn", label, f"Unlock write failed: {exc}")

        self._log("info", label, "Handshake complete")

    # ---- connect to a single device ----

    async def _connect_one(self, device):
        short_addr = device.address[-5:].replace(":", "")
        label = f"Click {short_addr}"
        self._log("info", label, f"Connecting to {device.address} ...")

        async with BleakClient(device, timeout=15.0) as client:
            self._log("ok", label, "Connected!")

            battery = None
            try:
                battery_data = await client.read_gatt_char(BATTERY_LEVEL_UUID)
                battery = battery_data[0]
                self._log("info", label, f"Battery: {battery}%")
            except Exception:
                pass

            if self.on_connected:
                self.on_connected(label, device.address, battery)

            handler = self._make_button_handler(label)
            await client.start_notify(ZWIFT_CLICK_BTN_UUID, handler)
            await self._handshake(client, label)
            self._log("info", label, "Listening for button events")

            while client.is_connected and self._running:
                await asyncio.sleep(0.5)

            self._log("info", label, "Disconnected")
            if self.on_disconnected:
                self.on_disconnected(label)

    async def _run_one_with_reconnect(self, device):
        short_addr = device.address[-5:].replace(":", "")
        label = f"Click {short_addr}"
        while self._running:
            try:
                await self._connect_one(device)
            except Exception as exc:
                self._log("error", label, str(exc))
            if not self._running or not self.cfg["auto_reconnect"]:
                break
            delay = self.cfg["reconnect_delay"]
            self._log("info", label, f"Reconnecting in {delay}s ...")
            await asyncio.sleep(delay)

    # ---- main loop ----

    async def _run(self):
        while self._running:
            devices = await self._find_devices()

            if not devices:
                if not self.cfg["auto_reconnect"]:
                    break
                delay = self.cfg["reconnect_delay"]
                self._log("info", "Scan", f"No devices found. Retrying in {delay}s ...")
                self._set_state("disconnected")
                await asyncio.sleep(delay)
                continue

            self._set_state("connected")
            tasks = [
                asyncio.create_task(self._run_one_with_reconnect(dev))
                for dev in devices
            ]
            self._log("info", "Bridge", f"Bridging {len(devices)} device(s)")
            await asyncio.gather(*tasks)

            if not self._running or not self.cfg["auto_reconnect"]:
                break
            delay = self.cfg["reconnect_delay"]
            self._log("info", "Bridge", f"All disconnected. Reconnecting in {delay}s ...")
            self._set_state("disconnected")
            await asyncio.sleep(delay)

        self._set_state("disconnected")

    def _thread_target(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        finally:
            self._loop.close()
            self._loop = None

    def start(self):
        """Start scanning and connecting in a background thread."""
        if self._running:
            return
        self._running = True
        self._gear = 0
        self._prev_buttons.clear()
        self._last_press.clear()
        self._thread = threading.Thread(target=self._thread_target, daemon=True)
        self._thread.start()

    def stop(self):
        """Signal the engine to stop. Returns immediately; thread winds down."""
        self._running = False
```

- [ ] **Step 2: Verify the file is syntactically valid**

Run: `python -c "from core import ZwooshCore; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add core.py
git commit -m "feat: add ZwooshCore class with callback API and background thread"
```

---

### Task 3: Rewrite `zwoosh.py` as CLI Wrapper

Replace the monolithic `zwoosh.py` with a thin CLI that imports from `core.py`.

**Files:**
- Rewrite: `zwoosh.py`

- [ ] **Step 1: Rewrite `zwoosh.py`**

```python
#!/usr/bin/env python3
"""Zwoosh CLI — Zwift Click V2 to MyWhoosh Virtual Shifting Bridge."""

import asyncio
import signal
import sys

from bleak import BleakClient, BleakScanner

from core import (
    ZwooshCore, load_config, BATTERY_LEVEL_UUID,
    ZWIFT_CLICK_BTN_UUID, ZWIFT_CLICK_CMD_UUID, ZWIFT_CLICK_INFO_UUID,
    ZWIFT_CLICK_CH0100, ZWIFT_CLICK_CH0101, ZWIFT_CLICK_CH0102,
    RIDEON_CMD, UNLOCK_CMD, BTN_MSG_TYPE, BTN_MSG_LEN, BTN_STATE_BYTE,
    BTN_A_MASK, BTN_B_MASK, DEBOUNCE_SECS, CONFIG_PATH,
)
from bleak.backends.characteristic import BleakGATTCharacteristic


# ---------------------------------------------------------------------------
# Scan-only mode (standalone, not part of core)
# ---------------------------------------------------------------------------

async def scan_and_dump(timeout: int = 15):
    """Scan for all BLE devices and dump GATT services for Zwift Clicks."""
    print(f"Scanning for all BLE devices ({timeout}s) ...\n")
    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)

    clicks = []
    others = []
    for dev, adv in discovered.values():
        if dev.name and "zwift" in dev.name.lower():
            clicks.append((dev, adv))
        elif dev.name:
            others.append((dev, adv))

    if clicks:
        print(f"Zwift devices found ({len(clicks)}):")
        for dev, adv in clicks:
            print(f"  * {dev.name}  [{dev.address}]  RSSI: {adv.rssi}")
    else:
        print("No Zwift devices found.")

    if others:
        print(f"\nOther named BLE devices ({len(others)}):")
        for dev, adv in sorted(others, key=lambda x: x[1].rssi or -999, reverse=True)[:15]:
            print(f"  - {dev.name}  [{dev.address}]  RSSI: {adv.rssi}")

    for d, _adv in clicks:
        print(f"\n{'=' * 50}")
        print(f"GATT dump: {d.name}  [{d.address}]")
        print("=" * 50)
        try:
            async with BleakClient(d, timeout=15.0) as client:
                try:
                    battery_data = await client.read_gatt_char(BATTERY_LEVEL_UUID)
                    print(f"\n  Battery: {battery_data[0]}%")
                except Exception:
                    pass
                for svc in client.services:
                    print(f"\n  Service: {svc.uuid}")
                    for char in svc.characteristics:
                        props = ", ".join(char.properties)
                        print(f"    Char: {char.uuid}  [{props}]")
        except Exception as exc:
            print(f"  Could not connect: {exc}")

    print("\nDone.")


# ---------------------------------------------------------------------------
# Debug listen (standalone)
# ---------------------------------------------------------------------------

async def debug_listen(device):
    """Connect to a device and print all raw BLE data."""
    short_addr = device.address[-5:].replace(":", "")
    label = f"Click {short_addr}"
    print(f"  [{label}] Connecting to {device.address} ...")

    async with BleakClient(device, timeout=15.0) as client:
        print(f"  [{label}] Connected! DEBUG MODE")

        print(f"  [{label}] Reading all readable characteristics ...")
        for svc in client.services:
            for char in svc.characteristics:
                if "read" in char.properties:
                    try:
                        data = await client.read_gatt_char(char)
                        short = char.uuid.split("-")[0]
                        if data:
                            printable = data.decode("utf-8", errors="replace")
                            print(f"  [{label}]   {short}: {data.hex()} = {list(data)}  '{printable}'")
                        else:
                            print(f"  [{label}]   {short}: (empty)")
                    except Exception as exc:
                        short = char.uuid.split("-")[0]
                        print(f"  [{label}]   {short}: READ FAILED: {exc}")

        print(f"  [{label}] Subscribing to all notify/indicate characteristics ...")
        for svc in client.services:
            for char in svc.characteristics:
                if "notify" in char.properties or "indicate" in char.properties:
                    def make_debug_handler(cuuid):
                        short = cuuid.split("-")[0]
                        def handler(_c, data: bytearray):
                            print(f"  [{label}] << {short}: {data.hex()} = {list(data)}")
                        return handler
                    try:
                        await client.start_notify(char, make_debug_handler(char.uuid))
                        short = char.uuid.split("-")[0]
                        print(f"  [{label}]   Subscribed: {short}")
                    except Exception as exc:
                        short = char.uuid.split("-")[0]
                        print(f"  [{label}]   FAILED: {short}: {exc}")

        print(f"  [{label}] Sending unlock (000900 -> char 0100) ...")
        try:
            await client.write_gatt_char(ZWIFT_CLICK_CH0100, UNLOCK_CMD, response=False)
            print(f"  [{label}] Unlock sent.")
        except Exception as exc:
            print(f"  [{label}] Unlock failed: {exc}")

        print(f"\n  [{label}] Press buttons now (Ctrl+C to quit).\n")
        while client.is_connected:
            await asyncio.sleep(0.5)
        print(f"  [{label}] Disconnected.")


# ---------------------------------------------------------------------------
# Test listen (standalone)
# ---------------------------------------------------------------------------

async def test_listen(device):
    """Connect and log button presses without sending keyboard events."""
    short_addr = device.address[-5:].replace(":", "")
    label = f"Click {short_addr}"
    print(f"  [{label}] Connecting to {device.address} ...")

    async with BleakClient(device, timeout=15.0) as client:
        print(f"  [{label}] Connected!")

        prev = BTN_A_MASK | BTN_B_MASK
        last_press: dict[int, float] = {}

        def debounced(mask: int) -> bool:
            now = time.monotonic()
            last = last_press.get(mask, 0.0)
            if now - last < DEBOUNCE_SECS:
                return False
            last_press[mask] = now
            return True

        def on_notify(_char: BleakGATTCharacteristic, data: bytearray):
            nonlocal prev
            if len(data) != BTN_MSG_LEN or data[0] != BTN_MSG_TYPE:
                return
            btn_byte = data[BTN_STATE_BYTE]
            newly_pressed = prev & ~btn_byte
            newly_released = ~prev & btn_byte

            if newly_pressed & BTN_A_MASK and debounced(BTN_A_MASK):
                print(f"  [{label}] Button A PRESSED  (byte[3]=0x{btn_byte:02X}, mask=0x20)")
            if newly_released & BTN_A_MASK:
                print(f"  [{label}] Button A released (byte[3]=0x{btn_byte:02X})")
            if newly_pressed & BTN_B_MASK and debounced(BTN_B_MASK):
                print(f"  [{label}] Button B PRESSED  (byte[3]=0x{btn_byte:02X}, mask=0x02)")
            if newly_released & BTN_B_MASK:
                print(f"  [{label}] Button B released (byte[3]=0x{btn_byte:02X})")
            prev = btn_byte

        await client.start_notify(ZWIFT_CLICK_BTN_UUID, on_notify)

        # Handshake
        noop = lambda _c, _d: None
        for uuid in [ZWIFT_CLICK_INFO_UUID, ZWIFT_CLICK_CH0100,
                     ZWIFT_CLICK_CH0101, ZWIFT_CLICK_CH0102]:
            try:
                await client.start_notify(uuid, noop)
            except Exception:
                pass
        try:
            await client.write_gatt_char(ZWIFT_CLICK_CMD_UUID, RIDEON_CMD, response=False)
            await asyncio.sleep(0.3)
        except Exception:
            pass
        try:
            await client.write_gatt_char(ZWIFT_CLICK_CH0100, UNLOCK_CMD, response=False)
            await asyncio.sleep(0.5)
        except Exception:
            pass

        print(f"  [{label}] TEST MODE - no keyboard output.")
        print(f"  [{label}] Press buttons to see which is A and which is B.\n")
        while client.is_connected:
            await asyncio.sleep(0.5)
        print(f"  [{label}] Disconnected.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

import time


def main():
    config = load_config()

    if "--scan" in sys.argv:
        asyncio.run(scan_and_dump(config["scan_timeout"]))
        return

    if "--help" in sys.argv or "-h" in sys.argv:
        print("Zwoosh - Zwift Click V2 -> MyWhoosh Virtual Shifting Bridge\n")
        print("Usage:")
        print("  python zwoosh.py           Connect and start bridging")
        print("  python zwoosh.py --scan    Scan BLE and dump GATT services")
        print("  python zwoosh.py --test    Test button detection (no keyboard)")
        print("  python zwoosh.py --debug   Connect and print raw BLE data")
        print(f"\nConfig: {CONFIG_PATH}")
        print(f"  device_name:    '{config['device_name']}'")
        print(f"  shift_up_key:   '{config['shift_up_key']}'")
        print(f"  shift_down_key: '{config['shift_down_key']}'")
        return

    debug = "--debug" in sys.argv
    test_mode = "--test" in sys.argv

    print("=" * 50)
    print("  Zwoosh - Zwift Click V2 -> MyWhoosh Bridge")
    print("=" * 50)
    if test_mode:
        print("  MODE: TEST (log buttons, no keyboard)")
    elif debug:
        print("  MODE: DEBUG (raw BLE data)")
    print(f"  Device name:  '{config['device_name']}'")
    print(f"  Shift up   -> key '{config['shift_up_key']}'")
    print(f"  Shift down -> key '{config['shift_down_key']}'")
    print()

    if debug or test_mode:
        # Debug/test modes: find devices, then run standalone listeners
        async def _run_special():
            discovered = await BleakScanner.discover(
                timeout=config["scan_timeout"], return_adv=True
            )
            devices = []
            target = config["device_name"]
            for dev, adv in discovered.values():
                if dev.name and target.lower() in dev.name.lower():
                    devices.append(dev)
            if not devices:
                print("No Zwift Click devices found.")
                return
            print(f"Found {len(devices)} device(s)\n")
            handler = debug_listen if debug else test_listen
            await asyncio.gather(*[handler(d) for d in devices])

        loop = asyncio.new_event_loop()
        loop.run_until_complete(_run_special())
        loop.close()
        return

    # Normal mode: use ZwooshCore with print callbacks
    core = ZwooshCore(config)
    core.on_log = lambda ts, level, source, msg: print(f"  {ts} [{source}] {msg}")

    def _sigint(*_):
        print("\nStopping ...")
        core.stop()

    signal.signal(signal.SIGINT, _sigint)

    print("  Starting bridge (Ctrl+C to quit) ...\n")
    core.start()

    # Block main thread until core stops
    try:
        while core.is_running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        core.stop()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify CLI help works**

Run: `python zwoosh.py --help`
Expected: Help text prints without errors

- [ ] **Step 3: Commit**

```bash
git add zwoosh.py
git commit -m "refactor: rewrite zwoosh.py as thin CLI wrapper around core.py"
```

---

### Task 4: Update `requirements.txt`

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Update requirements.txt**

```
bleak>=0.21.0
pynput>=1.7.6
PySide6>=6.6.0
pyinstaller>=6.0.0
```

- [ ] **Step 2: Install new dependencies**

Run: `.venv/Scripts/pip install PySide6 pyinstaller`
Expected: Successful installation

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "deps: add PySide6 and pyinstaller"
```

---

### Task 5: Create `gui.py` — Application Skeleton and Dark Theme

Set up the PySide6 application, main window shell, and dark stylesheet.

**Files:**
- Create: `gui.py`

- [ ] **Step 1: Create `gui.py` with app skeleton and dark theme**

```python
#!/usr/bin/env python3
"""Zwoosh GUI — PySide6 desktop application for the Zwift Click BLE bridge."""

import sys
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTabWidget, QTextEdit, QLineEdit, QSpinBox,
    QCheckBox, QFrame, QSystemTrayIcon, QMenu, QSizePolicy,
)
from PySide6.QtCore import Qt, Signal, QObject, QTimer
from PySide6.QtGui import QIcon, QColor, QPainter, QPixmap, QFont, QTextCursor, QAction

from core import ZwooshCore, load_config, save_config, DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# Dark theme stylesheet
# ---------------------------------------------------------------------------

DARK_STYLE = """
QMainWindow, QWidget {
    background-color: #1a1a2e;
    color: #e0e0e0;
    font-family: "Segoe UI";
    font-size: 13px;
}

QLabel {
    color: #e0e0e0;
}

QPushButton {
    padding: 8px 18px;
    border-radius: 6px;
    font-weight: 600;
    font-size: 13px;
    border: none;
}

QPushButton#connectBtn {
    background-color: #ff6b35;
    color: white;
}
QPushButton#connectBtn:hover {
    background-color: #ff8555;
}
QPushButton#connectBtn:disabled {
    background-color: #555;
    color: #888;
}

QPushButton#disconnectBtn {
    background-color: #c0392b;
    color: white;
}
QPushButton#disconnectBtn:hover {
    background-color: #e04838;
}

QPushButton#secondaryBtn {
    background-color: #1a4a7a;
    color: #ccc;
    border: 1px solid #2a5a8a;
}
QPushButton#secondaryBtn:hover {
    background-color: #2a5a8a;
}

QPushButton#primaryBtn {
    background-color: #ff6b35;
    color: white;
}
QPushButton#primaryBtn:hover {
    background-color: #ff8555;
}

QTabWidget::pane {
    border: none;
    background-color: #1a1a2e;
}
QTabBar::tab {
    padding: 8px 16px;
    color: #888;
    border-bottom: 2px solid transparent;
    background: transparent;
    font-size: 13px;
}
QTabBar::tab:selected {
    color: #ff6b35;
    border-bottom: 2px solid #ff6b35;
}
QTabBar::tab:hover {
    color: #ccc;
}

QTextEdit {
    background-color: #0a0a1a;
    color: #e0e0e0;
    border: none;
    border-radius: 8px;
    padding: 10px;
    font-family: "Cascadia Code", "Consolas", monospace;
    font-size: 12px;
}

QLineEdit, QSpinBox {
    background-color: #0a0a1a;
    border: 1px solid #333;
    border-radius: 4px;
    padding: 6px 10px;
    color: #e0e0e0;
    font-size: 13px;
}
QLineEdit:focus, QSpinBox:focus {
    border-color: #ff6b35;
}

QCheckBox {
    color: #ccc;
    font-size: 13px;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 1px solid #555;
    background: #0a0a1a;
}
QCheckBox::indicator:checked {
    background: #ff6b35;
    border-color: #ff6b35;
}

QFrame#statusBar {
    background-color: #0a0a1a;
    border-radius: 8px;
    padding: 10px;
}

QFrame#deviceCard {
    background-color: #0f3460;
    border: 1px solid #1a4a7a;
    border-radius: 8px;
    padding: 10px;
}
"""


# ---------------------------------------------------------------------------
# Signal bridge (thread-safe core → GUI communication)
# ---------------------------------------------------------------------------

class CoreSignals(QObject):
    log = Signal(str, str, str, str)           # ts, level, source, message
    device_found = Signal(str, str, int)        # name, address, rssi
    connected = Signal(str, str, object)        # label, address, battery (int|None)
    disconnected = Signal(str)                  # label
    shift = Signal(str, str, int)               # label, direction, gear
    state_changed = Signal(str)                 # state string


# ---------------------------------------------------------------------------
# Tray icon helpers
# ---------------------------------------------------------------------------

def _make_colored_icon(color: str) -> QIcon:
    """Create a small solid-colored circle icon for the tray."""
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(4, 4, size - 8, size - 8)
    painter.end()
    return QIcon(pixmap)


# ---------------------------------------------------------------------------
# Device card widget
# ---------------------------------------------------------------------------

class DeviceCard(QFrame):
    def __init__(self, label: str, address: str, battery: int | None):
        super().__init__()
        self.setObjectName("deviceCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(4)

        name_label = QLabel(label)
        name_label.setStyleSheet("font-weight: 600; font-size: 13px;")
        layout.addWidget(name_label)

        addr_label = QLabel(address)
        addr_label.setStyleSheet("font-size: 11px; color: #666; font-family: monospace;")
        layout.addWidget(addr_label)

        self.battery_label = QLabel()
        self._set_battery(battery)
        self.battery_label.setStyleSheet("font-size: 12px; color: #2ecc71;")
        layout.addWidget(self.battery_label)

        self.device_label = label

    def _set_battery(self, battery: int | None):
        if battery is not None:
            self.battery_label.setText(f"Battery: {battery}%")
        else:
            self.battery_label.setText("Battery: unknown")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class ZwooshWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Zwoosh")
        self.setFixedSize(520, 580)

        self.cfg = load_config()
        self.core = ZwooshCore(self.cfg)
        self.signals = CoreSignals()
        self._device_cards: dict[str, DeviceCard] = {}
        self._state = "disconnected"

        self._setup_core_callbacks()
        self._build_ui()
        self._setup_tray()
        self._connect_signals()
        self._update_ui_state("disconnected")

    # ---- core callback → signal bridge ----

    def _setup_core_callbacks(self):
        s = self.signals
        self.core.on_log = s.log.emit
        self.core.on_device_found = s.device_found.emit
        self.core.on_connected = s.connected.emit
        self.core.on_disconnected = s.disconnected.emit
        self.core.on_shift = s.shift.emit
        self.core.on_state_changed = s.state_changed.emit

    def _connect_signals(self):
        s = self.signals
        s.log.connect(self._on_log)
        s.connected.connect(self._on_connected)
        s.disconnected.connect(self._on_disconnected)
        s.state_changed.connect(self._update_ui_state)

    # ---- build UI ----

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # Status bar
        self.status_frame = QFrame()
        self.status_frame.setObjectName("statusBar")
        status_layout = QHBoxLayout(self.status_frame)
        status_layout.setContentsMargins(12, 8, 12, 8)

        self.status_dot = QLabel()
        self.status_dot.setFixedSize(12, 12)
        status_layout.addWidget(self.status_dot)

        self.status_text = QLabel("Disconnected")
        self.status_text.setStyleSheet("font-size: 13px;")
        status_layout.addWidget(self.status_text)
        status_layout.addStretch()

        root.addWidget(self.status_frame)

        # Device cards container
        self.devices_layout = QHBoxLayout()
        self.devices_layout.setSpacing(10)
        self.devices_container = QWidget()
        self.devices_container.setLayout(self.devices_layout)
        self.devices_container.setVisible(False)
        root.addWidget(self.devices_container)

        # Buttons row
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("connectBtn")
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        btn_layout.addWidget(self.connect_btn)

        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.setObjectName("disconnectBtn")
        self.disconnect_btn.clicked.connect(self._on_disconnect_clicked)
        btn_layout.addWidget(self.disconnect_btn)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("secondaryBtn")
        self.cancel_btn.clicked.connect(self._on_disconnect_clicked)
        btn_layout.addWidget(self.cancel_btn)

        self.clear_btn = QPushButton("Clear Logs")
        self.clear_btn.setObjectName("secondaryBtn")
        self.clear_btn.clicked.connect(self._clear_logs)
        btn_layout.addWidget(self.clear_btn)

        btn_layout.addStretch()
        root.addLayout(btn_layout)

        # Tab widget
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        # --- Logs tab ---
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.tabs.addTab(self.log_text, "Logs")

        # --- Settings tab ---
        settings_widget = QWidget()
        settings_layout = QVBoxLayout(settings_widget)
        settings_layout.setContentsMargins(8, 12, 8, 8)
        settings_layout.setSpacing(16)

        # Key mappings section
        key_section_label = QLabel("KEY MAPPINGS")
        key_section_label.setStyleSheet("font-size: 11px; color: #ff6b35; font-weight: 600; letter-spacing: 0.5px;")
        settings_layout.addWidget(key_section_label)

        key_grid = QHBoxLayout()
        key_grid.setSpacing(12)

        up_col = QVBoxLayout()
        up_col.addWidget(QLabel("Shift Up Key"))
        self.shift_up_input = QLineEdit(self.cfg["shift_up_key"])
        self.shift_up_input.setMaximumWidth(120)
        up_col.addWidget(self.shift_up_input)
        key_grid.addLayout(up_col)

        down_col = QVBoxLayout()
        down_col.addWidget(QLabel("Shift Down Key"))
        self.shift_down_input = QLineEdit(self.cfg["shift_down_key"])
        self.shift_down_input.setMaximumWidth(120)
        down_col.addWidget(self.shift_down_input)
        key_grid.addLayout(down_col)

        key_grid.addStretch()
        settings_layout.addLayout(key_grid)

        # Connection section
        conn_section_label = QLabel("CONNECTION")
        conn_section_label.setStyleSheet("font-size: 11px; color: #ff6b35; font-weight: 600; letter-spacing: 0.5px;")
        settings_layout.addWidget(conn_section_label)

        conn_grid = QHBoxLayout()
        conn_grid.setSpacing(12)

        name_col = QVBoxLayout()
        name_col.addWidget(QLabel("Device Name"))
        self.device_name_input = QLineEdit(self.cfg["device_name"])
        name_col.addWidget(self.device_name_input)
        conn_grid.addLayout(name_col)

        timeout_col = QVBoxLayout()
        timeout_col.addWidget(QLabel("Scan Timeout (s)"))
        self.scan_timeout_input = QSpinBox()
        self.scan_timeout_input.setRange(5, 60)
        self.scan_timeout_input.setValue(self.cfg["scan_timeout"])
        timeout_col.addWidget(self.scan_timeout_input)
        conn_grid.addLayout(timeout_col)

        delay_col = QVBoxLayout()
        delay_col.addWidget(QLabel("Reconnect Delay (s)"))
        self.reconnect_delay_input = QSpinBox()
        self.reconnect_delay_input.setRange(1, 30)
        self.reconnect_delay_input.setValue(self.cfg["reconnect_delay"])
        delay_col.addWidget(self.reconnect_delay_input)
        conn_grid.addLayout(delay_col)

        settings_layout.addLayout(conn_grid)

        self.auto_reconnect_cb = QCheckBox("Auto Reconnect")
        self.auto_reconnect_cb.setChecked(self.cfg["auto_reconnect"])
        settings_layout.addWidget(self.auto_reconnect_cb)

        settings_layout.addStretch()

        # Save / Reset buttons
        save_row = QHBoxLayout()
        save_row.addStretch()

        reset_btn = QPushButton("Reset Defaults")
        reset_btn.setObjectName("secondaryBtn")
        reset_btn.clicked.connect(self._reset_defaults)
        save_row.addWidget(reset_btn)

        save_btn = QPushButton("Save")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._save_settings)
        save_row.addWidget(save_btn)

        settings_layout.addLayout(save_row)

        self.tabs.addTab(settings_widget, "Settings")

        # Tray note
        tray_note = QLabel("Close button minimizes to system tray. Right-click tray icon to quit.")
        tray_note.setStyleSheet("font-size: 11px; color: #666; border-left: 3px solid #ff6b35; padding-left: 8px;")
        root.addWidget(tray_note)

    # ---- system tray ----

    def _setup_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        self._icon_red = _make_colored_icon("#e74c3c")
        self._icon_green = _make_colored_icon("#2ecc71")
        self._icon_orange = _make_colored_icon("#f39c12")
        self.tray_icon.setIcon(self._icon_red)

        tray_menu = QMenu()
        self.show_action = QAction("Show Window", self)
        self.show_action.triggered.connect(self._show_window)
        tray_menu.addAction(self.show_action)

        self.tray_connect_action = QAction("Connect", self)
        self.tray_connect_action.triggered.connect(self._on_tray_toggle)
        tray_menu.addAction(self.tray_connect_action)

        tray_menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit_app)
        tray_menu.addAction(quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_window()

    def _show_window(self):
        self.showNormal()
        self.activateWindow()

    def _on_tray_toggle(self):
        if self._state == "disconnected":
            self._on_connect_clicked()
        else:
            self._on_disconnect_clicked()

    def _quit_app(self):
        self.core.stop()
        self.tray_icon.hide()
        QApplication.quit()

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    # ---- UI state management ----

    def _update_ui_state(self, state: str):
        self._state = state

        if state == "disconnected":
            self.status_dot.setStyleSheet(
                "background-color: #e74c3c; border-radius: 6px;"
            )
            self.status_text.setText("Disconnected")
            self.connect_btn.setVisible(True)
            self.connect_btn.setEnabled(True)
            self.disconnect_btn.setVisible(False)
            self.cancel_btn.setVisible(False)
            self.clear_btn.setVisible(True)
            self.devices_container.setVisible(False)
            self.tray_icon.setIcon(self._icon_red)
            self.tray_connect_action.setText("Connect")
            self._clear_device_cards()

        elif state == "scanning":
            self.status_dot.setStyleSheet(
                "background-color: #f39c12; border-radius: 6px;"
            )
            self.status_text.setText("Scanning...")
            self.connect_btn.setVisible(True)
            self.connect_btn.setEnabled(False)
            self.disconnect_btn.setVisible(False)
            self.cancel_btn.setVisible(True)
            self.clear_btn.setVisible(False)
            self.tray_icon.setIcon(self._icon_orange)
            self.tray_connect_action.setText("Cancel")

        elif state == "connected":
            self.status_dot.setStyleSheet(
                "background-color: #2ecc71; border-radius: 6px;"
            )
            self.connect_btn.setVisible(False)
            self.disconnect_btn.setVisible(True)
            self.cancel_btn.setVisible(False)
            self.clear_btn.setVisible(True)
            self.tray_icon.setIcon(self._icon_green)
            self.tray_connect_action.setText("Disconnect")

    def _update_device_count(self):
        count = len(self._device_cards)
        if count > 0:
            self.status_text.setText(f"Connected \u2014 {count} device{'s' if count != 1 else ''}")
            self.devices_container.setVisible(True)
        else:
            self.devices_container.setVisible(False)

    # ---- slots ----

    def _on_connect_clicked(self):
        self.cfg = load_config()
        self.core = ZwooshCore(self.cfg)
        self._setup_core_callbacks()
        self.core.start()

    def _on_disconnect_clicked(self):
        self.core.stop()

    def _on_log(self, ts: str, level: str, source: str, message: str):
        color_map = {
            "info": "#3498db",
            "ok": "#2ecc71",
            "warn": "#f39c12",
            "error": "#e74c3c",
            "shift": "#ff6b35",
        }
        color = color_map.get(level, "#e0e0e0")
        weight = "font-weight:600;" if level == "shift" else ""
        html = (
            f'<span style="color:#555">{ts}</span> '
            f'<span style="color:{color};{weight}">[{source}]</span> '
            f'<span style="color:#ccc;{weight}">{message}</span>'
        )
        self.log_text.append(html)
        self.log_text.moveCursor(QTextCursor.MoveOperation.End)

    def _on_connected(self, label: str, address: str, battery: object):
        bat = battery if isinstance(battery, int) else None
        card = DeviceCard(label, address, bat)
        self._device_cards[label] = card
        self.devices_layout.addWidget(card)
        self._update_device_count()

    def _on_disconnected(self, label: str):
        card = self._device_cards.pop(label, None)
        if card:
            self.devices_layout.removeWidget(card)
            card.deleteLater()
        self._update_device_count()

    def _clear_device_cards(self):
        for card in self._device_cards.values():
            self.devices_layout.removeWidget(card)
            card.deleteLater()
        self._device_cards.clear()

    def _clear_logs(self):
        self.log_text.clear()

    # ---- settings ----

    def _save_settings(self):
        new_cfg = {
            "device_name": self.device_name_input.text(),
            "shift_up_key": self.shift_up_input.text(),
            "shift_down_key": self.shift_down_input.text(),
            "scan_timeout": self.scan_timeout_input.value(),
            "reconnect_delay": self.reconnect_delay_input.value(),
            "auto_reconnect": self.auto_reconnect_cb.isChecked(),
        }
        self.cfg = {**DEFAULT_CONFIG, **new_cfg}
        save_config(self.cfg)
        self.core.reload_config(new_cfg)
        self._on_log(
            __import__("time").strftime("%H:%M:%S"),
            "ok", "Settings", "Configuration saved"
        )

    def _reset_defaults(self):
        self.device_name_input.setText(DEFAULT_CONFIG["device_name"])
        self.shift_up_input.setText(DEFAULT_CONFIG["shift_up_key"])
        self.shift_down_input.setText(DEFAULT_CONFIG["shift_down_key"])
        self.scan_timeout_input.setValue(DEFAULT_CONFIG["scan_timeout"])
        self.reconnect_delay_input.setValue(DEFAULT_CONFIG["reconnect_delay"])
        self.auto_reconnect_cb.setChecked(DEFAULT_CONFIG["auto_reconnect"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(DARK_STYLE)

    window = ZwooshWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it launches**

Run: `python gui.py`
Expected: Window appears with dark theme, "Disconnected" status, Connect button visible

- [ ] **Step 3: Commit**

```bash
git add gui.py
git commit -m "feat: add PySide6 GUI with dark theme, tray, logs, settings"
```

---

### Task 6: Create App Icon and Build Script

**Files:**
- Create: `assets/icon.ico`
- Create: `build.bat`

- [ ] **Step 1: Create `assets/` directory and generate a simple icon programmatically**

Run: `mkdir -p assets`

Then create a Python script to generate the icon:

```python
# generate_icon.py (temporary, delete after running)
from PIL import Image, ImageDraw, ImageFont

sizes = [16, 32, 48, 64, 128, 256]
images = []

for size in sizes:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Dark circle background
    margin = size // 8
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill="#1a1a2e", outline="#ff6b35", width=max(1, size // 16)
    )
    # "Z" letter
    font_size = size // 2
    try:
        font = ImageFont.truetype("arial.ttf", font_size)
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), "Z", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - tw) // 2
    y = (size - th) // 2 - bbox[1]
    draw.text((x, y), "Z", fill="#ff6b35", font=font)
    images.append(img)

images[0].save("assets/icon.ico", format="ICO", sizes=[(s, s) for s in sizes], append_images=images[1:])
print("Icon saved to assets/icon.ico")
```

Run: `pip install Pillow && python generate_icon.py`
Expected: `Icon saved to assets/icon.ico`

Then delete the temporary script:
Run: `rm generate_icon.py`

- [ ] **Step 2: Create `build.bat`**

```bat
@echo off
echo Building Zwoosh.exe ...
pyinstaller --onefile --windowed --name Zwoosh --icon=assets/icon.ico gui.py
echo.
echo Done! Output: dist\Zwoosh.exe
pause
```

- [ ] **Step 3: Commit**

```bash
git add assets/icon.ico build.bat
git commit -m "feat: add app icon and PyInstaller build script"
```

---

### Task 7: Build and Verify .exe

**Files:** None (build verification only)

- [ ] **Step 1: Run the build**

Run: `build.bat`
Expected: PyInstaller completes, `dist/Zwoosh.exe` is created

- [ ] **Step 2: Verify the .exe launches**

Run: `dist/Zwoosh.exe`
Expected: GUI window appears with dark theme, system tray icon visible, all UI elements functional

- [ ] **Step 3: Test the settings tab**

In the running GUI:
1. Switch to Settings tab
2. Change a key mapping
3. Click Save
4. Log panel shows "Configuration saved"
5. Check that `config.json` was updated

- [ ] **Step 4: Test system tray behavior**

1. Click the X button — window hides, tray icon remains
2. Double-click tray icon — window reappears
3. Right-click tray icon — menu shows "Show Window", "Connect", "Quit"
4. Click Quit — app fully exits

- [ ] **Step 5: Add build artifacts to .gitignore**

Create or update `.gitignore`:

```
.venv/
__pycache__/
*.pyc
build/
dist/
*.spec
.superpowers/
.idea/
```

- [ ] **Step 6: Final commit**

```bash
git add .gitignore
git commit -m "chore: add .gitignore for build artifacts and IDE files"
```
