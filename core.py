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
