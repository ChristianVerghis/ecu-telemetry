#include "ecu/protocol.hpp"

#include <cctype>
#include <cstdio>
#include <cstring>

namespace ecu {

// ---------------------------------------------------------------------------
// Names

const char* msgTypeName(MsgType t) {
  switch (t) {
    case MsgType::TELEMETRY: return "TELEMETRY";
    case MsgType::HELLO: return "HELLO";
    case MsgType::HEARTBEAT: return "HEARTBEAT";
    case MsgType::LOG: return "LOG";
    case MsgType::CONFIG_SET: return "CONFIG_SET";
    case MsgType::CONFIG_ACK: return "CONFIG_ACK";
    case MsgType::FW_UPDATE: return "FW_UPDATE";
    case MsgType::FW_ACK: return "FW_ACK";
    case MsgType::FAULT_INJECT: return "FAULT_INJECT";
    case MsgType::FAULT_ACK: return "FAULT_ACK";
    case MsgType::CMD: return "CMD";
  }
  return "UNKNOWN";
}

namespace {
struct FaultEntry {
  uint32_t bit;
  const char* name;
};
const FaultEntry kFaults[] = {
    {F_OVERTEMP, "OVERTEMP"},         {F_OVERCURRENT, "OVERCURRENT"},
    {F_UNDERVOLTAGE, "UNDERVOLTAGE"}, {F_VIBRATION_HIGH, "VIBRATION_HIGH"},
    {F_SENSOR_STUCK, "SENSOR_STUCK"}, {F_GPS_LOST, "GPS_LOST"},
    {F_COMM_DEGRADED, "COMM_DEGRADED"}, {F_ENCODER_FAULT, "ENCODER_FAULT"},
};
}  // namespace

const char* faultName(uint32_t bit) {
  for (const auto& f : kFaults)
    if (f.bit == bit) return f.name;
  return "UNKNOWN";
}

bool parseFaultName(const std::string& name, uint32_t& bit) {
  std::string up;
  for (char c : name) up.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(c))));
  for (const auto& f : kFaults) {
    if (up == f.name) {
      bit = f.bit;
      return true;
    }
  }
  return false;
}

std::string faultFlagsToString(uint32_t mask) {
  if (mask == 0) return "NONE";
  std::string s;
  for (const auto& f : kFaults) {
    if (mask & f.bit) {
      if (!s.empty()) s += '|';
      s += f.name;
    }
  }
  uint32_t unknown = mask & ~static_cast<uint32_t>(F_ALL);
  if (unknown) {
    char buf[16];
    std::snprintf(buf, sizeof buf, "0x%X", unknown);
    if (!s.empty()) s += '|';
    s += buf;
  }
  return s;
}

const char* stateName(DeviceState s) {
  switch (s) {
    case DeviceState::IDLE: return "IDLE";
    case DeviceState::RUNNING: return "RUNNING";
    case DeviceState::DEGRADED: return "DEGRADED";
    case DeviceState::FAULT: return "FAULT";
  }
  return "UNKNOWN";
}

bool parseFwVersion(const std::string& s, uint32_t& out) {
  // Strict "MAJOR.MINOR.PATCH", each a decimal number that fits its field.
  unsigned parts[3] = {0, 0, 0};
  int idx = 0;
  bool have_digit = false;
  for (char c : s) {
    if (c >= '0' && c <= '9') {
      parts[idx] = parts[idx] * 10 + static_cast<unsigned>(c - '0');
      if (parts[idx] > 65535) return false;
      have_digit = true;
    } else if (c == '.') {
      if (!have_digit || idx == 2) return false;
      ++idx;
      have_digit = false;
    } else {
      return false;
    }
  }
  if (idx != 2 || !have_digit) return false;
  if (parts[0] > 0xFFFF || parts[1] > 0xFF || parts[2] > 0xFF) return false;
  out = (parts[0] << 16) | (parts[1] << 8) | parts[2];
  return true;
}

std::string fwVersionToString(uint32_t v) {
  char buf[32];
  std::snprintf(buf, sizeof buf, "%u.%u.%u", (v >> 16) & 0xFFFF, (v >> 8) & 0xFF, v & 0xFF);
  return buf;
}

const char* decodeErrorName(DecodeError e) {
  switch (e) {
    case DecodeError::OK: return "OK";
    case DecodeError::TOO_SHORT: return "TOO_SHORT";
    case DecodeError::BAD_MAGIC: return "BAD_MAGIC";
    case DecodeError::BAD_VERSION: return "BAD_VERSION";
    case DecodeError::BAD_LENGTH: return "BAD_LENGTH";
    case DecodeError::BAD_CRC: return "BAD_CRC";
  }
  return "UNKNOWN";
}

// ---------------------------------------------------------------------------
// CRC-32 (IEEE 802.3, reflected, poly 0xEDB88320, init/xorout 0xFFFFFFFF).
// Table built once on first use; bit-identical to zlib.crc32.

namespace {
const uint32_t* crcTable() {
  static uint32_t table[256];
  static bool init = false;
  if (!init) {
    for (uint32_t i = 0; i < 256; ++i) {
      uint32_t c = i;
      for (int k = 0; k < 8; ++k) c = (c & 1) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
      table[i] = c;
    }
    init = true;
  }
  return table;
}
}  // namespace

uint32_t crc32(const uint8_t* data, size_t len, uint32_t seed) {
  const uint32_t* t = crcTable();
  uint32_t c = seed ^ 0xFFFFFFFFu;
  for (size_t i = 0; i < len; ++i) c = t[(c ^ data[i]) & 0xFF] ^ (c >> 8);
  return c ^ 0xFFFFFFFFu;
}

// ---------------------------------------------------------------------------
// ByteWriter / ByteReader

void ByteWriter::u8(uint8_t v) { buf_.push_back(v); }
void ByteWriter::u16(uint16_t v) {
  buf_.push_back(static_cast<uint8_t>(v));
  buf_.push_back(static_cast<uint8_t>(v >> 8));
}
void ByteWriter::u32(uint32_t v) {
  for (int i = 0; i < 4; ++i) buf_.push_back(static_cast<uint8_t>(v >> (8 * i)));
}
void ByteWriter::u64(uint64_t v) {
  for (int i = 0; i < 8; ++i) buf_.push_back(static_cast<uint8_t>(v >> (8 * i)));
}
void ByteWriter::f32(float v) {
  static_assert(sizeof(float) == 4, "float must be IEEE binary32");
  uint32_t u;
  std::memcpy(&u, &v, 4);
  u32(u);
}
void ByteWriter::f64(double v) {
  static_assert(sizeof(double) == 8, "double must be IEEE binary64");
  uint64_t u;
  std::memcpy(&u, &v, 8);
  u64(u);
}
void ByteWriter::bytes(const void* p, size_t n) {
  const uint8_t* b = static_cast<const uint8_t*>(p);
  buf_.insert(buf_.end(), b, b + n);
}
void ByteWriter::fixedStr(const std::string& s, size_t n) {
  for (size_t i = 0; i < n; ++i) buf_.push_back(i < s.size() ? static_cast<uint8_t>(s[i]) : 0);
}

bool ByteReader::need(size_t n) {
  if (!ok_ || pos_ + n > n_) {
    ok_ = false;
    return false;
  }
  return true;
}
uint8_t ByteReader::u8() {
  if (!need(1)) return 0;
  return p_[pos_++];
}
uint16_t ByteReader::u16() {
  if (!need(2)) return 0;
  uint16_t v = static_cast<uint16_t>(p_[pos_] | (p_[pos_ + 1] << 8));
  pos_ += 2;
  return v;
}
uint32_t ByteReader::u32() {
  if (!need(4)) return 0;
  uint32_t v = 0;
  for (int i = 3; i >= 0; --i) v = (v << 8) | p_[pos_ + static_cast<size_t>(i)];
  pos_ += 4;
  return v;
}
uint64_t ByteReader::u64() {
  if (!need(8)) return 0;
  uint64_t v = 0;
  for (int i = 7; i >= 0; --i) v = (v << 8) | p_[pos_ + static_cast<size_t>(i)];
  pos_ += 8;
  return v;
}
float ByteReader::f32() {
  uint32_t u = u32();
  float f;
  std::memcpy(&f, &u, 4);
  return f;
}
double ByteReader::f64() {
  uint64_t u = u64();
  double d;
  std::memcpy(&d, &u, 8);
  return d;
}
std::string ByteReader::fixedStr(size_t n) {
  if (!need(n)) return {};
  std::string s;
  for (size_t i = 0; i < n && p_[pos_ + i] != 0; ++i) s.push_back(static_cast<char>(p_[pos_ + i]));
  pos_ += n;
  return s;
}
std::string ByteReader::rest() {
  if (!ok_) return {};
  std::string s(reinterpret_cast<const char*>(p_ + pos_), n_ - pos_);
  pos_ = n_;
  return s;
}
void ByteReader::skip(size_t n) {
  if (need(n)) pos_ += n;
}

// ---------------------------------------------------------------------------
// Frame

std::vector<uint8_t> encodeFrame(const FrameHeader& hdr, const std::vector<uint8_t>& payload) {
  ByteWriter w;
  w.u16(hdr.magic);
  w.u8(hdr.version);
  w.u8(static_cast<uint8_t>(hdr.msg_type));
  w.fixedStr(hdr.device_id, kDeviceIdLen);
  w.u32(hdr.seq);
  w.u64(hdr.ts_ms);
  w.u32(hdr.fw_version);
  size_t n = payload.size() > kMaxPayload ? kMaxPayload : payload.size();
  w.u16(static_cast<uint16_t>(n));
  w.u16(hdr.reserved);
  w.bytes(payload.data(), n);
  uint32_t crc = crc32(w.data().data(), w.data().size());
  w.u32(crc);
  return w.take();
}

DecodeError decodeFrame(const uint8_t* data, size_t len, Frame& out, size_t& consumed) {
  consumed = 0;
  if (len < kHeaderSize) return DecodeError::TOO_SHORT;
  ByteReader r(data, len);
  FrameHeader h;
  h.magic = r.u16();
  h.version = r.u8();
  h.msg_type = static_cast<MsgType>(r.u8());
  h.device_id = r.fixedStr(kDeviceIdLen);
  h.seq = r.u32();
  h.ts_ms = r.u64();
  h.fw_version = r.u32();
  h.payload_len = r.u16();
  h.reserved = r.u16();
  if (h.magic != kMagic) return DecodeError::BAD_MAGIC;
  if (h.version != kProtoVersion) return DecodeError::BAD_VERSION;
  if (h.payload_len > kMaxPayload) return DecodeError::BAD_LENGTH;
  size_t total = kHeaderSize + h.payload_len + kCrcSize;
  if (len < total) return DecodeError::TOO_SHORT;
  uint32_t expect = crc32(data, kHeaderSize + h.payload_len);
  ByteReader cr(data + kHeaderSize + h.payload_len, kCrcSize);
  if (cr.u32() != expect) return DecodeError::BAD_CRC;
  out.hdr = h;
  out.payload.assign(data + kHeaderSize, data + kHeaderSize + h.payload_len);
  consumed = total;
  return DecodeError::OK;
}

// ---------------------------------------------------------------------------
// Payloads

std::vector<uint8_t> Telemetry::encode() const {
  ByteWriter w;
  w.f32(rpm);
  w.f32(temp_c);
  w.f32(current_a);
  w.f32(voltage_v);
  w.f32(vibration_g);
  w.f32(speed_kph);
  w.f64(lat);
  w.f64(lon);
  w.u32(fault_flags);
  w.u8(static_cast<uint8_t>(state));
  w.u8(0);
  w.u8(0);
  w.u8(0);
  return w.take();
}
bool Telemetry::decode(const std::vector<uint8_t>& p, Telemetry& o) {
  if (p.size() != 48) return false;
  ByteReader r(p.data(), p.size());
  o.rpm = r.f32();
  o.temp_c = r.f32();
  o.current_a = r.f32();
  o.voltage_v = r.f32();
  o.vibration_g = r.f32();
  o.speed_kph = r.f32();
  o.lat = r.f64();
  o.lon = r.f64();
  o.fault_flags = r.u32();
  o.state = static_cast<DeviceState>(r.u8());
  return r.ok();
}

std::vector<uint8_t> Hello::encode() const {
  ByteWriter w;
  w.fixedStr(fw_string, 16);
  w.fixedStr(hw_model, 16);
  w.u32(config_version);
  w.u16(telemetry_hz);
  w.u16(0);
  return w.take();
}
bool Hello::decode(const std::vector<uint8_t>& p, Hello& o) {
  if (p.size() != 40) return false;
  ByteReader r(p.data(), p.size());
  o.fw_string = r.fixedStr(16);
  o.hw_model = r.fixedStr(16);
  o.config_version = r.u32();
  o.telemetry_hz = r.u16();
  return r.ok();
}

std::vector<uint8_t> Heartbeat::encode() const {
  ByteWriter w;
  w.u32(uptime_s);
  w.u32(frames_sent);
  w.u32(config_version);
  w.u32(reconnects);
  return w.take();
}
bool Heartbeat::decode(const std::vector<uint8_t>& p, Heartbeat& o) {
  if (p.size() != 16) return false;
  ByteReader r(p.data(), p.size());
  o.uptime_s = r.u32();
  o.frames_sent = r.u32();
  o.config_version = r.u32();
  o.reconnects = r.u32();
  return r.ok();
}

std::vector<uint8_t> LogMsg::encode() const {
  ByteWriter w;
  w.u8(level);
  w.u8(0);
  w.u8(0);
  w.u8(0);
  size_t n = message.size();
  if (n > kMaxPayload - 4) n = kMaxPayload - 4;
  w.bytes(message.data(), n);
  return w.take();
}
bool LogMsg::decode(const std::vector<uint8_t>& p, LogMsg& o) {
  if (p.size() < 4) return false;
  ByteReader r(p.data(), p.size());
  o.level = r.u8();
  r.skip(3);
  o.message = r.rest();
  return r.ok();
}

std::vector<uint8_t> ConfigSet::encode() const {
  ByteWriter w;
  w.u32(config_version);
  w.u16(telemetry_hz);
  w.u8(log_level);
  w.u8(0);
  w.f32(rpm_limit);
  w.f32(temp_limit_c);
  w.f32(current_limit_a);
  return w.take();
}
bool ConfigSet::decode(const std::vector<uint8_t>& p, ConfigSet& o) {
  if (p.size() != 20) return false;
  ByteReader r(p.data(), p.size());
  o.config_version = r.u32();
  o.telemetry_hz = r.u16();
  o.log_level = r.u8();
  r.skip(1);
  o.rpm_limit = r.f32();
  o.temp_limit_c = r.f32();
  o.current_limit_a = r.f32();
  return r.ok();
}

std::vector<uint8_t> ConfigAck::encode() const {
  ByteWriter w;
  w.u32(config_version);
  w.u8(status);
  w.u8(0);
  w.u8(0);
  w.u8(0);
  return w.take();
}
bool ConfigAck::decode(const std::vector<uint8_t>& p, ConfigAck& o) {
  if (p.size() != 8) return false;
  ByteReader r(p.data(), p.size());
  o.config_version = r.u32();
  o.status = r.u8();
  return r.ok();
}

std::vector<uint8_t> FwUpdate::encode() const {
  ByteWriter w;
  w.fixedStr(target_version, 16);
  w.u32(image_size_bytes);
  w.u32(image_crc);
  return w.take();
}
bool FwUpdate::decode(const std::vector<uint8_t>& p, FwUpdate& o) {
  if (p.size() != 24) return false;
  ByteReader r(p.data(), p.size());
  o.target_version = r.fixedStr(16);
  o.image_size_bytes = r.u32();
  o.image_crc = r.u32();
  return r.ok();
}

std::vector<uint8_t> FwAck::encode() const {
  ByteWriter w;
  w.u8(static_cast<uint8_t>(status));
  w.u8(0);
  w.u8(0);
  w.u8(0);
  w.fixedStr(version, 16);
  return w.take();
}
bool FwAck::decode(const std::vector<uint8_t>& p, FwAck& o) {
  if (p.size() != 20) return false;
  ByteReader r(p.data(), p.size());
  o.status = static_cast<FwAckStatus>(r.u8());
  r.skip(3);
  o.version = r.fixedStr(16);
  return r.ok();
}

std::vector<uint8_t> FaultInject::encode() const {
  ByteWriter w;
  w.u32(set_flags);
  w.u32(clear_flags);
  w.u32(duration_ms);
  return w.take();
}
bool FaultInject::decode(const std::vector<uint8_t>& p, FaultInject& o) {
  if (p.size() != 12) return false;
  ByteReader r(p.data(), p.size());
  o.set_flags = r.u32();
  o.clear_flags = r.u32();
  o.duration_ms = r.u32();
  return r.ok();
}

std::vector<uint8_t> FaultAck::encode() const {
  ByteWriter w;
  w.u32(active_flags);
  return w.take();
}
bool FaultAck::decode(const std::vector<uint8_t>& p, FaultAck& o) {
  if (p.size() != 4) return false;
  ByteReader r(p.data(), p.size());
  o.active_flags = r.u32();
  return r.ok();
}

std::vector<uint8_t> Cmd::encode() const {
  ByteWriter w;
  w.u8(static_cast<uint8_t>(cmd));
  w.u8(0);
  w.u8(0);
  w.u8(0);
  return w.take();
}
bool Cmd::decode(const std::vector<uint8_t>& p, Cmd& o) {
  if (p.size() != 4) return false;
  ByteReader r(p.data(), p.size());
  o.cmd = static_cast<CmdCode>(r.u8());
  return r.ok();
}

}  // namespace ecu
