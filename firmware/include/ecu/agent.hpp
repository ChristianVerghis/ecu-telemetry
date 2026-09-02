// The telemetry agent: runs the plant model at --hz, streams TELEMETRY over UDP
// and maintains the TCP control channel (HELLO / HEARTBEAT / LOG / CONFIG_SET /
// FW_UPDATE / FAULT_INJECT / CMD). Single-threaded; select()-driven so the
// telemetry loop never blocks on the network.
#pragma once

#include <sys/socket.h>

#include <cstdint>
#include <string>
#include <vector>

#include "ecu/config.hpp"
#include "ecu/physics.hpp"
#include "ecu/protocol.hpp"

namespace ecu {

// One "--fault FLAGNAME@START_S:DUR_S" entry.
struct FaultSpec {
  uint32_t flag = 0;
  double start_s = 0;
  double dur_s = 0;  // 0 = until cleared
};
bool parseFaultSpec(const std::string& spec, FaultSpec& out, std::string& err);

struct AgentOptions {
  std::string device_id;
  std::string host = "127.0.0.1";
  int udp_port = 8781;
  int tcp_port = 8782;
  std::string fw = "1.0.0";
  std::string hw = "SIM-MOTOR-A";
  Profile profile = Profile::CITY;
  double drop_rate = 0.0;
  uint32_t seed = 42;
  double duration_s = 0;  // 0 = forever
  std::string config_path;
  std::vector<FaultSpec> faults;
  bool gps = true;
};

class Agent {
 public:
  Agent(const AgentOptions& opts, const Config& cfg);
  ~Agent();
  Agent(const Agent&) = delete;
  Agent& operator=(const Agent&) = delete;

  // Blocks until --duration elapses or requestStop() is called. Returns 0.
  int run();
  // Async-signal-safe: sets a flag polled by the main loop.
  static void requestStop();

 private:
  enum class TcpState { DISCONNECTED, CONNECTING, CONNECTED };
  struct Injection {
    uint32_t flags;
    double expires_at;  // monotonic seconds, 0 = never
  };
  struct Ota {
    bool active = false;
    std::string target;
    uint32_t target_version = 0;
    double done_at = 0;
    double next_progress_at = 0;
  };

  // scheduling
  static double now();
  void tick();
  void updateInjections(double t);
  void checkLocalAlerts();

  // UDP
  bool openUdp();
  void sendTelemetry();

  // TCP
  void tcpService(double timeout_s);
  void tcpStartConnect();
  void tcpFinishConnect();
  void tcpOnConnected();
  void tcpFail(const char* why);
  void tcpRead();
  bool tcpFlush();
  void sendFrame(MsgType type, const std::vector<uint8_t>& payload);
  void handleFrame(const Frame& f);
  void handleConfigSet(const Frame& f);
  void handleFwUpdate(const Frame& f);
  void handleFaultInject(const Frame& f);
  void handleCmd(const Frame& f);
  void serviceOta();
  void sendHello();
  void sendHeartbeat();
  void sendFaultAck();
  void startReboot(const std::string& reason, bool apply_fw);

  FrameHeader makeHeader(MsgType type, uint32_t seq) const;
  void applyHz(int hz);

  AgentOptions opts_;
  Config cfg_;
  Physics physics_;
  Rng drop_rng_;

  uint32_t fw_version_ = 0;
  std::string fw_string_;

  // UDP
  int udp_fd_ = -1;
  sockaddr_storage udp_addr_{};
  socklen_t udp_addr_len_ = 0;
  uint32_t udp_seq_ = 0;
  uint32_t frames_sent_ = 0;

  // TCP
  int tcp_fd_ = -1;
  TcpState tcp_state_ = TcpState::DISCONNECTED;
  double connect_started_at_ = 0;
  double next_connect_at_ = 0;
  double backoff_s_ = 1.0;
  bool ever_connected_ = false;
  uint32_t reconnects_ = 0;
  uint32_t tcp_seq_ = 0;
  std::vector<uint8_t> rx_;
  std::vector<uint8_t> tx_;
  double next_heartbeat_at_ = 0;
  bool in_log_sink_ = false;

  // timing
  double start_at_ = 0;
  double boot_at_ = 0;
  double period_s_ = 0.1;
  double next_tick_at_ = 0;
  double reboot_until_ = 0;
  bool rebooting_ = false;
  std::string pending_fw_;

  // faults
  std::vector<Injection> injections_;
  std::vector<bool> cli_fault_fired_;
  uint32_t prev_detected_ = 0;
  Ota ota_;
};

}  // namespace ecu
