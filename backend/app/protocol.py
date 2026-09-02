"""ECU Telemetry Wire Protocol v1 — encode/decode.

Standalone module (stdlib only) so sim/ and tests can import it directly.
See docs/protocol.md for the binding spec. All integers little-endian.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from enum import IntEnum
from typing import ClassVar

MAGIC = 0x4554
VERSION = 1
HEADER_LEN = 36
MAX_PAYLOAD = 1024
HEADER_FMT = "<HBB12sIQIHH"  # magic, ver, type, device_id, seq, ts_ms, fw, payload_len, reserved
assert struct.calcsize(HEADER_FMT) == HEADER_LEN


class MsgType(IntEnum):
    TELEMETRY = 0x01
    HELLO = 0x02
    HEARTBEAT = 0x03
    LOG = 0x04
    CONFIG_SET = 0x10
    CONFIG_ACK = 0x11
    FW_UPDATE = 0x12
    FW_ACK = 0x13
    FAULT_INJECT = 0x14
    FAULT_ACK = 0x15
    CMD = 0x20


FAULT_FLAGS: dict[str, int] = {
    "OVERTEMP": 0x01,
    "OVERCURRENT": 0x02,
    "UNDERVOLTAGE": 0x04,
    "VIBRATION_HIGH": 0x08,
    "SENSOR_STUCK": 0x10,
    "GPS_LOST": 0x20,
    "COMM_DEGRADED": 0x40,
    "ENCODER_FAULT": 0x80,
}
FLAG_BITS: dict[int, str] = {v: k for k, v in FAULT_FLAGS.items()}

STATE_NAMES = {0: "IDLE", 1: "RUNNING", 2: "DEGRADED", 3: "FAULT"}
LOG_LEVELS = {0: "debug", 1: "info", 2: "warn", 3: "error"}
LOG_LEVEL_IDS = {v: k for k, v in LOG_LEVELS.items()}
CMD_IDS = {"reboot": 0, "clear_faults": 1, "request_hello": 2}
CONFIG_ACK_STATUS = {0: "applied", 1: "rejected"}
FW_ACK_STATUS = {0: "accepted", 1: "downloading", 2: "applied", 3: "rejected"}


class ProtocolError(ValueError):
    pass


class BadMagic(ProtocolError):
    pass


class BadVersion(ProtocolError):
    pass


class BadCRC(ProtocolError):
    pass


class BadLength(ProtocolError):
    pass


# ---------------------------------------------------------------- flags / fw


def flags_to_names(flags: int) -> list[str]:
    return [name for name, bit in FAULT_FLAGS.items() if flags & bit]


def names_to_flags(names) -> int:
    out = 0
    for n in names:
        key = str(n).strip().upper()
        if key not in FAULT_FLAGS:
            raise ProtocolError(f"unknown fault flag {n!r}")
        out |= FAULT_FLAGS[key]
    return out


def fw_encode(version: str) -> int:
    parts = version.strip().split(".")
    if len(parts) != 3:
        raise ProtocolError(f"bad fw version {version!r}")
    try:
        major, minor, patch = (int(p) for p in parts)
    except ValueError as e:
        raise ProtocolError(f"bad fw version {version!r}") from e
    if not (0 <= major < 65536 and 0 <= minor < 256 and 0 <= patch < 256):
        raise ProtocolError(f"fw version out of range {version!r}")
    return (major << 16) | (minor << 8) | patch


def fw_decode(v: int) -> str:
    return f"{(v >> 16) & 0xFFFF}.{(v >> 8) & 0xFF}.{v & 0xFF}"


def _cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("ascii", "replace")


def _pad(s: str, n: int) -> bytes:
    b = s.encode("ascii", "replace")[: n - 1] if len(s.encode("ascii", "replace")) >= n else s.encode("ascii", "replace")
    return b.ljust(n, b"\0")


# ---------------------------------------------------------------- header


@dataclass
class Header:
    msg_type: int
    device_id: str
    seq: int
    ts_ms: int
    fw_version: int
    payload_len: int = 0
    version: int = VERSION

    def pack(self) -> bytes:
        return struct.pack(
            HEADER_FMT, MAGIC, self.version, self.msg_type, _pad(self.device_id, 12),
            self.seq & 0xFFFFFFFF, self.ts_ms & 0xFFFFFFFFFFFFFFFF, self.fw_version & 0xFFFFFFFF,
            self.payload_len, 0,
        )

    @classmethod
    def unpack(cls, buf: bytes) -> "Header":
        if len(buf) < HEADER_LEN:
            raise BadLength("short header")
        magic, ver, mt, dev, seq, ts, fw, plen, _res = struct.unpack_from(HEADER_FMT, buf, 0)
        if magic != MAGIC:
            raise BadMagic(f"magic 0x{magic:04x}")
        if ver != VERSION:
            raise BadVersion(f"version {ver}")
        if plen > MAX_PAYLOAD:
            raise BadLength(f"payload_len {plen}")
        return cls(msg_type=mt, device_id=_cstr(dev), seq=seq, ts_ms=ts, fw_version=fw, payload_len=plen, version=ver)


# ---------------------------------------------------------------- payloads


class Payload:
    TYPE: ClassVar[int]

    def pack(self) -> bytes:  # pragma: no cover - abstract
        raise NotImplementedError

    @classmethod
    def unpack(cls, b: bytes):  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass
class Telemetry(Payload):
    TYPE: ClassVar[int] = MsgType.TELEMETRY
    FMT: ClassVar[str] = "<ffffffddIB3x"
    rpm: float = 0.0
    temp_c: float = 0.0
    current_a: float = 0.0
    voltage_v: float = 0.0
    vibration_g: float = 0.0
    speed_kph: float = 0.0
    lat: float = 0.0
    lon: float = 0.0
    fault_flags: int = 0
    state: int = 0

    def pack(self) -> bytes:
        return struct.pack(self.FMT, self.rpm, self.temp_c, self.current_a, self.voltage_v,
                           self.vibration_g, self.speed_kph, self.lat, self.lon,
                           self.fault_flags & 0xFFFFFFFF, self.state & 0xFF)

    @classmethod
    def unpack(cls, b: bytes) -> "Telemetry":
        if len(b) != struct.calcsize(cls.FMT):
            raise BadLength(f"telemetry payload {len(b)}")
        return cls(*struct.unpack(cls.FMT, b))

    def as_dict(self) -> dict:
        return {
            "rpm": self.rpm, "temp_c": self.temp_c, "current_a": self.current_a,
            "voltage_v": self.voltage_v, "vibration_g": self.vibration_g, "speed_kph": self.speed_kph,
            "lat": self.lat, "lon": self.lon, "fault_flags": self.fault_flags, "state": self.state,
            "state_name": STATE_NAMES.get(self.state, str(self.state)),
            "faults": flags_to_names(self.fault_flags),
        }


assert struct.calcsize(Telemetry.FMT) == 48


@dataclass
class Hello(Payload):
    TYPE: ClassVar[int] = MsgType.HELLO
    FMT: ClassVar[str] = "<16s16sIHH"
    fw_string: str = "1.0.0"
    hw_model: str = "SIM"
    config_version: int = 0
    telemetry_hz: int = 10

    def pack(self) -> bytes:
        return struct.pack(self.FMT, _pad(self.fw_string, 16), _pad(self.hw_model, 16),
                           self.config_version, self.telemetry_hz, 0)

    @classmethod
    def unpack(cls, b: bytes) -> "Hello":
        if len(b) != 40:
            raise BadLength(f"hello payload {len(b)}")
        fw, hw, cv, hz, _ = struct.unpack(cls.FMT, b)
        return cls(_cstr(fw), _cstr(hw), cv, hz)


@dataclass
class Heartbeat(Payload):
    TYPE: ClassVar[int] = MsgType.HEARTBEAT
    FMT: ClassVar[str] = "<IIII"
    uptime_s: int = 0
    frames_sent: int = 0
    config_version: int = 0
    reconnects: int = 0

    def pack(self) -> bytes:
        return struct.pack(self.FMT, self.uptime_s, self.frames_sent, self.config_version, self.reconnects)

    @classmethod
    def unpack(cls, b: bytes) -> "Heartbeat":
        if len(b) != 16:
            raise BadLength(f"heartbeat payload {len(b)}")
        return cls(*struct.unpack(cls.FMT, b))


@dataclass
class Log(Payload):
    TYPE: ClassVar[int] = MsgType.LOG
    level: int = 1
    message: str = ""

    def pack(self) -> bytes:
        return struct.pack("<B3x", self.level) + self.message.encode("utf-8")[: MAX_PAYLOAD - 4]

    @classmethod
    def unpack(cls, b: bytes) -> "Log":
        if len(b) < 4:
            raise BadLength("log payload")
        return cls(b[0], b[4:].decode("utf-8", "replace"))

    @property
    def level_name(self) -> str:
        return LOG_LEVELS.get(self.level, str(self.level))


@dataclass
class ConfigSet(Payload):
    TYPE: ClassVar[int] = MsgType.CONFIG_SET
    FMT: ClassVar[str] = "<IHBBfff"
    config_version: int = 1
    telemetry_hz: int = 10
    log_level: int = 1
    rpm_limit: float = 6000.0
    temp_limit_c: float = 90.0
    current_limit_a: float = 120.0

    def pack(self) -> bytes:
        return struct.pack(self.FMT, self.config_version, self.telemetry_hz, self.log_level, 0,
                           self.rpm_limit, self.temp_limit_c, self.current_limit_a)

    @classmethod
    def unpack(cls, b: bytes) -> "ConfigSet":
        if len(b) != 20:
            raise BadLength("config_set payload")
        cv, hz, ll, _, rpm, temp, cur = struct.unpack(cls.FMT, b)
        return cls(cv, hz, ll, rpm, temp, cur)


@dataclass
class ConfigAck(Payload):
    TYPE: ClassVar[int] = MsgType.CONFIG_ACK
    config_version: int = 0
    status: int = 0

    def pack(self) -> bytes:
        return struct.pack("<IB3x", self.config_version, self.status)

    @classmethod
    def unpack(cls, b: bytes) -> "ConfigAck":
        if len(b) != 8:
            raise BadLength("config_ack payload")
        cv, st = struct.unpack("<IB3x", b)
        return cls(cv, st)

    @property
    def status_name(self) -> str:
        return CONFIG_ACK_STATUS.get(self.status, str(self.status))


@dataclass
class FwUpdate(Payload):
    TYPE: ClassVar[int] = MsgType.FW_UPDATE
    target_version: str = "1.0.1"
    image_size_bytes: int = 0
    image_crc: int = 0

    def pack(self) -> bytes:
        return struct.pack("<16sII", _pad(self.target_version, 16), self.image_size_bytes, self.image_crc)

    @classmethod
    def unpack(cls, b: bytes) -> "FwUpdate":
        if len(b) != 24:
            raise BadLength("fw_update payload")
        v, sz, crc = struct.unpack("<16sII", b)
        return cls(_cstr(v), sz, crc)


@dataclass
class FwAck(Payload):
    TYPE: ClassVar[int] = MsgType.FW_ACK
    status: int = 0
    version: str = ""

    def pack(self) -> bytes:
        return struct.pack("<B3x16s", self.status, _pad(self.version, 16))

    @classmethod
    def unpack(cls, b: bytes) -> "FwAck":
        if len(b) != 20:
            raise BadLength("fw_ack payload")
        st, v = struct.unpack("<B3x16s", b)
        return cls(st, _cstr(v))

    @property
    def status_name(self) -> str:
        return FW_ACK_STATUS.get(self.status, str(self.status))


@dataclass
class FaultInject(Payload):
    TYPE: ClassVar[int] = MsgType.FAULT_INJECT
    set_flags: int = 0
    clear_flags: int = 0
    duration_ms: int = 0

    def pack(self) -> bytes:
        return struct.pack("<III", self.set_flags, self.clear_flags, self.duration_ms)

    @classmethod
    def unpack(cls, b: bytes) -> "FaultInject":
        if len(b) != 12:
            raise BadLength("fault_inject payload")
        return cls(*struct.unpack("<III", b))


@dataclass
class FaultAck(Payload):
    TYPE: ClassVar[int] = MsgType.FAULT_ACK
    active_flags: int = 0

    def pack(self) -> bytes:
        return struct.pack("<I", self.active_flags)

    @classmethod
    def unpack(cls, b: bytes) -> "FaultAck":
        if len(b) != 4:
            raise BadLength("fault_ack payload")
        return cls(struct.unpack("<I", b)[0])


@dataclass
class Cmd(Payload):
    TYPE: ClassVar[int] = MsgType.CMD
    cmd: int = 0

    def pack(self) -> bytes:
        return struct.pack("<B3x", self.cmd)

    @classmethod
    def unpack(cls, b: bytes) -> "Cmd":
        if len(b) != 4:
            raise BadLength("cmd payload")
        return cls(b[0])


PAYLOAD_CLASSES: dict[int, type[Payload]] = {
    c.TYPE: c for c in (Telemetry, Hello, Heartbeat, Log, ConfigSet, ConfigAck, FwUpdate, FwAck, FaultInject, FaultAck, Cmd)
}


# ---------------------------------------------------------------- frames


@dataclass
class Frame:
    header: Header
    payload: Payload | bytes
    raw: bytes = field(default=b"", repr=False)

    @property
    def msg_type(self) -> int:
        return self.header.msg_type

    @property
    def device_id(self) -> str:
        return self.header.device_id


def encode(msg_type: int, device_id: str, seq: int, ts_ms: int, fw_version: int, payload: Payload | bytes) -> bytes:
    body = payload if isinstance(payload, (bytes, bytearray)) else payload.pack()
    if len(body) > MAX_PAYLOAD:
        raise BadLength(f"payload too big {len(body)}")
    hdr = Header(msg_type, device_id, seq, ts_ms, fw_version, len(body)).pack()
    frame = hdr + bytes(body)
    return frame + struct.pack("<I", zlib.crc32(frame) & 0xFFFFFFFF)


def encode_frame(device_id: str, seq: int, ts_ms: int, fw_version: int | str, payload: Payload) -> bytes:
    fw = fw_encode(fw_version) if isinstance(fw_version, str) else fw_version
    return encode(payload.TYPE, device_id, seq, ts_ms, fw, payload)


def frame_length(buf: bytes) -> int | None:
    """Total frame length given at least a header, else None."""
    if len(buf) < HEADER_LEN:
        return None
    plen = struct.unpack_from("<H", buf, 32)[0]
    return HEADER_LEN + plen + 4


def decode(buf: bytes) -> Frame:
    """Decode one full frame. Raises ProtocolError subclasses on bad magic/version/CRC/length."""
    hdr = Header.unpack(buf)
    total = HEADER_LEN + hdr.payload_len + 4
    if len(buf) < total:
        raise BadLength(f"need {total} have {len(buf)}")
    body = buf[:total]
    (crc,) = struct.unpack_from("<I", body, total - 4)
    if zlib.crc32(body[: total - 4]) & 0xFFFFFFFF != crc:
        raise BadCRC("crc mismatch")
    raw_payload = body[HEADER_LEN : total - 4]
    cls = PAYLOAD_CLASSES.get(hdr.msg_type)
    payload: Payload | bytes = cls.unpack(raw_payload) if cls else raw_payload
    return Frame(hdr, payload, body)


class StreamDecoder:
    """Incremental decoder for the TCP byte stream (frames concatenated back-to-back)."""

    def __init__(self):
        self.buf = bytearray()
        self.bad = 0

    def feed(self, data: bytes):
        self.buf += data
        while True:
            if len(self.buf) < HEADER_LEN:
                return
            try:
                hdr = Header.unpack(bytes(self.buf[:HEADER_LEN]))
            except ProtocolError:
                # resync: drop one byte and search for magic
                self.bad += 1
                del self.buf[0]
                idx = self.buf.find(b"TE")
                if idx < 0:
                    self.buf.clear()
                    return
                del self.buf[:idx]
                continue
            total = HEADER_LEN + hdr.payload_len + 4
            if len(self.buf) < total:
                return
            chunk = bytes(self.buf[:total])
            del self.buf[:total]
            try:
                yield decode(chunk)
            except ProtocolError:
                self.bad += 1
