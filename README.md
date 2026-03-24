# Zwoosh

Use your **Zwift Click V2** controllers for virtual shifting on **MyWhoosh** (or any app that accepts keyboard input).

Zwoosh connects to both Zwift Click V2 units via Bluetooth Low Energy, performs the handshake to activate them, and translates button presses into configurable keyboard events.

## How it works

The Zwift Click V2 consists of two BLE devices (left and right hood), each with shift-up and shift-down buttons. The protocol was reverse-engineered:

1. Connect to both "Zwift Click" BLE devices
2. Subscribe to GATT notifications and perform a handshake (`RideOn` + unlock command)
3. Parse 7-byte button state messages on characteristic `0002`
4. Translate button presses into keyboard events that MyWhoosh reads as virtual shifting

## Requirements

- Python 3.10+
- Bluetooth adapter with BLE support
- Zwift Click V2 controllers
- Windows, macOS, or Linux

## Installation

```bash
git clone https://github.com/amith2368/zwoosh.git
cd zwoosh
pip install -r requirements.txt
```

## Usage

### Start the bridge

```bash
python zwoosh.py
```

This scans for your Zwift Click V2 devices, connects, unlocks them, and starts translating button presses to keyboard events. Make sure MyWhoosh is the focused window.

### Scan for BLE devices

```bash
python zwoosh.py --scan
```

Lists all nearby BLE devices and dumps GATT services for any Zwift devices found. Useful for troubleshooting connectivity.

### Test button detection

```bash
python zwoosh.py --test
```

Connects and logs button presses without sending any keyboard events. Use this to verify your Clicks are working before a ride.

### Debug mode

```bash
python zwoosh.py --debug
```

Subscribes to all BLE characteristics and prints raw notification data. Useful for protocol analysis.

## Configuration

Edit `config.json` to customize:

```json
{
    "device_name": "Zwift Click",
    "shift_up_key": "=",
    "shift_down_key": "-",
    "scan_timeout": 15,
    "reconnect_delay": 3,
    "auto_reconnect": true
}
```

| Option | Description | Default |
|--------|-------------|---------|
| `device_name` | BLE device name to scan for | `"Zwift Click"` |
| `shift_up_key` | Key to send for shift up | `"="` |
| `shift_down_key` | Key to send for shift down | `"-"` |
| `scan_timeout` | BLE scan duration in seconds | `15` |
| `reconnect_delay` | Seconds to wait before reconnecting | `3` |
| `auto_reconnect` | Automatically reconnect on disconnect | `true` |

### Supported keys

Single characters (`a`, `1`, `=`, `-`, etc.) or special keys: `up`, `down`, `left`, `right`, `space`, `enter`, `tab`, `page_up`, `page_down`.

## Troubleshooting

**Devices not found:** Make sure your Zwift Clicks are not connected to the Zwift app or any other application. Only one app can connect to a BLE device at a time.

**No button events:** The Zwift Click V2 requires a handshake to activate. If `--test` shows no output when pressing buttons, the handshake may have failed. Try disconnecting and reconnecting.

**Double shifts:** Both Click units report the same button press simultaneously. Zwoosh includes a 250ms debounce to deduplicate these. If you still see doubles, try increasing `DEBOUNCE_SECS` in `zwoosh.py`.

**Permission errors on Linux:** You may need to run with `sudo` or configure udev rules for Bluetooth access.

## BLE Protocol Reference

The Zwift Click V2 uses a custom BLE GATT service (`0000fc82-0000-1000-8000-00805f9b34fb`):

| Characteristic | UUID | Properties | Purpose |
|---------------|------|------------|---------|
| 0002 | `00000002-19ca-...` | notify | Button state messages |
| 0003 | `00000003-19ca-...` | write | Commands (RideOn handshake) |
| 0004 | `00000004-19ca-...` | read, indicate | Device info |
| 0100 | `00000100-19ca-...` | write, notify | Unlock/init command |
| 0101 | `00000101-19ca-...` | write, notify | Extended data |
| 0102 | `00000102-19ca-...` | notify | Status |

### Handshake sequence

1. Subscribe to notifications on chars `0002`, `0004`, `0100`, `0101`, `0102`
2. Write `RideOn` (ASCII) to char `0003`
3. Write `0x00 0x09 0x00` to char `0100`
4. Device starts streaming button state on char `0002`

### Button state message format

7 bytes on char `0002`: `0x23 0x08 XX YY ZZ WW 0x0F`

- Byte 3 contains the button bitmask (active-low)
- Idle: `0xFF` (all bits set)
- Shift up pressed: bit 5 cleared (`0xDF`)
- Shift down pressed: bit 1 cleared (`0xFD`)
- Both pressed: `0xDD`

## License

MIT
