import struct
import zlib

import pytest

from app import protocol as P


def test_header_layout_hand_built():
    # Hand-built HELLO frame per protocol.md offsets
    payload = b"1.2.3".ljust(16, b"\0") + b"SIM-MOTOR-A".ljust(16, b"\0") + struct.pack("<IHH", 7, 10, 0)
    hdr = (b"TE" + bytes([1, 0x02]) + b"ECU-0001".ljust(12, b"\0") + struct.pack("<I", 42)
           + struct.pack("<Q", 1700000000123) + struct.pack("<I", (1 << 16) | (2 << 8) | 3)
           + struct.pack("<H", len(payload)) + b"\0\0")
    assert len(hdr) == 36
    body = hdr + payload
    frame = body + struct.pack("<I", zlib.crc32(body))
    assert struct.unpack_from("<H", frame, 0)[0] == 0x4554
    assert frame[2] == 1 and frame[3] == 0x02
    assert struct.unpack_from("<I", frame, 16)[0] == 42
    assert struct.unpack_from("<Q", frame, 20)[0] == 1700000000123
    assert struct.unpack_from("<I", frame, 28)[0] == 0x010203
    assert struct.unpack_from("<H", frame, 32)[0] == 40

    f = P.decode(frame)
    assert f.header.device_id == "ECU-0001" and f.header.seq == 42 and f.header.ts_ms == 1700000000123
    assert P.fw_decode(f.header.fw_version) == "1.2.3"
    assert isinstance(f.payload, P.Hello)
    assert f.payload.fw_string == "1.2.3" and f.payload.hw_model == "SIM-MOTOR-A"
    assert f.payload.config_version == 7 and f.payload.telemetry_hz == 10

    # our encoder reproduces the exact bytes
    assert P.encode_frame("ECU-0001", 42, 1700000000123, "1.2.3", P.Hello("1.2.3", "SIM-MOTOR-A", 7, 10)) == frame


def test_telemetry_roundtrip_and_layout():
    t = P.Telemetry(rpm=3210.5, temp_c=71.25, current_a=88.0, voltage_v=95.9, vibration_g=0.42, speed_kph=61.0,
                    lat=51.5074, lon=-0.1278, fault_flags=0x09, state=2)
    frame = P.encode_frame("ECU-0002", 1, 5, 0x00010000, t)
    assert len(frame) == 36 + 48 + 4
    assert struct.unpack_from("<f", frame, 36)[0] == pytest.approx(3210.5)
    assert struct.unpack_from("<d", frame, 36 + 24)[0] == pytest.approx(51.5074)
    assert struct.unpack_from("<I", frame, 36 + 40)[0] == 0x09
    assert frame[36 + 44] == 2
    d = P.decode(frame)
    assert d.payload.as_dict()["faults"] == ["OVERTEMP", "VIBRATION_HIGH"]
    assert d.payload.state == 2 and d.payload.lon == pytest.approx(-0.1278)


@pytest.mark.parametrize("payload", [
    P.Heartbeat(100, 2000, 3, 1), P.Log(2, "hello wörld"), P.ConfigSet(5, 20, 0, 5500.0, 85.5, 100.0),
    P.ConfigAck(5, 0), P.FwUpdate("2.0.0", 1_500_000, 0xDEADBEEF), P.FwAck(1, "2.0.0"),
    P.FaultInject(0x03, 0x20, 5000), P.FaultAck(0x03), P.Cmd(2),
])
def test_payload_roundtrips(payload):
    frame = P.encode_frame("ECU-0003", 9, 123456, "1.0.0", payload)
    d = P.decode(frame)
    assert d.header.msg_type == payload.TYPE
    assert d.payload == payload


def test_payload_sizes():
    assert len(P.Hello().pack()) == 40
    assert len(P.Heartbeat().pack()) == 16
    assert len(P.ConfigSet().pack()) == 20
    assert len(P.ConfigAck().pack()) == 8
    assert len(P.FwUpdate().pack()) == 24
    assert len(P.FwAck().pack()) == 20
    assert len(P.FaultInject().pack()) == 12
    assert len(P.FaultAck().pack()) == 4
    assert len(P.Cmd().pack()) == 4
    assert len(P.Telemetry().pack()) == 48


def test_bad_crc_magic_version():
    frame = bytearray(P.encode_frame("ECU-0001", 1, 1, "1.0.0", P.Heartbeat()))
    bad = bytearray(frame); bad[40] ^= 0xFF
    with pytest.raises(P.BadCRC):
        P.decode(bytes(bad))
    bad = bytearray(frame); bad[0] = 0
    with pytest.raises(P.BadMagic):
        P.decode(bytes(bad))
    bad = bytearray(frame); bad[2] = 2
    with pytest.raises(P.BadVersion):
        P.decode(bytes(bad))
    with pytest.raises(P.BadLength):
        P.decode(bytes(frame[:-1]))


def test_flags_and_fw():
    assert P.names_to_flags(["OVERTEMP", "gps_lost"]) == 0x21
    assert P.flags_to_names(0xFF) == list(P.FAULT_FLAGS)
    assert P.fw_encode("1.2.3") == 0x010203 and P.fw_decode(0x010203) == "1.2.3"
    with pytest.raises(P.ProtocolError):
        P.fw_encode("1.2")
    with pytest.raises(P.ProtocolError):
        P.names_to_flags(["NOPE"])


def test_stream_decoder_reassembles_and_resyncs():
    a = P.encode_frame("ECU-0001", 1, 1, "1.0.0", P.Hello())
    b = P.encode_frame("ECU-0001", 2, 2, "1.0.0", P.Heartbeat(1, 2, 3, 4))
    c = P.encode_frame("ECU-0001", 3, 3, "1.0.0", P.Log(1, "x"))
    stream = b"\x00garbage" + a + b + c
    dec = P.StreamDecoder()
    out = []
    for i in range(0, len(stream), 7):  # feed in odd-sized chunks
        out += list(dec.feed(stream[i:i + 7]))
    assert [f.msg_type for f in out] == [P.MsgType.HELLO, P.MsgType.HEARTBEAT, P.MsgType.LOG]
    assert out[1].payload.reconnects == 4
