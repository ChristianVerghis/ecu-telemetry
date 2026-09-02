# ECU Telemetry Wire Protocol v1

All integers little-endian. All frames (UDP and TCP) share one format:

```
offset  size  field
0       2     magic        u16   = 0x4554  (bytes "TE" on the wire, i.e. 'T'=0x54 low byte, 'E'=0x45 high byte)
2       1     version      u8    = 1
3       1     msg_type     u8    (see below)
4       12    device_id    char[12] ASCII, NUL padded (e.g. "ECU-0001")
16      4     seq          u32   per-device, per-transport monotonically increasing (wraps)
20      8     ts_ms        u64   unix epoch milliseconds (device clock)
28      4     fw_version   u32   (major<<16)|(minor<<8)|patch
32      2     payload_len  u16
34      2     reserved     u16   = 0
36      N     payload
36+N    4     crc32        u32   IEEE 802.3 CRC-32 (zlib.crc32) over bytes [0, 36+N)
```

Header = 36 bytes. Max payload = 1024 bytes. Frames with bad magic/version/CRC are dropped and counted.

## Transports
* **UDP :8781** — device → backend. TELEMETRY only. Fire-and-forget; backend derives packet loss from `seq` gaps.
* **TCP :8782** — bidirectional control channel. Device connects, sends HELLO, then HEARTBEAT every 5 s and LOG frames as they occur. Backend pushes CONFIG_SET / FW_UPDATE / FAULT_INJECT / CMD. Frames are concatenated back-to-back; receiver reads the 36-byte header, then `payload_len + 4` bytes. Device reconnects with exponential backoff (1 s → 30 s, jittered) on any error; telemetry over UDP continues regardless. Backend marks a device `offline` after 15 s without HEARTBEAT or TELEMETRY.

## Message types

| type | name         | dir | payload |
|------|--------------|-----|---------|
| 0x01 | TELEMETRY    | D→B | see below (48 bytes) |
| 0x02 | HELLO        | D→B | `char fw_string[16]`, `char hw_model[16]`, `u32 config_version`, `u16 telemetry_hz`, `u16 pad` (40 bytes) |
| 0x03 | HEARTBEAT    | D→B | `u32 uptime_s`, `u32 frames_sent`, `u32 config_version`, `u32 reconnects` (16 bytes) |
| 0x04 | LOG          | D→B | `u8 level` (0 debug,1 info,2 warn,3 error), `u8 pad[3]`, utf8 message (rest of payload) |
| 0x10 | CONFIG_SET   | B→D | `u32 config_version`, `u16 telemetry_hz`, `u8 log_level`, `u8 pad`, `f32 rpm_limit`, `f32 temp_limit_c`, `f32 current_limit_a` (20 bytes) |
| 0x11 | CONFIG_ACK   | D→B | `u32 config_version`, `u8 status` (0 applied, 1 rejected), `u8 pad[3]` (8 bytes) |
| 0x12 | FW_UPDATE    | B→D | `char target_version[16]`, `u32 image_size_bytes`, `u32 image_crc` (24 bytes) |
| 0x13 | FW_ACK       | D→B | `u8 status` (0 accepted, 1 downloading, 2 applied, 3 rejected), `u8 pad[3]`, `char version[16]` (20 bytes) |
| 0x14 | FAULT_INJECT | B→D | `u32 set_flags`, `u32 clear_flags`, `u32 duration_ms` (0 = until cleared) (12 bytes) |
| 0x15 | FAULT_ACK    | D→B | `u32 active_flags` (4 bytes) |
| 0x20 | CMD          | B→D | `u8 cmd` (0 reboot, 1 clear_faults, 2 request_hello), `u8 pad[3]` (4 bytes) |

### TELEMETRY payload (48 bytes)
```
f32 rpm            motor speed
f32 temp_c         motor winding temperature
f32 current_a      phase current
f32 voltage_v      DC bus voltage
f32 vibration_g    RMS vibration
f32 speed_kph      vehicle speed
f64 lat
f64 lon
u32 fault_flags    bitmask below
u8  state          0 IDLE, 1 RUNNING, 2 DEGRADED, 3 FAULT
u8  pad[3]
```

### Fault flags
```
0x01 OVERTEMP        0x02 OVERCURRENT     0x04 UNDERVOLTAGE   0x08 VIBRATION_HIGH
0x10 SENSOR_STUCK    0x20 GPS_LOST        0x40 COMM_DEGRADED  0x80 ENCODER_FAULT
```

### Firmware versions
`fw_string` is "MAJOR.MINOR.PATCH". `fw_version` u32 = (major<<16)|(minor<<8)|patch. Simulated OTA: device receives FW_UPDATE → FW_ACK(accepted) → sleeps `image_size_bytes / 500000` s (simulated download, min 1 s) sending FW_ACK(downloading) → FW_ACK(applied) → closes TCP, "reboots" (resets uptime, seq continues), reconnects and sends HELLO with the new version. A FW_UPDATE whose target_version is lower than current or malformed → FW_ACK(rejected).
