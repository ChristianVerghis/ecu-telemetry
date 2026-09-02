#include "ecu/config.hpp"

#include <cstdlib>
#include <fstream>
#include <sstream>

namespace ecu {

namespace {
std::string trim(const std::string& s) {
  size_t a = s.find_first_not_of(" \t\r\n");
  if (a == std::string::npos) return "";
  size_t b = s.find_last_not_of(" \t\r\n");
  return s.substr(a, b - a + 1);
}

bool toFloat(const std::string& s, float& out) {
  char* end = nullptr;
  out = std::strtof(s.c_str(), &end);
  return end && *end == '\0' && !s.empty();
}
bool toLong(const std::string& s, long& out) {
  char* end = nullptr;
  out = std::strtol(s.c_str(), &end, 10);
  return end && *end == '\0' && !s.empty();
}
}  // namespace

bool parseConfigText(const std::string& text, Config& cfg, std::string& err) {
  std::istringstream in(text);
  std::string line;
  int lineno = 0;
  while (std::getline(in, line)) {
    ++lineno;
    size_t hash = line.find('#');
    if (hash != std::string::npos) line = line.substr(0, hash);
    line = trim(line);
    if (line.empty()) continue;
    size_t eq = line.find('=');
    if (eq == std::string::npos) {
      err = "line " + std::to_string(lineno) + ": expected key=value";
      return false;
    }
    std::string key = trim(line.substr(0, eq));
    std::string val = trim(line.substr(eq + 1));
    long l = 0;
    float f = 0;
    if (key == "hz") {
      if (!toLong(val, l) || l < 1 || l > 1000) { err = "hz must be 1..1000"; return false; }
      cfg.hz = static_cast<int>(l);
    } else if (key == "config_version") {
      if (!toLong(val, l) || l < 0) { err = "config_version must be >= 0"; return false; }
      cfg.config_version = static_cast<uint32_t>(l);
    } else if (key == "rpm_limit") {
      if (!toFloat(val, f) || f <= 0) { err = "rpm_limit must be > 0"; return false; }
      cfg.limits.rpm_limit = f;
    } else if (key == "temp_limit_c") {
      if (!toFloat(val, f)) { err = "temp_limit_c must be a number"; return false; }
      cfg.limits.temp_limit_c = f;
    } else if (key == "current_limit_a") {
      if (!toFloat(val, f) || f <= 0) { err = "current_limit_a must be > 0"; return false; }
      cfg.limits.current_limit_a = f;
    } else if (key == "log_level") {
      if (!parseLogLevel(val, cfg.log_level)) { err = "log_level must be debug|info|warn|error"; return false; }
    } else {
      err = "line " + std::to_string(lineno) + ": unknown key '" + key + "'";
      return false;
    }
  }
  return true;
}

bool loadConfigFile(const std::string& path, Config& cfg, std::string& err) {
  std::ifstream f(path);
  if (!f) {
    err = "cannot open " + path;
    return false;
  }
  std::stringstream ss;
  ss << f.rdbuf();
  return parseConfigText(ss.str(), cfg, err);
}

std::string configToText(const Config& cfg) {
  std::ostringstream o;
  o << "# ECU agent configuration (written by ecu_agent)\n";
  o << "config_version=" << cfg.config_version << "\n";
  o << "hz=" << cfg.hz << "\n";
  o << "log_level=" << logLevelName(cfg.log_level) << "\n";
  o << "rpm_limit=" << cfg.limits.rpm_limit << "\n";
  o << "temp_limit_c=" << cfg.limits.temp_limit_c << "\n";
  o << "current_limit_a=" << cfg.limits.current_limit_a << "\n";
  return o.str();
}

bool saveConfigFile(const std::string& path, const Config& cfg, std::string& err) {
  std::ofstream f(path, std::ios::trunc);
  if (!f) {
    err = "cannot write " + path;
    return false;
  }
  f << configToText(cfg);
  return static_cast<bool>(f);
}

}  // namespace ecu
