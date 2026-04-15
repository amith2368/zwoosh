#!/usr/bin/env python3
"""Zwoosh CLI — Zwift Click V2 to MyWhoosh Virtual Shifting Bridge."""

import asyncio
import signal
import sys
import time

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

from core import (
    ZwooshCore, load_config, BATTERY_LEVEL_UUID,
    ZWIFT_CLICK_BTN_UUID, ZWIFT_CLICK_CMD_UUID, ZWIFT_CLICK_INFO_UUID,
    ZWIFT_CLICK_CH0100, ZWIFT_CLICK_CH0101, ZWIFT_CLICK_CH0102,
    RIDEON_CMD, UNLOCK_CMD, BTN_MSG_TYPE, BTN_MSG_LEN, BTN_STATE_BYTE,
    BTN_A_MASK, BTN_B_MASK, DEBOUNCE_SECS, CONFIG_PATH,
)


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

    try:
        while core.is_running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        core.stop()


if __name__ == "__main__":
    main()
