#include "ecu/agent.hpp"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <signal.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/types.h>
#include <unistd.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdlib>
#include <cstring>

#include "ecu/log.hpp"

#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0  // macOS: we ignore SIGPIPE in main() instead
#endif

namespace ecu {

namespace {
volatile std::sig_atomic_t g_stop = 0;

constexpr double kHeartbeatPeriodS = 5.0;
constexpr double kBackoffMinS = 1.0;
constexpr double kBackoffMaxS = 30.0;
constexpr double kConnectTimeoutS = 5.0;
constexpr double kRebootDurationS = 1.0;
constexpr double kOtaBytesPerSecond = 500000.0;
constexpr size_t kRxLimit = 64 * 1024;
constexpr size_t kTxLimit = 256 * 1024;

uint64_t unixMillis() {
  using namespace std::chrono;
  return static_cast<uint64_t>(duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count());
}

bool resolve(const std::string& host, int port, int socktype, sockaddr_storage& out, socklen_t& len) {
  addrinfo hints{};
  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = socktype;
  addrinfo* res = nullptr;
  std::string port_s = std::to_string(port);
  if (getaddrinfo(host.c_str(), port_s.c_str(), &hints, &res) != 0 || !res) return false;
  // Prefer IPv4 for predictability with the Python backend; fall back to the first result.
  addrinfo* pick = res;
  for (addrinfo* a = res; a; a = a->ai_next)
    if (a->ai_family == AF_INET) { pick = a; break; }
  std::memcpy(&out, pick->ai_addr, pick->ai_addrlen);
  len = pick->ai_addrlen;
  freeaddrinfo(res);
  return true;
}

void setNonBlocking(int fd) {
  int fl = fcntl(fd, F_GETFL, 0);
  if (fl >= 0) fcntl(fd, F_SETFL, fl | O_NONBLOCK);
}
}  // namespace

// ---------------------------------------------------------------------------
// --fault parsing: FLAGNAME@START_S:DUR_S  (":DUR_S" optional -> until cleared)

bool parseFaultSpec(const std::string& spec, FaultSpec& out, std::string& err) {
  size_t at = spec.find('@');
  if (at == std::string::npos || at == 0) {
    err = "expected FLAGNAME@START_S[:DUR_S], got '" + spec + "'";
    return false;
  }
  std::string name = spec.substr(0, at);
  std::string times = spec.substr(at + 1);
  if (!parseFaultName(name, out.flag)) {
    err = "unknown fault name '" + name + "'";
    return false;
  }
  std::string start_s = times, dur_s = "0";
  size_t colon = times.find(':');
  if (colon != std::string::npos) {
    start_s = times.substr(0, colon);
    dur_s = times.substr(colon + 1);
  }
  char* end = nullptr;
  out.start_s = std::strtod(start_s.c_str(), &end);
  if (start_s.empty() || !end || *end != '\0' || out.start_s < 0) {
    err = "bad start time in '" + spec + "'";
    return false;
  }
  out.dur_s = std::strtod(dur_s.c_str(), &end);
  if (dur_s.empty() || !end || *end != '\0' || out.dur_s < 0) {
    err = "bad duration in '" + spec + "'";
    return false;
  }
  return true;
}

// ---------------------------------------------------------------------------

Agent::Agent(const AgentOptions& opts, const Config& cfg)
    : opts_(opts),
      cfg_(cfg),
      physics_(opts.seed, opts.profile, opts.gps),
      drop_rng_(static_cast<uint64_t>(opts.seed) * 7919u + 17u),
      cli_fault_fired_(opts.faults.size(), false) {
  if (!parseFwVersion(opts_.fw, fw_version_)) fw_version_ = 0x010000;
  fw_string_ = fwVersionToString(fw_version_);
  physics_.setLimits(cfg_.limits);
  physics_.setCommDegraded(true);  // until the control channel is up
  Logger::instance().setLevel(cfg_.log_level);
  applyHz(cfg_.hz);

  // Ship every emitted log record as a LOG frame while connected. The guard
  // prevents recursion if sending itself logs an error.
  Logger::instance().setSink([this](LogLevel lvl, const std::string& msg) {
    if (in_log_sink_ || tcp_state_ != TcpState::CONNECTED) return;
    in_log_sink_ = true;
    LogMsg m;
    m.level = static_cast<uint8_t>(lvl);
    m.message = msg;
    sendFrame(MsgType::LOG, m.encode());
    in_log_sink_ = false;
  });
}

Agent::~Agent() {
  Logger::instance().setSink(nullptr);
  if (udp_fd_ >= 0) close(udp_fd_);
  if (tcp_fd_ >= 0) close(tcp_fd_);
}

void Agent::requestStop() { g_stop = 1; }

double Agent::now() {
  using namespace std::chrono;
  return duration<double>(steady_clock::now().time_since_epoch()).count();
}

void Agent::applyHz(int hz) {
  if (hz < 1) hz = 1;
  if (hz > 1000) hz = 1000;
  cfg_.hz = hz;
  period_s_ = 1.0 / hz;
  next_tick_at_ = now() + period_s_;
}

FrameHeader Agent::makeHeader(MsgType type, uint32_t seq) const {
  FrameHeader h;
  h.msg_type = type;
  h.device_id = opts_.device_id;
  h.seq = seq;
  h.ts_ms = unixMillis();
  h.fw_version = fw_version_;
  return h;
}

// ---------------------------------------------------------------------------
// Main loop

int Agent::run() {
  start_at_ = boot_at_ = now();
  next_tick_at_ = start_at_ + period_s_;
  next_connect_at_ = start_at_;
  openUdp();

  LOGI("ecu_agent %s starting: fw=%s hw=%s profile=%s hz=%d seed=%u udp=%s:%d tcp=%s:%d drop=%.2f",
       opts_.device_id.c_str(), fw_string_.c_str(), opts_.hw.c_str(), profileName(opts_.profile), cfg_.hz,
       opts_.seed, opts_.host.c_str(), opts_.udp_port, opts_.host.c_str(), opts_.tcp_port, opts_.drop_rate);

  while (!g_stop) {
    double t = now();
    if (opts_.duration_s > 0 && t - start_at_ >= opts_.duration_s) {
      LOGI("duration %.1fs elapsed, exiting", opts_.duration_s);
      break;
    }

    // Simulated reboot: telemetry and control are silent for a moment.
    if (rebooting_ && t >= reboot_until_) {
      rebooting_ = false;
      boot_at_ = t;
      if (!pending_fw_.empty()) {
        parseFwVersion(pending_fw_, fw_version_);
        fw_string_ = fwVersionToString(fw_version_);
        pending_fw_.clear();
      }
      next_tick_at_ = t + period_s_;
      next_connect_at_ = t;
      LOGI("boot complete, fw=%s", fw_string_.c_str());
    }

    if (!rebooting_) {
      if (t >= next_tick_at_) {
        tick();
        next_tick_at_ += period_s_;
        if (next_tick_at_ < t - 1.0) next_tick_at_ = t + period_s_;  // fell far behind: resync
      }
      if (tcp_state_ == TcpState::CONNECTED && t >= next_heartbeat_at_) {
        sendHeartbeat();
        next_heartbeat_at_ += kHeartbeatPeriodS;
      }
      serviceOta();
    }

    // Sleep until the next deadline while servicing the TCP socket.
    double deadline = rebooting_ ? reboot_until_ : next_tick_at_;
    if (!rebooting_) {
      if (tcp_state_ == TcpState::CONNECTED) deadline = std::min(deadline, next_heartbeat_at_);
      if (tcp_state_ == TcpState::DISCONNECTED) deadline = std::min(deadline, next_connect_at_);
      if (ota_.active) deadline = std::min(deadline, std::min(ota_.done_at, ota_.next_progress_at));
    }
    double timeout = std::max(0.0, std::min(deadline - now(), 0.25));
    tcpService(timeout);
  }

  if (g_stop) LOGI("stop requested, shutting down");
  if (tcp_fd_ >= 0) {
    close(tcp_fd_);
    tcp_fd_ = -1;
    tcp_state_ = TcpState::DISCONNECTED;
  }
  return 0;
}

void Agent::tick() {
  double t = now();
  updateInjections(t);
  physics_.step(period_s_);
  checkLocalAlerts();
  sendTelemetry();
}

// Apply CLI-scheduled faults and expire timed injections, then push the
// combined mask into the plant.
void Agent::updateInjections(double t) {
  double run_t = t - start_at_;
  for (size_t i = 0; i < opts_.faults.size(); ++i) {
    if (cli_fault_fired_[i] || run_t < opts_.faults[i].start_s) continue;
    cli_fault_fired_[i] = true;
    const FaultSpec& fs = opts_.faults[i];
    injections_.push_back({fs.flag, fs.dur_s > 0 ? t + fs.dur_s : 0.0});
    LOGW("fault injected (cli): %s for %.1fs", faultName(fs.flag), fs.dur_s);
  }
  uint32_t mask = 0;
  bool expired = false;
  for (auto it = injections_.begin(); it != injections_.end();) {
    if (it->expires_at > 0 && t >= it->expires_at) {
      LOGI("fault expired: %s", faultFlagsToString(it->flags).c_str());
      it = injections_.erase(it);
      expired = true;
    } else {
      mask |= it->flags;
      ++it;
    }
  }
  if (mask != physics_.injectedFaults()) {
    physics_.setInjectedFaults(mask);
    if (expired && tcp_state_ == TcpState::CONNECTED) sendFaultAck();
  }
}

// Device-side threshold alerts: one WARN per rising edge, one INFO on clear.
void Agent::checkLocalAlerts() {
  uint32_t det = physics_.detectedFaults();
  uint32_t rose = det & ~prev_detected_;
  uint32_t fell = prev_detected_ & ~det;
  prev_detected_ = det;
  if (!rose && !fell) return;
  const Telemetry& s = physics_.sample();
  const Limits& L = physics_.limits();
  for (uint32_t bit = 1; bit <= F_ENCODER_FAULT; bit <<= 1) {
    if (rose & bit) {
      switch (bit) {
        case F_OVERTEMP: LOGW("ALERT OVERTEMP: temp %.1fC > limit %.1fC", s.temp_c, L.temp_limit_c); break;
        case F_OVERCURRENT: LOGW("ALERT OVERCURRENT: current %.1fA > limit %.1fA", s.current_a, L.current_limit_a); break;
        case F_UNDERVOLTAGE: LOGW("ALERT UNDERVOLTAGE: bus %.1fV", s.voltage_v); break;
        case F_VIBRATION_HIGH: LOGW("ALERT VIBRATION_HIGH: %.2fg", s.vibration_g); break;
        case F_ENCODER_FAULT: LOGW("ALERT ENCODER_FAULT: rpm %.0f > limit %.0f", s.rpm, L.rpm_limit); break;
        default: LOGW("ALERT %s", faultName(bit)); break;
      }
    }
    if (fell & bit) LOGI("ALERT cleared: %s", faultName(bit));
  }
}

// ---------------------------------------------------------------------------
// UDP telemetry

bool Agent::openUdp() {
  socklen_t len = 0;
  if (!resolve(opts_.host, opts_.udp_port, SOCK_DGRAM, udp_addr_, len)) {
    LOGE("cannot resolve %s for UDP; telemetry disabled until resolution succeeds", opts_.host.c_str());
    return false;
  }
  udp_addr_len_ = len;
  udp_fd_ = socket(udp_addr_.ss_family, SOCK_DGRAM, 0);
  if (udp_fd_ < 0) {
    LOGE("udp socket: %s", std::strerror(errno));
    return false;
  }
  setNonBlocking(udp_fd_);
  return true;
}

void Agent::sendTelemetry() {
  if (udp_fd_ < 0 && !openUdp()) return;
  const Telemetry& s = physics_.sample();
  uint32_t seq = udp_seq_++;  // seq advances even for dropped frames (that is the point)
  ++frames_sent_;
  if (opts_.drop_rate > 0 && drop_rng_.uniform() < opts_.drop_rate) {
    LOGD("telemetry seq=%u dropped (simulated loss)", seq);
    return;
  }
  std::vector<uint8_t> frame = encodeFrame(makeHeader(MsgType::TELEMETRY, seq), s.encode());
  ssize_t n = sendto(udp_fd_, frame.data(), frame.size(), 0, reinterpret_cast<const sockaddr*>(&udp_addr_), udp_addr_len_);
  if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != ECONNREFUSED)
    LOGD("udp sendto: %s", std::strerror(errno));
}

// ---------------------------------------------------------------------------
// TCP control channel

void Agent::tcpService(double timeout_s) {
  double t = now();
  if (rebooting_) {
    // Nothing to do on the network during a reboot; just sleep.
    timeval tv{static_cast<time_t>(timeout_s), static_cast<suseconds_t>((timeout_s - std::floor(timeout_s)) * 1e6)};
    select(0, nullptr, nullptr, nullptr, &tv);
    return;
  }
  if (tcp_state_ == TcpState::DISCONNECTED && t >= next_connect_at_) tcpStartConnect();

  fd_set rfds, wfds;
  FD_ZERO(&rfds);
  FD_ZERO(&wfds);
  int maxfd = -1;
  if (tcp_fd_ >= 0) {
    if (tcp_state_ == TcpState::CONNECTING || !tx_.empty()) FD_SET(tcp_fd_, &wfds);
    if (tcp_state_ == TcpState::CONNECTED) FD_SET(tcp_fd_, &rfds);
    maxfd = tcp_fd_;
  }
  timeval tv{static_cast<time_t>(timeout_s), static_cast<suseconds_t>((timeout_s - std::floor(timeout_s)) * 1e6)};
  int r = select(maxfd + 1, &rfds, &wfds, nullptr, &tv);
  if (r < 0) {
    if (errno == EINTR) return;
    LOGE("select: %s", std::strerror(errno));
    return;
  }
  if (tcp_fd_ < 0) return;
  if (tcp_state_ == TcpState::CONNECTING) {
    if (FD_ISSET(tcp_fd_, &wfds)) tcpFinishConnect();
    else if (now() - connect_started_at_ > kConnectTimeoutS) tcpFail("connect timeout");
    return;
  }
  if (r > 0 && FD_ISSET(tcp_fd_, &rfds)) tcpRead();
  if (tcp_fd_ >= 0 && r > 0 && FD_ISSET(tcp_fd_, &wfds)) tcpFlush();
}

void Agent::tcpStartConnect() {
  sockaddr_storage addr{};
  socklen_t len = 0;
  if (!resolve(opts_.host, opts_.tcp_port, SOCK_STREAM, addr, len)) {
    tcpFail("cannot resolve host");
    return;
  }
  tcp_fd_ = socket(addr.ss_family, SOCK_STREAM, 0);
  if (tcp_fd_ < 0) {
    tcpFail("socket() failed");
    return;
  }
  setNonBlocking(tcp_fd_);
  int one = 1;
  setsockopt(tcp_fd_, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
#ifdef SO_NOSIGPIPE
  setsockopt(tcp_fd_, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one);
#endif
  if (ever_connected_) ++reconnects_;
  connect_started_at_ = now();
  int rc = connect(tcp_fd_, reinterpret_cast<sockaddr*>(&addr), len);
  if (rc == 0) {
    tcp_state_ = TcpState::CONNECTING;  // finalise on the common path
    tcpOnConnected();
  } else if (errno == EINPROGRESS) {
    tcp_state_ = TcpState::CONNECTING;
  } else {
    tcpFail(std::strerror(errno));
  }
}

void Agent::tcpFinishConnect() {
  int err = 0;
  socklen_t elen = sizeof err;
  if (getsockopt(tcp_fd_, SOL_SOCKET, SO_ERROR, &err, &elen) < 0 || err != 0) {
    tcpFail(std::strerror(err ? err : errno));
    return;
  }
  tcpOnConnected();
}

void Agent::tcpOnConnected() {
  tcp_state_ = TcpState::CONNECTED;
  ever_connected_ = true;
  backoff_s_ = kBackoffMinS;
  rx_.clear();
  tx_.clear();
  physics_.setCommDegraded(false);
  sendHello();  // HELLO must be the first frame on the channel (before any shipped LOG)
  next_heartbeat_at_ = now() + kHeartbeatPeriodS;
  LOGI("TCP control channel connected to %s:%d (reconnects=%u)", opts_.host.c_str(), opts_.tcp_port, reconnects_);
}

// Drop the connection and schedule a jittered exponential-backoff retry.
void Agent::tcpFail(const char* why) {
  if (tcp_fd_ >= 0) {
    close(tcp_fd_);
    tcp_fd_ = -1;
  }
  bool was_connected = tcp_state_ == TcpState::CONNECTED;
  tcp_state_ = TcpState::DISCONNECTED;
  rx_.clear();
  tx_.clear();
  ota_.active = false;  // an in-flight OTA cannot complete without the channel
  physics_.setCommDegraded(true);
  double jitter = drop_rng_.uniform(0.75, 1.25);
  double delay = backoff_s_ * jitter;
  next_connect_at_ = now() + delay;
  if (was_connected) LOGW("TCP control channel lost (%s); reconnect in %.1fs", why, delay);
  else LOGW("TCP connect to %s:%d failed (%s); retry in %.1fs", opts_.host.c_str(), opts_.tcp_port, why, delay);
  backoff_s_ = std::min(kBackoffMaxS, backoff_s_ * 2.0);
}

void Agent::tcpRead() {
  uint8_t buf[4096];
  for (;;) {
    ssize_t n = recv(tcp_fd_, buf, sizeof buf, 0);
    if (n > 0) {
      rx_.insert(rx_.end(), buf, buf + n);
      if (rx_.size() > kRxLimit) {
        tcpFail("rx buffer overflow");
        return;
      }
      continue;
    }
    if (n == 0) {
      tcpFail("peer closed");
      return;
    }
    if (errno == EAGAIN || errno == EWOULDBLOCK) break;
    if (errno == EINTR) continue;
    tcpFail(std::strerror(errno));
    return;
  }
  // Parse as many complete frames as are buffered.
  size_t off = 0;
  while (off < rx_.size()) {
    Frame f;
    size_t used = 0;
    DecodeError e = decodeFrame(rx_.data() + off, rx_.size() - off, f, used);
    if (e == DecodeError::TOO_SHORT) break;
    if (e != DecodeError::OK) {
      // A framing error leaves the stream unsynchronised; reconnect.
      rx_.clear();
      tcpFail(decodeErrorName(e));
      return;
    }
    off += used;
    handleFrame(f);
    if (tcp_state_ != TcpState::CONNECTED) return;  // handler closed the channel
  }
  rx_.erase(rx_.begin(), rx_.begin() + static_cast<std::ptrdiff_t>(off));
}

bool Agent::tcpFlush() {
  while (!tx_.empty()) {
    ssize_t n = send(tcp_fd_, tx_.data(), tx_.size(), MSG_NOSIGNAL);
    if (n > 0) {
      tx_.erase(tx_.begin(), tx_.begin() + n);
      continue;
    }
    if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return false;
    if (n < 0 && errno == EINTR) continue;
    tcpFail(n < 0 ? std::strerror(errno) : "send returned 0");
    return false;
  }
  return true;
}

void Agent::sendFrame(MsgType type, const std::vector<uint8_t>& payload) {
  if (tcp_state_ != TcpState::CONNECTED) return;
  std::vector<uint8_t> frame = encodeFrame(makeHeader(type, tcp_seq_++), payload);
  if (tx_.size() + frame.size() > kTxLimit) {
    tcpFail("tx buffer overflow");
    return;
  }
  tx_.insert(tx_.end(), frame.begin(), frame.end());
  tcpFlush();
}

void Agent::sendHello() {
  Hello h;
  h.fw_string = fw_string_;
  h.hw_model = opts_.hw;
  h.config_version = cfg_.config_version;
  h.telemetry_hz = static_cast<uint16_t>(cfg_.hz);
  sendFrame(MsgType::HELLO, h.encode());
}

void Agent::sendHeartbeat() {
  Heartbeat hb;
  hb.uptime_s = static_cast<uint32_t>(now() - boot_at_);
  hb.frames_sent = frames_sent_;
  hb.config_version = cfg_.config_version;
  hb.reconnects = reconnects_;
  sendFrame(MsgType::HEARTBEAT, hb.encode());
}

void Agent::sendFaultAck() {
  FaultAck a;
  a.active_flags = physics_.faultFlags();
  sendFrame(MsgType::FAULT_ACK, a.encode());
}

// ---------------------------------------------------------------------------
// Inbound control messages

void Agent::handleFrame(const Frame& f) {
  LOGD("rx %s seq=%u len=%u", msgTypeName(f.hdr.msg_type), f.hdr.seq, f.hdr.payload_len);
  switch (f.hdr.msg_type) {
    case MsgType::CONFIG_SET: handleConfigSet(f); break;
    case MsgType::FW_UPDATE: handleFwUpdate(f); break;
    case MsgType::FAULT_INJECT: handleFaultInject(f); break;
    case MsgType::CMD: handleCmd(f); break;
    default:
      LOGW("ignoring unexpected %s (0x%02X) on control channel", msgTypeName(f.hdr.msg_type),
           static_cast<unsigned>(f.hdr.msg_type));
      break;
  }
}

void Agent::handleConfigSet(const Frame& f) {
  ConfigSet cs;
  ConfigAck ack;
  if (!ConfigSet::decode(f.payload, cs)) {
    LOGW("CONFIG_SET malformed (len=%zu); rejected", f.payload.size());
    ack.status = 1;
    sendFrame(MsgType::CONFIG_ACK, ack.encode());
    return;
  }
  ack.config_version = cs.config_version;
  bool ok = cs.telemetry_hz >= 1 && cs.telemetry_hz <= 1000 && cs.log_level <= 3 &&
            std::isfinite(cs.rpm_limit) && cs.rpm_limit > 0 && std::isfinite(cs.temp_limit_c) &&
            std::isfinite(cs.current_limit_a) && cs.current_limit_a > 0;
  if (!ok) {
    LOGW("CONFIG_SET v%u rejected: hz=%u log_level=%u rpm=%.0f temp=%.1f cur=%.1f", cs.config_version,
         cs.telemetry_hz, cs.log_level, cs.rpm_limit, cs.temp_limit_c, cs.current_limit_a);
    ack.status = 1;
    sendFrame(MsgType::CONFIG_ACK, ack.encode());
    return;
  }
  cfg_.config_version = cs.config_version;
  cfg_.log_level = static_cast<LogLevel>(cs.log_level);
  cfg_.limits.rpm_limit = cs.rpm_limit;
  cfg_.limits.temp_limit_c = cs.temp_limit_c;
  cfg_.limits.current_limit_a = cs.current_limit_a;
  Logger::instance().setLevel(cfg_.log_level);
  physics_.setLimits(cfg_.limits);
  applyHz(cs.telemetry_hz);
  LOGI("CONFIG_SET v%u applied: hz=%d log_level=%s rpm_limit=%.0f temp_limit=%.1f current_limit=%.1f",
       cfg_.config_version, cfg_.hz, logLevelName(cfg_.log_level), cfg_.limits.rpm_limit,
       cfg_.limits.temp_limit_c, cfg_.limits.current_limit_a);
  if (!opts_.config_path.empty()) {
    std::string err;
    std::string path = opts_.config_path + ".applied";
    if (saveConfigFile(path, cfg_, err)) LOGI("config persisted to %s", path.c_str());
    else LOGE("config persist failed: %s", err.c_str());
  }
  ack.status = 0;
  sendFrame(MsgType::CONFIG_ACK, ack.encode());
}

void Agent::handleFwUpdate(const Frame& f) {
  FwUpdate fu;
  FwAck ack;
  ack.status = FwAckStatus::REJECTED;
  uint32_t target = 0;
  if (!FwUpdate::decode(f.payload, fu) || !parseFwVersion(fu.target_version, target)) {
    ack.version = fw_string_;
    LOGW("FW_UPDATE rejected: malformed (target='%s')", fu.target_version.c_str());
    sendFrame(MsgType::FW_ACK, ack.encode());
    return;
  }
  ack.version = fu.target_version;
  if (target < fw_version_) {
    LOGW("FW_UPDATE rejected: target %s is lower than running %s", fu.target_version.c_str(), fw_string_.c_str());
    sendFrame(MsgType::FW_ACK, ack.encode());
    return;
  }
  if (ota_.active) {
    LOGW("FW_UPDATE rejected: OTA to %s already in progress", ota_.target.c_str());
    sendFrame(MsgType::FW_ACK, ack.encode());
    return;
  }
  double dl_s = std::max(1.0, static_cast<double>(fu.image_size_bytes) / kOtaBytesPerSecond);
  ota_.active = true;
  ota_.target = fu.target_version;
  ota_.target_version = target;
  ota_.done_at = now() + dl_s;
  ota_.next_progress_at = now() + 1.0;
  LOGI("FW_UPDATE accepted: %s -> %s, %u bytes crc=0x%08X, simulated download %.1fs", fw_string_.c_str(),
       ota_.target.c_str(), fu.image_size_bytes, fu.image_crc, dl_s);
  ack.status = FwAckStatus::ACCEPTED;
  sendFrame(MsgType::FW_ACK, ack.encode());
  ack.status = FwAckStatus::DOWNLOADING;
  sendFrame(MsgType::FW_ACK, ack.encode());
}

// Progress the simulated OTA: periodic "downloading" acks, then applied + reboot.
void Agent::serviceOta() {
  if (!ota_.active || tcp_state_ != TcpState::CONNECTED) return;
  double t = now();
  FwAck ack;
  ack.version = ota_.target;
  if (t >= ota_.done_at) {
    ack.status = FwAckStatus::APPLIED;
    sendFrame(MsgType::FW_ACK, ack.encode());
    LOGI("firmware %s applied, rebooting", ota_.target.c_str());
    ota_.active = false;
    pending_fw_ = ota_.target;
    startReboot("firmware update", true);
  } else if (t >= ota_.next_progress_at) {
    ack.status = FwAckStatus::DOWNLOADING;
    sendFrame(MsgType::FW_ACK, ack.encode());
    ota_.next_progress_at = t + 1.0;
  }
}

void Agent::handleFaultInject(const Frame& f) {
  FaultInject fi;
  if (!FaultInject::decode(f.payload, fi)) {
    LOGW("FAULT_INJECT malformed (len=%zu)", f.payload.size());
    sendFaultAck();
    return;
  }
  double t = now();
  if (fi.clear_flags) {
    for (auto& inj : injections_) inj.flags &= ~fi.clear_flags;
    injections_.erase(std::remove_if(injections_.begin(), injections_.end(),
                                     [](const Injection& i) { return i.flags == 0; }),
                      injections_.end());
  }
  if (fi.set_flags) injections_.push_back({fi.set_flags & F_ALL, fi.duration_ms ? t + fi.duration_ms / 1000.0 : 0.0});
  uint32_t mask = 0;
  for (const auto& inj : injections_) mask |= inj.flags;
  physics_.setInjectedFaults(mask);
  LOGW("FAULT_INJECT set=%s clear=%s duration=%ums -> active=%s", faultFlagsToString(fi.set_flags).c_str(),
       faultFlagsToString(fi.clear_flags).c_str(), fi.duration_ms, faultFlagsToString(physics_.faultFlags()).c_str());
  sendFaultAck();
}

void Agent::handleCmd(const Frame& f) {
  Cmd c;
  if (!Cmd::decode(f.payload, c)) {
    LOGW("CMD malformed (len=%zu)", f.payload.size());
    return;
  }
  switch (c.cmd) {
    case CmdCode::REBOOT:
      startReboot("CMD reboot", false);
      break;
    case CmdCode::CLEAR_FAULTS:
      injections_.clear();
      physics_.setInjectedFaults(0);
      LOGI("CMD clear_faults: injected faults cleared (active=%s)", faultFlagsToString(physics_.faultFlags()).c_str());
      sendFaultAck();
      break;
    case CmdCode::REQUEST_HELLO:
      LOGI("CMD request_hello");
      sendHello();
      break;
    default:
      LOGW("CMD unknown code %u", static_cast<unsigned>(c.cmd));
      break;
  }
}

// Close the control channel and go quiet for kRebootDurationS. Uptime resets;
// UDP/TCP sequence numbers continue. Injected faults are volatile and cleared.
void Agent::startReboot(const std::string& reason, bool apply_fw) {
  LOGI("rebooting (%s)%s", reason.c_str(), apply_fw ? " with new firmware" : "");
  if (tcp_fd_ >= 0) {
    tcpFlush();
    close(tcp_fd_);
    tcp_fd_ = -1;
  }
  tcp_state_ = TcpState::DISCONNECTED;
  rx_.clear();
  tx_.clear();
  ota_.active = false;
  injections_.clear();
  physics_.setInjectedFaults(0);
  physics_.setCommDegraded(true);
  backoff_s_ = kBackoffMinS;
  rebooting_ = true;
  reboot_until_ = now() + kRebootDurationS;
}

}  // namespace ecu
