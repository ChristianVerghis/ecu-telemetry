// ECU Telemetry Wire Protocol v1 — see docs/protocol.md (binding contract).
// All integers are little-endian on the wire. Every frame (UDP and TCP) shares
// a 36-byte header followed by a payload and a trailing CRC-32 over header+payload.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace ecu {

constexpr uint16_t kMagic = 0x4554;     // "TE" on the wire (LE: 0x54 0x45)
constexpr uint8_t kProtoVersion = 1;
constexpr size_t kHeaderSize = 36;
constexpr size_t kCrcSize = 4;
constexpr size_t kMaxPayload = 1024;
constexpr size_t kDeviceIdLen = 12;

enum class MsgType : uint8_t {
  TELEMETRY = 0x01,
  HELLO = 0x02,
  HEARTBEAT = 0x03,
  LOG = 0x04,
  CONFIG_SET = 0x10,
  CONFIG_ACK = 0x11,
  FW_UPDATE = 0x12,
  FW_ACK = 0x13,
  FAULT_INJECT = 0x14,
  FAULT_ACK = 0x15,
  CMD = 0x20,
};
const char* msgTypeName(MsgType t);

// Fault flag bits (TELEMETRY.fault_flags, FAULT_INJECT, FAULT_ACK).
enum Fault : uint32_t {
  F_OVERTEMP = 0x01,
  F_OVERCURRENT = 0x02,
  F_UNDERVOLTAGE = 0x04,
  F_VIBRATION_HIGH = 0x08,
  F_SENSOR_STUCK = 0x10,
  F_GPS_LOST = 0x20,
  F_COMM_DEGRADED = 0x40,
  F_ENCODER_FAULT = 0x80,
  F_ALL = 0xFF,
};
// Name of a single flag bit ("OVERTEMP"), or "UNKNOWN".
const char* faultName(uint32_t bit);
// Parse a single flag name (case-insensitive) into its bit. Returns false if unknown.
bool parseFaultName(const std::string& name, uint32_t& bit);
// "OVERTEMP|GPS_LOST" style rendering of a mask ("NONE" when empty).
std::string faultFlagsToString(uint32_t mask);

enum class DeviceState : uint8_t { IDLE = 0, RUNNING = 1, DEGRADED = 2, FAULT = 3 };
const char* stateName(DeviceState s);

// Firmware version helpers: "1.2.3" <-> (major<<16)|(minor<<8)|patch.
bool parseFwVersion(const std::string& s, uint32_t& out);
std::string fwVersionToString(uint32_t v);

// IEEE 802.3 CRC-32 (same polynomial/init/final-xor as zlib.crc32).
uint32_t crc32(const uint8_t* data, size_t len, uint32_t seed = 0);

// ---------------------------------------------------------------------------
// Little-endian byte (de)serialisation helpers.
class ByteWriter {
 public:
  void u8(uint8_t v);
  void u16(uint16_t v);
  void u32(uint32_t v);
  void u64(uint64_t v);
  void f32(float v);
  void f64(double v);
  void bytes(const void* p, size_t n);
  // Fixed-width, NUL-padded ASCII field (truncates if longer than n).
  void fixedStr(const std::string& s, size_t n);
  const std::vector<uint8_t>& data() const { return buf_; }
  std::vector<uint8_t> take() { return std::move(buf_); }

 private:
  std::vector<uint8_t> buf_;
};

class ByteReader {
 public:
  ByteReader(const uint8_t* p, size_t n) : p_(p), n_(n) {}
  uint8_t u8();
  uint16_t u16();
  uint32_t u32();
  uint64_t u64();
  float f32();
  double f64();
  std::string fixedStr(size_t n);   // stops at first NUL
  std::string rest();               // remaining bytes as string
  void skip(size_t n);
  bool ok() const { return ok_; }   // false once any read ran past the end
  size_t remaining() const { return n_ - pos_; }

 private:
  bool need(size_t n);
  const uint8_t* p_;
  size_t n_;
  size_t pos_ = 0;
  bool ok_ = true;
};

// ---------------------------------------------------------------------------
// Frame header (36 bytes on the wire).
struct FrameHeader {
  uint16_t magic = kMagic;
  uint8_t version = kProtoVersion;
  MsgType msg_type = MsgType::TELEMETRY;
  std::string device_id;      // <= 12 ASCII chars
  uint32_t seq = 0;
  uint64_t ts_ms = 0;
  uint32_t fw_version = 0;
  uint16_t payload_len = 0;   // filled in by encodeFrame
  uint16_t reserved = 0;
};

struct Frame {
  FrameHeader hdr;
  std::vector<uint8_t> payload;
};

enum class DecodeError { OK, TOO_SHORT, BAD_MAGIC, BAD_VERSION, BAD_LENGTH, BAD_CRC };
const char* decodeErrorName(DecodeError e);

// Serialise header + payload + CRC. Payload must be <= kMaxPayload.
std::vector<uint8_t> encodeFrame(const FrameHeader& hdr, const std::vector<uint8_t>& payload);

// Parse one frame from the front of [data, data+len). On OK, `consumed` is the
// number of bytes used. On TOO_SHORT nothing is consumed (caller should read
// more). On any other error the caller should drop the stream / datagram.
DecodeError decodeFrame(const uint8_t* data, size_t len, Frame& out, size_t& consumed);

// ---------------------------------------------------------------------------
// Message payloads. Each has encode() -> bytes and static decode(bytes, out).

struct Telemetry {  // 0x01, 48 bytes
  float rpm = 0, temp_c = 0, current_a = 0, voltage_v = 0, vibration_g = 0, speed_kph = 0;
  double lat = 0, lon = 0;
  uint32_t fault_flags = 0;
  DeviceState state = DeviceState::IDLE;
  std::vector<uint8_t> encode() const;
  static bool decode(const std::vector<uint8_t>& p, Telemetry& out);
};

struct Hello {  // 0x02, 40 bytes
  std::string fw_string;   // "MAJOR.MINOR.PATCH", <= 15 chars
  std::string hw_model;    // <= 15 chars
  uint32_t config_version = 0;
  uint16_t telemetry_hz = 0;
  std::vector<uint8_t> encode() const;
  static bool decode(const std::vector<uint8_t>& p, Hello& out);
};

struct Heartbeat {  // 0x03, 16 bytes
  uint32_t uptime_s = 0, frames_sent = 0, config_version = 0, reconnects = 0;
  std::vector<uint8_t> encode() const;
  static bool decode(const std::vector<uint8_t>& p, Heartbeat& out);
};

struct LogMsg {  // 0x04, 4 + N bytes
  uint8_t level = 1;   // 0 debug, 1 info, 2 warn, 3 error
  std::string message; // utf8, truncated so the payload fits kMaxPayload
  std::vector<uint8_t> encode() const;
  static bool decode(const std::vector<uint8_t>& p, LogMsg& out);
};

struct ConfigSet {  // 0x10, 20 bytes
  uint32_t config_version = 0;
  uint16_t telemetry_hz = 0;
  uint8_t log_level = 1;
  float rpm_limit = 0, temp_limit_c = 0, current_limit_a = 0;
  std::vector<uint8_t> encode() const;
  static bool decode(const std::vector<uint8_t>& p, ConfigSet& out);
};

struct ConfigAck {  // 0x11, 8 bytes
  uint32_t config_version = 0;
  uint8_t status = 0;  // 0 applied, 1 rejected
  std::vector<uint8_t> encode() const;
  static bool decode(const std::vector<uint8_t>& p, ConfigAck& out);
};

struct FwUpdate {  // 0x12, 24 bytes
  std::string target_version;  // <= 15 chars
  uint32_t image_size_bytes = 0;
  uint32_t image_crc = 0;
  std::vector<uint8_t> encode() const;
  static bool decode(const std::vector<uint8_t>& p, FwUpdate& out);
};

enum class FwAckStatus : uint8_t { ACCEPTED = 0, DOWNLOADING = 1, APPLIED = 2, REJECTED = 3 };
struct FwAck {  // 0x13, 20 bytes
  FwAckStatus status = FwAckStatus::ACCEPTED;
  std::string version;  // <= 15 chars
  std::vector<uint8_t> encode() const;
  static bool decode(const std::vector<uint8_t>& p, FwAck& out);
};

struct FaultInject {  // 0x14, 12 bytes
  uint32_t set_flags = 0, clear_flags = 0, duration_ms = 0;  // duration 0 = until cleared
  std::vector<uint8_t> encode() const;
  static bool decode(const std::vector<uint8_t>& p, FaultInject& out);
};

struct FaultAck {  // 0x15, 4 bytes
  uint32_t active_flags = 0;
  std::vector<uint8_t> encode() const;
  static bool decode(const std::vector<uint8_t>& p, FaultAck& out);
};

enum class CmdCode : uint8_t { REBOOT = 0, CLEAR_FAULTS = 1, REQUEST_HELLO = 2 };
struct Cmd {  // 0x20, 4 bytes
  CmdCode cmd = CmdCode::REBOOT;
  std::vector<uint8_t> encode() const;
  static bool decode(const std::vector<uint8_t>& p, Cmd& out);
};

}  // namespace ecu
