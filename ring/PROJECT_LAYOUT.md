# Ring Sound V2 project layout

The active project now uses the vendor V2 Python SDK (`ring_sound.py` 0.4.1).

| Path | Purpose |
| --- | --- |
| `ring_sound.py`, `protocol.md`, `ring_sound_use.md`, `README.md` | Active V2 SDK and documentation |
| `V2/` | Unmodified vendor V2 package uploaded on 2026-07-23 |
| `legacy/V1/` | Archived V1 SDK, documentation, APK, and V1 mobile build outputs |
| `ring_bridge/` | Desktop BLE bridge, gesture mapping editor, and web UI |
| `mobile/V2/work/` | Modified V2 mobile application assets |
| `mobile/V2/build/` | Expanded APK build tree |
| `output/V2/ring_gesture_editor_v2.0.apk` | Signed V2 mobile APK |
| `gesture_mappings.json` | Desktop gesture-to-action mappings |
| `start_v2_bridge.ps1` | Starts the V2 desktop bridge on port 8520 |

## V2 migration notes

- SDK version: 0.4.1.
- Default BLE scan timeout: 25 seconds.
- NUS RX writes are always split into 20-byte chunks while MTU negotiation remains enabled.
- The desktop bridge retries after an unexpected BLE transport loss.
- The mobile app keeps the V1 gesture mapping workflow and UI, adds 0x0704 single-click handling, and now also fixes BLE writes at 20 bytes for V2.
- The public V2 protocol reports gestures but still does not expose a command for uploading a new HMM gesture model to ring firmware. The editors therefore modify gesture-to-application-action mappings.

## Start

```powershell
powershell -ExecutionPolicy Bypass -File .\start_v2_bridge.ps1
```

Open `http://127.0.0.1:8520` on the PC. Devices on the same LAN can use the mobile URL printed by the server.
