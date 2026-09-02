// Tiny self-contained test runner (no gtest). Each test is a function; CTest
// invokes this binary once per test name (see CMakeLists.txt). With no args,
// all tests run.
#include <cmath>
#include <cstdio>
#include <cstring>
#include <functional>
#include <string>
#include <vector>

#include "ecu/agent.hpp"
#include "ecu/config.hpp"
#include "ecu/physics.hpp"
#include "ecu/protocol.hpp"

using namespace ecu;

namespace {
int g_failures = 0;

#define CHECK(cond)                                                                   \
  do {                                                                                \
    if (!(cond)) {                                                                    \
      std::fprintf(stderr, "  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);          \
      ++g_failures;                                                                   \
    }                                                                                 \
  } while (0)
#define CHECK_EQ(a, b) CHECK((a) == (b))
#define CHECK_NEAR(a, b, eps) CHECK(std::fabs(static_cast<double>(a) - static_cast<double>(b)) <= (eps))

// Helper: encode a frame for a payload and decode it again.
Frame roundTrip(MsgType type, const std::vector<uint8_t>& payload) {
  FrameHeader h;
  h.msg_type = type;
  h.device_id = "ECU-0001";
  h.seq = 12345;
  h.ts_ms = 1700000000123ull;
  h.fw_version = 0x010203;
  std::vector<uint8_t> bytes = encodeFrame(h, payload);
  Frame f;
  size_t used = 0;
  DecodeError e = decodeFrame(bytes.data(), bytes.size(), f, used);
  CHECK_EQ(e, DecodeError::OK);
  CHECK_EQ(used, bytes.size());
  CHECK_EQ(f.hdr.msg_type, type);
  CHECK_EQ(f.hdr.device_id, "ECU-0001");
  CHECK_EQ(f.hdr.seq, 12345u);
  CHECK_EQ(f.hdr.ts_ms, 1700000000123ull);
  CHECK_EQ(f.hdr.fw_version, 0x010203u);
  CHECK_EQ(f.payload, payload);
  return f;
}

// ---------------------------------------------------------------------------

void test_crc32() {
  const char* s = "123456789";
  CHECK_EQ(crc32(reinterpret_cast<const uint8_t*>(s), 9), 0xCBF43926u);
  CHECK_EQ(crc32(nullptr, 0), 0u);
  // Incremental == one-shot (seed chaining as zlib.crc32(data, value)).
  uint32_t part = crc32(reinterpret_cast<const uint8_t*>(s), 4);
  CHECK_EQ(crc32(reinterpret_cast<const uint8_t*>(s) + 4, 5, part), 0xCBF43926u);
}

void test_header_layout() {
  FrameHeader h;
  h.msg_type = MsgType::HELLO;
  h.device_id = "ECU-0001";
  h.seq = 0x04030201;
  h.ts_ms = 0x0807060504030201ull;
  h.fw_version = 0x00010203;  // 1.2.3
  std::vector<uint8_t> payload = {0xAA, 0xBB, 0xCC};
  std::vector<uint8_t> b = encodeFrame(h, payload);
  CHECK_EQ(b.size(), kHeaderSize + 3 + kCrcSize);
  CHECK_EQ(b[0], 0x54);  // magic LE: 'T'
  CHECK_EQ(b[1], 0x45);  //           'E'
  CHECK_EQ(b[2], 1);     // version
  CHECK_EQ(b[3], 0x02);  // msg_type HELLO
  CHECK_EQ(std::memcmp(&b[4], "ECU-0001\0\0\0\0", 12), 0);
  CHECK_EQ(b[16], 0x01); CHECK_EQ(b[17], 0x02); CHECK_EQ(b[18], 0x03); CHECK_EQ(b[19], 0x04);  // seq LE
  for (int i = 0; i < 8; ++i) CHECK_EQ(b[20 + i], i + 1);  // ts_ms LE
  CHECK_EQ(b[28], 0x03); CHECK_EQ(b[29], 0x02); CHECK_EQ(b[30], 0x01); CHECK_EQ(b[31], 0x00);  // fw
  CHECK_EQ(b[32], 3); CHECK_EQ(b[33], 0);  // payload_len
  CHECK_EQ(b[34], 0); CHECK_EQ(b[35], 0);  // reserved
  CHECK_EQ(b[36], 0xAA); CHECK_EQ(b[38], 0xCC);
  uint32_t crc = crc32(b.data(), 39);
  CHECK_EQ(b[39], crc & 0xFF);
  CHECK_EQ(b[42], (crc >> 24) & 0xFF);

  // Corruption is detected and classified.
  Frame f;
  size_t used = 0;
  std::vector<uint8_t> bad = b;
  bad[37] ^= 0xFF;
  CHECK_EQ(decodeFrame(bad.data(), bad.size(), f, used), DecodeError::BAD_CRC);
  bad = b; bad[0] = 0;
  CHECK_EQ(decodeFrame(bad.data(), bad.size(), f, used), DecodeError::BAD_MAGIC);
  bad = b; bad[2] = 2;
  CHECK_EQ(decodeFrame(bad.data(), bad.size(), f, used), DecodeError::BAD_VERSION);
  bad = b; bad[33] = 0x10;  // payload_len 4099 > max
  CHECK_EQ(decodeFrame(bad.data(), bad.size(), f, used), DecodeError::BAD_LENGTH);
  CHECK_EQ(decodeFrame(b.data(), b.size() - 1, f, used), DecodeError::TOO_SHORT);
  CHECK_EQ(used, 0u);
  // Two concatenated frames on a stream: first decode consumes exactly one.
  std::vector<uint8_t> two = b;
  two.insert(two.end(), b.begin(), b.end());
  CHECK_EQ(decodeFrame(two.data(), two.size(), f, used), DecodeError::OK);
  CHECK_EQ(used, b.size());
}

void test_roundtrip_all_messages() {
  {
    Telemetry t;
    t.rpm = 3210.5f; t.temp_c = 71.25f; t.current_a = 88.0f; t.voltage_v = 95.5f;
    t.vibration_g = 0.42f; t.speed_kph = 64.0f; t.lat = 37.7749; t.lon = -122.4194;
    t.fault_flags = F_OVERTEMP | F_GPS_LOST; t.state = DeviceState::DEGRADED;
    auto p = t.encode();
    CHECK_EQ(p.size(), 48u);
    Frame f = roundTrip(MsgType::TELEMETRY, p);
    Telemetry o;
    CHECK(Telemetry::decode(f.payload, o));
    CHECK_EQ(o.rpm, t.rpm); CHECK_EQ(o.temp_c, t.temp_c); CHECK_EQ(o.current_a, t.current_a);
    CHECK_EQ(o.voltage_v, t.voltage_v); CHECK_EQ(o.vibration_g, t.vibration_g); CHECK_EQ(o.speed_kph, t.speed_kph);
    CHECK_EQ(o.lat, t.lat); CHECK_EQ(o.lon, t.lon); CHECK_EQ(o.fault_flags, t.fault_flags); CHECK_EQ(o.state, t.state);
    CHECK_EQ(p[40], 0x21);  // fault_flags at offset 40 (6*f32 + 2*f64)
    CHECK_EQ(p[44], 0x02);  // state at offset 44, then 3 pad bytes
    CHECK_EQ(p[45], 0x00); CHECK_EQ(p[47], 0x00);
  }
  {
    Hello h; h.fw_string = "1.2.3"; h.hw_model = "SIM-MOTOR-A"; h.config_version = 7; h.telemetry_hz = 20;
    auto p = h.encode(); CHECK_EQ(p.size(), 40u);
    Frame f = roundTrip(MsgType::HELLO, p);
    Hello o; CHECK(Hello::decode(f.payload, o));
    CHECK_EQ(o.fw_string, "1.2.3"); CHECK_EQ(o.hw_model, "SIM-MOTOR-A"); CHECK_EQ(o.config_version, 7u); CHECK_EQ(o.telemetry_hz, 20);
  }
  {
    Heartbeat h; h.uptime_s = 100; h.frames_sent = 2000; h.config_version = 3; h.reconnects = 4;
    auto p = h.encode(); CHECK_EQ(p.size(), 16u);
    Frame f = roundTrip(MsgType::HEARTBEAT, p);
    Heartbeat o; CHECK(Heartbeat::decode(f.payload, o));
    CHECK_EQ(o.uptime_s, 100u); CHECK_EQ(o.frames_sent, 2000u); CHECK_EQ(o.config_version, 3u); CHECK_EQ(o.reconnects, 4u);
  }
  {
    LogMsg l; l.level = 2; l.message = "temp 120.0C > limit 110.0C — überhitzt";
    auto p = l.encode(); CHECK_EQ(p.size(), 4 + l.message.size());
    Frame f = roundTrip(MsgType::LOG, p);
    LogMsg o; CHECK(LogMsg::decode(f.payload, o));
    CHECK_EQ(o.level, 2); CHECK_EQ(o.message, l.message);
    LogMsg big; big.message = std::string(5000, 'x');
    CHECK_EQ(big.encode().size(), kMaxPayload);  // truncated to fit
  }
  {
    ConfigSet c; c.config_version = 9; c.telemetry_hz = 50; c.log_level = 3; c.rpm_limit = 5500; c.temp_limit_c = 105.5f; c.current_limit_a = 140;
    auto p = c.encode(); CHECK_EQ(p.size(), 20u);
    Frame f = roundTrip(MsgType::CONFIG_SET, p);
    ConfigSet o; CHECK(ConfigSet::decode(f.payload, o));
    CHECK_EQ(o.config_version, 9u); CHECK_EQ(o.telemetry_hz, 50); CHECK_EQ(o.log_level, 3);
    CHECK_EQ(o.rpm_limit, 5500.0f); CHECK_EQ(o.temp_limit_c, 105.5f); CHECK_EQ(o.current_limit_a, 140.0f);
  }
  {
    ConfigAck a; a.config_version = 9; a.status = 1;
    auto p = a.encode(); CHECK_EQ(p.size(), 8u);
    Frame f = roundTrip(MsgType::CONFIG_ACK, p);
    ConfigAck o; CHECK(ConfigAck::decode(f.payload, o));
    CHECK_EQ(o.config_version, 9u); CHECK_EQ(o.status, 1);
  }
  {
    FwUpdate u; u.target_version = "1.3.0"; u.image_size_bytes = 1500000; u.image_crc = 0xDEADBEEF;
    auto p = u.encode(); CHECK_EQ(p.size(), 24u);
    Frame f = roundTrip(MsgType::FW_UPDATE, p);
    FwUpdate o; CHECK(FwUpdate::decode(f.payload, o));
    CHECK_EQ(o.target_version, "1.3.0"); CHECK_EQ(o.image_size_bytes, 1500000u); CHECK_EQ(o.image_crc, 0xDEADBEEFu);
  }
  {
    FwAck a; a.status = FwAckStatus::APPLIED; a.version = "1.3.0";
    auto p = a.encode(); CHECK_EQ(p.size(), 20u);
    Frame f = roundTrip(MsgType::FW_ACK, p);
    FwAck o; CHECK(FwAck::decode(f.payload, o));
    CHECK_EQ(o.status, FwAckStatus::APPLIED); CHECK_EQ(o.version, "1.3.0");
  }
  {
    FaultInject i; i.set_flags = F_OVERTEMP; i.clear_flags = F_GPS_LOST; i.duration_ms = 20000;
    auto p = i.encode(); CHECK_EQ(p.size(), 12u);
    Frame f = roundTrip(MsgType::FAULT_INJECT, p);
    FaultInject o; CHECK(FaultInject::decode(f.payload, o));
    CHECK_EQ(o.set_flags, 1u); CHECK_EQ(o.clear_flags, 0x20u); CHECK_EQ(o.duration_ms, 20000u);
  }
  {
    FaultAck a; a.active_flags = 0xC3;
    auto p = a.encode(); CHECK_EQ(p.size(), 4u);
    Frame f = roundTrip(MsgType::FAULT_ACK, p);
    FaultAck o; CHECK(FaultAck::decode(f.payload, o));
    CHECK_EQ(o.active_flags, 0xC3u);
  }
  {
    Cmd c; c.cmd = CmdCode::REQUEST_HELLO;
    auto p = c.encode(); CHECK_EQ(p.size(), 4u);
    Frame f = roundTrip(MsgType::CMD, p);
    Cmd o; CHECK(Cmd::decode(f.payload, o));
    CHECK_EQ(o.cmd, CmdCode::REQUEST_HELLO);
  }
  // Wrong-size payloads are rejected.
  Telemetry t; CHECK(!Telemetry::decode(std::vector<uint8_t>(47, 0), t));
  Hello h; CHECK(!Hello::decode(std::vector<uint8_t>(39, 0), h));
}

void test_fault_names() {
  uint32_t bit = 0;
  CHECK(parseFaultName("OVERTEMP", bit)); CHECK_EQ(bit, 0x01u);
  CHECK(parseFaultName("overcurrent", bit)); CHECK_EQ(bit, 0x02u);
  CHECK(parseFaultName("UNDERVOLTAGE", bit)); CHECK_EQ(bit, 0x04u);
  CHECK(parseFaultName("VIBRATION_HIGH", bit)); CHECK_EQ(bit, 0x08u);
  CHECK(parseFaultName("SENSOR_STUCK", bit)); CHECK_EQ(bit, 0x10u);
  CHECK(parseFaultName("GPS_LOST", bit)); CHECK_EQ(bit, 0x20u);
  CHECK(parseFaultName("COMM_DEGRADED", bit)); CHECK_EQ(bit, 0x40u);
  CHECK(parseFaultName("ENCODER_FAULT", bit)); CHECK_EQ(bit, 0x80u);
  CHECK(!parseFaultName("BOGUS", bit));
  CHECK(!parseFaultName("", bit));
  CHECK_EQ(std::string(faultName(0x20)), "GPS_LOST");
  CHECK_EQ(std::string(faultName(0x100)), "UNKNOWN");
  CHECK_EQ(faultFlagsToString(0), "NONE");
  CHECK_EQ(faultFlagsToString(F_OVERTEMP | F_ENCODER_FAULT), "OVERTEMP|ENCODER_FAULT");
  // Firmware version helpers
  uint32_t v = 0;
  CHECK(parseFwVersion("1.2.3", v)); CHECK_EQ(v, 0x010203u);
  CHECK(parseFwVersion("0.0.0", v)); CHECK_EQ(v, 0u);
  CHECK(!parseFwVersion("1.2", v)); CHECK(!parseFwVersion("1.2.3.4", v));
  CHECK(!parseFwVersion("a.b.c", v)); CHECK(!parseFwVersion("", v)); CHECK(!parseFwVersion("1..3", v));
  CHECK(!parseFwVersion("1.300.0", v));
  CHECK_EQ(fwVersionToString(0x020a01), "2.10.1");
}

void test_fault_spec_parsing() {
  FaultSpec fs;
  std::string err;
  CHECK(parseFaultSpec("OVERTEMP@30:20", fs, err));
  CHECK_EQ(fs.flag, F_OVERTEMP); CHECK_EQ(fs.start_s, 30.0); CHECK_EQ(fs.dur_s, 20.0);
  CHECK(parseFaultSpec("gps_lost@2.5", fs, err));
  CHECK_EQ(fs.flag, F_GPS_LOST); CHECK_EQ(fs.start_s, 2.5); CHECK_EQ(fs.dur_s, 0.0);
  CHECK(parseFaultSpec("ENCODER_FAULT@0:0", fs, err));
  CHECK_EQ(fs.flag, F_ENCODER_FAULT);
  CHECK(!parseFaultSpec("OVERTEMP", fs, err));
  CHECK(!parseFaultSpec("@30:20", fs, err));
  CHECK(!parseFaultSpec("NOPE@30:20", fs, err));
  CHECK(!parseFaultSpec("OVERTEMP@abc:20", fs, err));
  CHECK(!parseFaultSpec("OVERTEMP@30:xyz", fs, err));
  CHECK(!parseFaultSpec("OVERTEMP@-5:20", fs, err));
  CHECK(!parseFaultSpec("OVERTEMP@30:", fs, err));
}

void test_config_parsing() {
  Config c;
  std::string err;
  CHECK(parseConfigText("# comment\nhz = 25\nrpm_limit=5000\ntemp_limit_c=100.5\ncurrent_limit_a=120\nlog_level=warn\n", c, err));
  CHECK_EQ(c.hz, 25); CHECK_EQ(c.limits.rpm_limit, 5000.0f); CHECK_EQ(c.limits.temp_limit_c, 100.5f);
  CHECK_EQ(c.limits.current_limit_a, 120.0f); CHECK_EQ(c.log_level, LogLevel::WARN);
  CHECK(!parseConfigText("bogus=1\n", c, err));
  CHECK(!parseConfigText("hz=0\n", c, err));
  CHECK(!parseConfigText("no equals sign\n", c, err));
  // Round-trip through the text writer.
  Config d;
  CHECK(parseConfigText(configToText(c), d, err));
  CHECK_EQ(d.hz, c.hz); CHECK_EQ(d.limits.temp_limit_c, c.limits.temp_limit_c); CHECK_EQ(d.log_level, c.log_level);
}

void test_physics_determinism() {
  for (Profile prof : {Profile::CITY, Profile::HIGHWAY, Profile::IDLE, Profile::STRESS}) {
    Physics a(1234, prof), b(1234, prof), c(99, prof);
    bool any_diff_seed = false;
    for (int i = 0; i < 600; ++i) {
      a.step(0.1); b.step(0.1); c.step(0.1);
      const Telemetry& sa = a.sample();
      const Telemetry& sb = b.sample();
      CHECK_EQ(sa.rpm, sb.rpm); CHECK_EQ(sa.temp_c, sb.temp_c); CHECK_EQ(sa.current_a, sb.current_a);
      CHECK_EQ(sa.voltage_v, sb.voltage_v); CHECK_EQ(sa.vibration_g, sb.vibration_g);
      CHECK_EQ(sa.lat, sb.lat); CHECK_EQ(sa.lon, sb.lon); CHECK_EQ(sa.fault_flags, sb.fault_flags);
      if (sa.vibration_g != c.sample().vibration_g) any_diff_seed = true;
      if (g_failures) return;
    }
    CHECK(any_diff_seed);
  }
}

void test_physics_behaviour() {
  // City profile runs and produces plausible values; no faults under nominal limits.
  Physics city(42, Profile::CITY);
  bool ran = false;
  for (int i = 0; i < 1200; ++i) {
    city.step(0.1);
    const Telemetry& s = city.sample();
    if (s.state == DeviceState::RUNNING && s.rpm > 1000) ran = true;
    CHECK(s.rpm >= 0 && s.rpm < 7000);
    CHECK(s.voltage_v > 80 && s.voltage_v < 105);
    CHECK(s.temp_c > 20 && s.temp_c < 110);
    CHECK_NEAR(s.lat, 37.7749, 0.05);
    CHECK_EQ(s.fault_flags & (F_OVERTEMP | F_OVERCURRENT | F_UNDERVOLTAGE), 0u);
    if (g_failures) return;
  }
  CHECK(ran);
  // speed_kph is derived from rpm through the drivetrain
  Physics hw(1, Profile::HIGHWAY);
  for (int i = 0; i < 300; ++i) hw.step(0.1);
  CHECK(hw.sample().speed_kph > 50 && hw.sample().speed_kph < 120);
  CHECK_EQ(hw.state(), DeviceState::RUNNING);

  // Idle is IDLE and cold.
  Physics idle(7, Profile::IDLE);
  for (int i = 0; i < 100; ++i) idle.step(0.1);
  CHECK_EQ(idle.state(), DeviceState::IDLE);
  CHECK(idle.sample().rpm < 50);

  // COMM_DEGRADED is owned by the agent and gives DEGRADED state.
  idle.setCommDegraded(true);
  idle.step(0.1);
  CHECK(idle.faultFlags() & F_COMM_DEGRADED);
  CHECK_EQ(idle.state(), DeviceState::DEGRADED);
  idle.setCommDegraded(false);
}

void test_overtemp_injection() {
  Physics p(42, Profile::IDLE);
  for (int i = 0; i < 50; ++i) p.step(0.1);
  float before = p.sample().temp_c;
  p.setInjectedFaults(F_OVERTEMP);
  double t_cross = -1;
  for (int i = 0; i < 600; ++i) {  // 60 simulated seconds
    p.step(0.1);
    if (t_cross < 0 && p.sample().temp_c > p.limits().temp_limit_c) t_cross = (i + 1) * 0.1;
  }
  std::printf("  overtemp: %.1fC -> %.1fC (limit %.1fC) crossed at t=%.1fs\n", before, p.sample().temp_c,
              p.limits().temp_limit_c, t_cross);
  CHECK(t_cross > 0);
  CHECK(p.sample().temp_c > p.limits().temp_limit_c);
  CHECK(p.detectedFaults() & F_OVERTEMP);
  CHECK_EQ(p.state(), DeviceState::FAULT);
  // Clearing the injection lets it cool back down (reading follows the plant).
  p.setInjectedFaults(0);
  for (int i = 0; i < 3000; ++i) p.step(0.1);
  CHECK(p.sample().temp_c < p.limits().temp_limit_c);
  CHECK_EQ(p.detectedFaults() & F_OVERTEMP, 0u);
}

void test_other_injections() {
  Physics p(5, Profile::HIGHWAY);
  for (int i = 0; i < 100; ++i) p.step(0.1);

  // SENSOR_STUCK freezes the temperature reading while the plant keeps heating.
  p.setInjectedFaults(F_SENSOR_STUCK | F_OVERTEMP);
  p.step(0.1);
  float frozen = p.sample().temp_c;
  for (int i = 0; i < 200; ++i) p.step(0.1);
  CHECK_EQ(p.sample().temp_c, frozen);
  CHECK(p.trueTemp() > frozen + 10);
  CHECK(p.faultFlags() & F_SENSOR_STUCK);
  p.setInjectedFaults(0);
  p.step(0.1);
  CHECK(p.sample().temp_c > frozen + 10);  // reading unfreezes

  // GPS_LOST zeroes position and sets the flag.
  p.setInjectedFaults(F_GPS_LOST);
  p.step(0.1);
  CHECK_EQ(p.sample().lat, 0.0); CHECK_EQ(p.sample().lon, 0.0);
  CHECK(p.faultFlags() & F_GPS_LOST);
  CHECK_EQ(p.state(), DeviceState::DEGRADED);
  p.setInjectedFaults(0);
  p.step(0.1);
  CHECK(p.sample().lat != 0.0);

  // UNDERVOLTAGE pulls the bus under the detection threshold.
  p.setInjectedFaults(F_UNDERVOLTAGE);
  for (int i = 0; i < 20; ++i) p.step(0.1);
  CHECK(p.sample().voltage_v < 80.0f);
  CHECK(p.detectedFaults() & F_UNDERVOLTAGE);
  CHECK_EQ(p.state(), DeviceState::FAULT);

  // OVERCURRENT adds current beyond the limit.
  p.setInjectedFaults(F_OVERCURRENT);
  for (int i = 0; i < 20; ++i) p.step(0.1);
  CHECK(p.sample().current_a > 150.0f);
  CHECK(p.detectedFaults() & F_OVERCURRENT);

  // VIBRATION_HIGH bumps vibration above threshold.
  p.setInjectedFaults(F_VIBRATION_HIGH);
  p.step(0.1);
  CHECK(p.sample().vibration_g > 2.0f);
  CHECK(p.detectedFaults() & F_VIBRATION_HIGH);

  // ENCODER_FAULT: noisy rpm with dropouts.
  p.setInjectedFaults(F_ENCODER_FAULT);
  int zeros = 0;
  for (int i = 0; i < 300; ++i) {
    p.step(0.1);
    if (p.sample().rpm == 0.0f) ++zeros;
  }
  CHECK(zeros > 5 && zeros < 100);
  CHECK_EQ(p.state(), DeviceState::FAULT);

  // --no-gps reports 0/0 without raising GPS_LOST.
  Physics nogps(3, Profile::CITY, false);
  nogps.step(0.1);
  CHECK_EQ(nogps.sample().lat, 0.0);
  CHECK_EQ(nogps.faultFlags() & F_GPS_LOST, 0u);
}

struct TestCase {
  const char* name;
  std::function<void()> fn;
};
const TestCase kTests[] = {
    {"crc32", test_crc32},
    {"header_layout", test_header_layout},
    {"roundtrip_all_messages", test_roundtrip_all_messages},
    {"fault_names", test_fault_names},
    {"fault_spec_parsing", test_fault_spec_parsing},
    {"config_parsing", test_config_parsing},
    {"physics_determinism", test_physics_determinism},
    {"physics_behaviour", test_physics_behaviour},
    {"overtemp_injection", test_overtemp_injection},
    {"other_injections", test_other_injections},
};
}  // namespace

int main(int argc, char** argv) {
  int run = 0;
  for (const auto& t : kTests) {
    if (argc > 1 && std::strcmp(argv[1], t.name) != 0) continue;
    int before = g_failures;
    std::printf("[ RUN  ] %s\n", t.name);
    t.fn();
    std::printf("[ %s ] %s\n", g_failures == before ? " OK " : "FAIL", t.name);
    ++run;
  }
  if (run == 0) {
    std::fprintf(stderr, "no such test: %s\n", argv[1]);
    return 1;
  }
  std::printf("%d test(s), %d failure(s)\n", run, g_failures);
  return g_failures == 0 ? 0 : 1;
}
