# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Zwoosh is a BLE bridge that connects Zwift Click V2 shifters to MyWhoosh virtual shifting. It scans for Zwift Click BLE devices, performs a handshake to unlock button streaming, then translates button presses into keyboard events via pynput.

## Running

```bash
# Install dependencies (Python 3, venv already exists)
.venv/Scripts/activate && pip install -r requirements.txt

# Normal mode - connect and bridge shift buttons to keyboard
python zwoosh.py

# Scan-only - discover BLE devices and dump GATT services
python zwoosh.py --scan

# Test mode - log button presses without sending keyboard events
python zwoosh.py --test

# Debug mode - subscribe to all BLE characteristics and print raw data
python zwoosh.py --debug
```

## Architecture

Single-file application (`zwoosh.py`) with one main class:

- **`ZwooshBridge`** - Core class managing BLE connections and button-to-key translation. Connects to multiple Click devices concurrently via `asyncio.gather`. Each device gets its own `connect_one` coroutine with auto-reconnect logic.
- **BLE Protocol** - Reverse-engineered Zwift Click V2 protocol. Handshake writes "RideOn" to char 0003, then 0x000900 to char 0100. Button state arrives on char 0002 as 7-byte messages with active-low bitmask in byte[3].
- **Debouncing** - Global (cross-device) debounce since both Click units report the same press simultaneously (0.25s window).
- **Config** - `config.json` next to the script, merged over defaults. Key mappings support single chars or special key names (up, down, space, etc.).

## Key Constants

The GATT UUIDs and button bitmasks at the top of `zwoosh.py` are derived from reverse-engineering. `BTN_A_MASK` (0x20) = shift up, `BTN_B_MASK` (0x02) = shift down.
