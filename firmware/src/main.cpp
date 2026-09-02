// ecu_agent entry point: CLI parsing per docs/interfaces.md, signal handling.
#include <signal.h>

#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "ecu/agent.hpp"
#include "ecu/config.hpp"
#include "ecu/log.hpp"

namespace {

void usage(FILE* out) {
  std::fprintf(out,
               "Usage: ecu_agent --id ECU-0001 [options]\n"
               "\n"
               "  --id ID              device id, 1..12 ASCII chars (required)\n"
               "  --host HOST          backend host              (default 127.0.0.1)\n"
               "  --udp-port N         telemetry UDP port         (default 8781)\n"
               "  --tcp-port N         control TCP port           (default 8782)\n"
               "  --hz N               telemetry rate 1..1000     (default 10)\n"
               "  --fw MAJ.MIN.PATCH   firmware version           (default 1.0.0)\n"
               "  --hw MODEL           hardware model, <=15 chars (default SIM-MOTOR-A)\n"
               "  --profile P          city|highway|idle|stress   (default city)\n"
               "  --drop-rate F        simulated UDP loss 0..1    (default 0.0)\n"
               "  --seed N             model PRNG seed            (default 42)\n"
               "  --duration S         run S seconds then exit 0  (default 0 = forever)\n"
               "  --config PATH        key=value config file (hz, rpm_limit, temp_limit_c,\n"
               "                       current_limit_a, log_level); CONFIG_SET is persisted\n"
               "                       to PATH.applied\n"
               "  --fault NAME@S:D     inject fault NAME at t=S for D seconds (repeatable;\n"
               "                       D omitted or 0 = until cleared)\n"
               "  --log-level L        debug|info|warn|error      (default info)\n"
               "  --no-gps             report lat/lon as 0\n"
               "  -h, --help           this help\n"
               "\n"
               "Exit codes: 0 ok, 2 bad arguments. Network failures never exit.\n");
}

[[noreturn]] void die(const std::string& msg) {
  std::fprintf(stderr, "ecu_agent: %s\n", msg.c_str());
  std::fprintf(stderr, "Try 'ecu_agent --help'.\n");
  std::exit(2);
}

bool parseInt(const std::string& s, long& out) {
  char* end = nullptr;
  out = std::strtol(s.c_str(), &end, 10);
  return !s.empty() && end && *end == '\0';
}
bool parseDouble(const std::string& s, double& out) {
  char* end = nullptr;
  out = std::strtod(s.c_str(), &end);
  return !s.empty() && end && *end == '\0';
}

void onSignal(int) { ecu::Agent::requestStop(); }

}  // namespace

int main(int argc, char** argv) {
  ecu::AgentOptions opts;
  ecu::Config cfg;
  bool cli_hz = false, cli_log_level = false;
  int hz = 10;
  ecu::LogLevel log_level = ecu::LogLevel::INFO;

  // Accept both "--key value" and "--key=value".
  std::vector<std::string> args(argv + 1, argv + argc);
  for (size_t i = 0; i < args.size(); ++i) {
    std::string key = args[i], val;
    bool has_val = false;
    size_t eq = key.find('=');
    if (key.rfind("--", 0) == 0 && eq != std::string::npos) {
      val = key.substr(eq + 1);
      key = key.substr(0, eq);
      has_val = true;
    }
    auto need = [&]() -> const std::string& {
      if (has_val) return val;
      if (i + 1 >= args.size()) die("missing value for " + key);
      val = args[++i];
      has_val = true;
      return val;
    };
    long l = 0;
    double d = 0;
    if (key == "-h" || key == "--help") {
      usage(stdout);
      return 0;
    } else if (key == "--id") {
      opts.device_id = need();
    } else if (key == "--host") {
      opts.host = need();
    } else if (key == "--udp-port") {
      if (!parseInt(need(), l) || l < 1 || l > 65535) die("--udp-port must be 1..65535");
      opts.udp_port = static_cast<int>(l);
    } else if (key == "--tcp-port") {
      if (!parseInt(need(), l) || l < 1 || l > 65535) die("--tcp-port must be 1..65535");
      opts.tcp_port = static_cast<int>(l);
    } else if (key == "--hz") {
      if (!parseInt(need(), l) || l < 1 || l > 1000) die("--hz must be 1..1000");
      hz = static_cast<int>(l);
      cli_hz = true;
    } else if (key == "--fw") {
      uint32_t v;
      if (!ecu::parseFwVersion(need(), v)) die("--fw must be MAJOR.MINOR.PATCH");
      opts.fw = val;
    } else if (key == "--hw") {
      if (need().empty() || val.size() > 15) die("--hw must be 1..15 chars");
      opts.hw = val;
    } else if (key == "--profile") {
      if (!ecu::parseProfile(need(), opts.profile)) die("--profile must be city|highway|idle|stress");
    } else if (key == "--drop-rate") {
      if (!parseDouble(need(), d) || d < 0.0 || d > 1.0) die("--drop-rate must be 0..1");
      opts.drop_rate = d;
    } else if (key == "--seed") {
      if (!parseInt(need(), l) || l < 0 || l > 0xFFFFFFFFL) die("--seed must be a non-negative integer");
      opts.seed = static_cast<uint32_t>(l);
    } else if (key == "--duration") {
      if (!parseDouble(need(), d) || d < 0) die("--duration must be >= 0");
      opts.duration_s = d;
    } else if (key == "--config") {
      opts.config_path = need();
    } else if (key == "--fault") {
      ecu::FaultSpec fs;
      std::string err;
      if (!ecu::parseFaultSpec(need(), fs, err)) die("--fault: " + err);
      opts.faults.push_back(fs);
    } else if (key == "--log-level") {
      if (!ecu::parseLogLevel(need(), log_level)) die("--log-level must be debug|info|warn|error");
      cli_log_level = true;
    } else if (key == "--no-gps") {
      if (has_val) die("--no-gps takes no value");
      opts.gps = false;
    } else {
      die("unknown argument '" + key + "'");
    }
  }

  if (opts.device_id.empty()) die("--id is required");
  if (opts.device_id.size() > ecu::kDeviceIdLen) die("--id must be at most 12 characters");
  for (char c : opts.device_id)
    if (!std::isprint(static_cast<unsigned char>(c)) || static_cast<unsigned char>(c) > 0x7E)
      die("--id must be printable ASCII");

  // Precedence: built-in defaults < --config file < explicit CLI flags.
  if (!opts.config_path.empty()) {
    std::string err;
    if (!ecu::loadConfigFile(opts.config_path, cfg, err)) die("--config: " + err);
  }
  if (cli_hz) cfg.hz = hz;
  if (cli_log_level) cfg.log_level = log_level;

  signal(SIGPIPE, SIG_IGN);
  struct sigaction sa{};
  sa.sa_handler = onSignal;
  sigemptyset(&sa.sa_mask);
  sa.sa_flags = 0;  // no SA_RESTART: let select() return EINTR so the loop notices promptly
  sigaction(SIGINT, &sa, nullptr);
  sigaction(SIGTERM, &sa, nullptr);

  ecu::Agent agent(opts, cfg);
  return agent.run();
}
