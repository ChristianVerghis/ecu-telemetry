// Minimal stderr logger: "[ts] [level] msg". Levels mirror the LOG frame
// (0 debug, 1 info, 2 warn, 3 error). An optional sink receives every record
// at or above the configured level so the agent can ship it as a LOG frame.
#pragma once

#include <chrono>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <ctime>
#include <functional>
#include <string>

namespace ecu {

enum class LogLevel : uint8_t { DEBUG = 0, INFO = 1, WARN = 2, ERROR = 3 };

inline const char* logLevelName(LogLevel l) {
  switch (l) {
    case LogLevel::DEBUG: return "debug";
    case LogLevel::INFO: return "info";
    case LogLevel::WARN: return "warn";
    case LogLevel::ERROR: return "error";
  }
  return "?";
}

inline bool parseLogLevel(const std::string& s, LogLevel& out) {
  if (s == "debug" || s == "0") out = LogLevel::DEBUG;
  else if (s == "info" || s == "1") out = LogLevel::INFO;
  else if (s == "warn" || s == "warning" || s == "2") out = LogLevel::WARN;
  else if (s == "error" || s == "3") out = LogLevel::ERROR;
  else return false;
  return true;
}

// ISO-8601 UTC timestamp with milliseconds.
inline std::string isoTimestamp() {
  using namespace std::chrono;
  auto now = system_clock::now();
  std::time_t t = system_clock::to_time_t(now);
  auto ms = duration_cast<milliseconds>(now.time_since_epoch()).count() % 1000;
  std::tm tm{};
  gmtime_r(&t, &tm);
  char buf[40];
  std::strftime(buf, sizeof buf, "%Y-%m-%dT%H:%M:%S", &tm);
  char out[48];
  std::snprintf(out, sizeof out, "%s.%03dZ", buf, static_cast<int>(ms));
  return out;
}

class Logger {
 public:
  using Sink = std::function<void(LogLevel, const std::string&)>;

  static Logger& instance() {
    static Logger l;
    return l;
  }

  void setLevel(LogLevel l) { level_ = l; }
  LogLevel level() const { return level_; }
  void setSink(Sink s) { sink_ = std::move(s); }

  void log(LogLevel lvl, const char* fmt, ...) __attribute__((format(printf, 3, 4))) {
    if (lvl < level_) return;
    char msg[1024];
    va_list ap;
    va_start(ap, fmt);
    std::vsnprintf(msg, sizeof msg, fmt, ap);
    va_end(ap);
    std::fprintf(stderr, "[%s] [%s] %s\n", isoTimestamp().c_str(), logLevelName(lvl), msg);
    std::fflush(stderr);
    if (sink_) sink_(lvl, msg);
  }

 private:
  LogLevel level_ = LogLevel::INFO;
  Sink sink_;
};

#define LOGD(...) ::ecu::Logger::instance().log(::ecu::LogLevel::DEBUG, __VA_ARGS__)
#define LOGI(...) ::ecu::Logger::instance().log(::ecu::LogLevel::INFO, __VA_ARGS__)
#define LOGW(...) ::ecu::Logger::instance().log(::ecu::LogLevel::WARN, __VA_ARGS__)
#define LOGE(...) ::ecu::Logger::instance().log(::ecu::LogLevel::ERROR, __VA_ARGS__)

}  // namespace ecu
