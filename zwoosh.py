#!/usr/bin/env python3
"""
Zwoosh - Zwift Click V2 to MyWhoosh Virtual Shifting Bridge

Connects to both Zwift Click V2 units (left + right hood) via BLE
and translates shift up/down button presses into keyboard events
for MyWhoosh virtual shifting.

The Click V2 has two separate BLE devices that both advertise as
"Zwift Click". Each has two buttons (shift up / shift down).

Protocol (reverse-engineered):
  - Service:    0000fc82-0000-1000-8000-00805f9b34fb
  - Buttons:    char 0002 (notify)  - streams 7-byte button state messages
  - Command:    char 0100 (write+notify) - send unlock command here
  - Unlock:     write 0x00 0x09 0x00 to char 0100 to activate button streaming
  - Button msg: 0x23 0x08 XX YY ZZ WW 0x0F  (active-low bitmask in byte[3])
    - Idle:        byte[3] = 0xFF
    - Button A:    byte[3] bit 5 cleared (0xDF) - shift up
    - Button B:    byte[3] bit 1 cleared (0xFD) - shift down
    - Both:        byte[3] = 0xDD
"""

import asyncio
import json
import signal
import sys
import time
from pathlib import Path

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
    return DEFAULT_CONFIG


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
# BLE - Zwift Click V2 protocol
# ---------------------------------------------------------------------------

# GATT characteristics
ZWIFT_CLICK_BTN_UUID  = "00000002-19ca-4651-86e5-fa29dcdd09d1"  # notify - button state
ZWIFT_CLICK_CMD_UUID  = "00000003-19ca-4651-86e5-fa29dcdd09d1"  # write  - commands
ZWIFT_CLICK_INFO_UUID = "00000004-19ca-4651-86e5-fa29dcdd09d1"  # read/indicate
ZWIFT_CLICK_CH0100    = "00000100-19ca-4651-86e5-fa29dcdd09d1"  # write+notify
ZWIFT_CLICK_CH0101    = "00000101-19ca-4651-86e5-fa29dcdd09d1"  # write+notify
ZWIFT_CLICK_CH0102    = "00000102-19ca-4651-86e5-fa29dcdd09d1"  # notify
BATTERY_LEVEL_UUID    = "00002a19-0000-1000-8000-00805f9b34fb"

# Handshake commands (derived from successful debug session):
#   1. Write "RideOn" to char 0003 → device echoes on char 0004
#   2. Write 0x000900 to char 0100 → device responds with 0x0a, starts streaming
RIDEON_CMD = b"RideOn"
UNLOCK_CMD = bytes([0x00, 0x09, 0x00])

# Button state message format on char 0002:
#   7 bytes: 0x23 0x08 XX YY ZZ WW 0x0F
#   Message type 0x23, protobuf field 1 varint with button bitmask
#   Buttons are active-low in byte[3] (4th byte, index 3)
BTN_MSG_TYPE = 0x23
BTN_MSG_LEN = 7
BTN_STATE_BYTE = 3  # index of the byte containing button bits

# Active-low bit masks in byte[3]:
# When pressed, the bit is CLEARED (0). When released, it's SET (1).
BTN_A_MASK = 0x20   # bit 5 - shift up
BTN_B_MASK = 0x02   # bit 1 - shift down

# Debounce: ignore repeated presses of the same button within this window (seconds)
DEBOUNCE_SECS = 0.25


class ZwooshBridge:
    def __init__(self, config: dict, debug: bool = False, test: bool = False):
        self.cfg = config
        self.debug = debug
        self.test = test
        self.kb = Controller()
        self.key_up = resolve_key(config["shift_up_key"])
        self.key_down = resolve_key(config["shift_down_key"])
        self._running = True
        self._gear = 0
        # Track previous button state per device to detect edges
        self._prev_buttons: dict[str, int] = {}
        # Debounce: last press time per button (global across all devices,
        # because both Click units report the same press simultaneously)
        self._last_press: dict[int, float] = {}

    # ---- keyboard output ----

    def press_key(self, key, direction: str, device_label: str):
        self.kb.press(key)
        self.kb.release(key)
        if direction == "up":
            self._gear += 1
        else:
            self._gear -= 1
        print(f"  >> [{device_label}] Shift {direction}  (gear: {self._gear:+d})")

    # ---- BLE notification handler ----

    def _debounced(self, mask: int) -> bool:
        """Return True if this button press should be accepted.

        Global across all devices — both Click units report the same press,
        so we deduplicate by button, not by device.
        """
        now = time.monotonic()
        last = self._last_press.get(mask, 0.0)
        if now - last < DEBOUNCE_SECS:
            return False
        self._last_press[mask] = now
        return True

    def make_button_handler(self, device_label: str):
        """Return a notification callback that parses button state messages."""
        # Start with all buttons "released" (bits set = active-low released)
        self._prev_buttons[device_label] = BTN_A_MASK | BTN_B_MASK

        def on_notify(_char: BleakGATTCharacteristic, data: bytearray):
            if len(data) != BTN_MSG_LEN or data[0] != BTN_MSG_TYPE:
                return

            btn_byte = data[BTN_STATE_BYTE]
            prev = self._prev_buttons[device_label]

            # Active-low: detect falling edges (bit going 1 -> 0 = newly pressed)
            newly_pressed = prev & ~btn_byte

            if newly_pressed & BTN_A_MASK and self._debounced(BTN_A_MASK):
                self.press_key(self.key_up, "up", device_label)
            if newly_pressed & BTN_B_MASK and self._debounced(BTN_B_MASK):
                self.press_key(self.key_down, "down", device_label)

            self._prev_buttons[device_label] = btn_byte

        return on_notify

    # ---- scan ----

    async def find_devices(self) -> list:
        """Scan for all Zwift Click V2 devices."""
        target = self.cfg["device_name"]
        timeout = self.cfg["scan_timeout"]
        print(f"Scanning for '{target}' (timeout {timeout}s) ...")

        found = []
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)

        # Show all nearby BLE devices
        named = [(dev, adv) for dev, adv in discovered.values() if dev.name]
        if named:
            print(f"\n  Nearby BLE devices ({len(named)}):")
            for dev, adv in sorted(named, key=lambda x: x[1].rssi or -999, reverse=True):
                is_match = dev.name and target.lower() in dev.name.lower()
                marker = "*" if is_match else "-"
                print(f"    {marker} {dev.name}  [{dev.address}]  RSSI: {adv.rssi}")
            print()
        else:
            print("  No BLE devices found nearby.\n")

        for dev, adv in discovered.values():
            if dev.name and target.lower() in dev.name.lower():
                found.append(dev)

        if not found:
            for dev, adv in discovered.values():
                if dev.name and "zwift" in dev.name.lower() and "click" in dev.name.lower():
                    found.append(dev)

        if found:
            print(f"  Matched {len(found)} Zwift Click device(s):")
            for d in found:
                print(f"    -> {d.name}  [{d.address}]")
        else:
            print("  No Zwift Click devices matched.")
        return found

    # ---- connect to a single device ----

    async def connect_one(self, device):
        """Connect to one Click unit, unlock it, and listen for button events."""
        short_addr = device.address[-5:].replace(":", "")
        label = f"Click {short_addr}"
        print(f"  [{label}] Connecting to {device.address} ...")

        async with BleakClient(device, timeout=15.0) as client:
            print(f"  [{label}] Connected!")

            # Read battery level
            try:
                battery_data = await client.read_gatt_char(BATTERY_LEVEL_UUID)
                print(f"  [{label}] Battery: {battery_data[0]}%")
            except Exception:
                pass

            if self.debug:
                await self._debug_listen(client, label)
            elif self.test:
                await self._test_listen(client, label)
            else:
                await self._normal_listen(client, label)

    async def _handshake(self, client, label: str):
        """Full handshake to activate button streaming.

        Replicates the sequence observed in the debug session:
        1. Subscribe to notifications on all custom characteristics
        2. Write "RideOn" to char 0003 (device echoes it on char 0004)
        3. Write 0x000900 to char 0100 (device starts streaming on 0002)
        """
        noop = lambda _c, _d: None

        # Step 1: subscribe to all custom notify/indicate characteristics
        for uuid in [ZWIFT_CLICK_INFO_UUID, ZWIFT_CLICK_CH0100,
                     ZWIFT_CLICK_CH0101, ZWIFT_CLICK_CH0102]:
            try:
                await client.start_notify(uuid, noop)
            except Exception:
                pass

        # Step 2: write "RideOn" to char 0003
        try:
            await client.write_gatt_char(ZWIFT_CLICK_CMD_UUID, RIDEON_CMD, response=False)
            await asyncio.sleep(0.3)
        except Exception as exc:
            print(f"  [{label}] RideOn write failed: {exc}")

        # Step 3: write unlock to char 0100
        try:
            await client.write_gatt_char(ZWIFT_CLICK_CH0100, UNLOCK_CMD, response=False)
            await asyncio.sleep(0.5)
        except Exception as exc:
            print(f"  [{label}] Unlock write failed: {exc}")

        print(f"  [{label}] Handshake complete.")

    async def _normal_listen(self, client, label: str):
        """Subscribe to button notifications, unlock device, then listen."""
        handler = self.make_button_handler(label)
        await client.start_notify(ZWIFT_CLICK_BTN_UUID, handler)

        await self._handshake(client, label)
        print(f"  [{label}] Listening for button events.")

        while client.is_connected and self._running:
            await asyncio.sleep(0.5)

        print(f"  [{label}] Disconnected.")

    async def _test_listen(self, client, label: str):
        """Subscribe to button notifications and log presses without sending keys."""
        prev = BTN_A_MASK | BTN_B_MASK

        def on_notify(_char: BleakGATTCharacteristic, data: bytearray):
            nonlocal prev
            if len(data) != BTN_MSG_LEN or data[0] != BTN_MSG_TYPE:
                return

            btn_byte = data[BTN_STATE_BYTE]
            newly_pressed = prev & ~btn_byte
            newly_released = ~prev & btn_byte

            if newly_pressed & BTN_A_MASK and self._debounced(BTN_A_MASK):
                print(f"  [{label}] Button A PRESSED  (byte[3]=0x{btn_byte:02X}, mask=0x20)")
            if newly_released & BTN_A_MASK:
                print(f"  [{label}] Button A released (byte[3]=0x{btn_byte:02X})")
            if newly_pressed & BTN_B_MASK and self._debounced(BTN_B_MASK):
                print(f"  [{label}] Button B PRESSED  (byte[3]=0x{btn_byte:02X}, mask=0x02)")
            if newly_released & BTN_B_MASK:
                print(f"  [{label}] Button B released (byte[3]=0x{btn_byte:02X})")

            prev = btn_byte

        await client.start_notify(ZWIFT_CLICK_BTN_UUID, on_notify)
        await self._handshake(client, label)
        print(f"  [{label}] TEST MODE - no keyboard output.")
        print(f"  [{label}] Press buttons to see which is A and which is B.\n")

        while client.is_connected and self._running:
            await asyncio.sleep(0.5)

        print(f"  [{label}] Disconnected.")

    async def _debug_listen(self, client, label: str):
        """Subscribe to ALL notify/indicate chars, read all readable chars, try unlock."""
        print(f"  [{label}] DEBUG MODE")

        # Read all readable characteristics
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

        # Subscribe to all notify/indicate characteristics
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

        # Unlock
        print(f"  [{label}] Sending unlock (000900 -> char 0100) ...")
        try:
            await client.write_gatt_char(ZWIFT_CLICK_INIT_UUID, UNLOCK_CMD, response=False)
            print(f"  [{label}] Unlock sent.")
        except Exception as exc:
            print(f"  [{label}] Unlock failed: {exc}")

        print(f"\n  [{label}] Press buttons now (Ctrl+C to quit).\n")

        while client.is_connected and self._running:
            await asyncio.sleep(0.5)

        print(f"  [{label}] Disconnected.")

    # ---- main loop ----

    async def run(self):
        while self._running:
            devices = await self.find_devices()

            if not devices:
                if not self.cfg["auto_reconnect"]:
                    break
                delay = self.cfg["reconnect_delay"]
                print(f"  Retrying in {delay}s ...\n")
                await asyncio.sleep(delay)
                continue

            tasks = [
                asyncio.create_task(self._run_one_with_reconnect(dev))
                for dev in devices
            ]
            print(f"\n  Bridging {len(devices)} device(s). Press Ctrl+C to quit.\n")

            await asyncio.gather(*tasks)

            if not self.cfg["auto_reconnect"]:
                break
            delay = self.cfg["reconnect_delay"]
            print(f"  All devices disconnected. Reconnecting in {delay}s ...\n")
            await asyncio.sleep(delay)

    async def _run_one_with_reconnect(self, device):
        """Manage a single device connection with reconnect."""
        short_addr = device.address[-5:].replace(":", "")
        label = f"Click {short_addr}"
        while self._running:
            try:
                await self.connect_one(device)
            except Exception as exc:
                print(f"  [{label}] Error: {exc}")
            if not self._running or not self.cfg["auto_reconnect"]:
                break
            delay = self.cfg["reconnect_delay"]
            print(f"  [{label}] Reconnecting in {delay}s ...")
            await asyncio.sleep(delay)

    def stop(self):
        self._running = False


# ---------------------------------------------------------------------------
# Scan-only mode
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
# Entry point
# ---------------------------------------------------------------------------

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
    test = "--test" in sys.argv
    bridge = ZwooshBridge(config, debug=debug, test=test)

    loop = asyncio.new_event_loop()

    def _sigint(*_):
        print("\nStopping ...")
        bridge.stop()

    signal.signal(signal.SIGINT, _sigint)

    print("=" * 50)
    print("  Zwoosh - Zwift Click V2 -> MyWhoosh Bridge")
    print("=" * 50)
    if test:
        print("  MODE: TEST (log buttons, no keyboard)")
    elif debug:
        print("  MODE: DEBUG (raw BLE data)")
    print(f"  Device name:  '{config['device_name']}'")
    print(f"  Shift up   -> key '{config['shift_up_key']}'")
    print(f"  Shift down -> key '{config['shift_down_key']}'")
    print()

    loop.run_until_complete(bridge.run())
    loop.close()


if __name__ == "__main__":
    main()
