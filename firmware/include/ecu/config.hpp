// Device configuration: defaults, key=value file parsing, and persistence of
// backend-applied CONFIG_SET values to "<config>.applied".
#pragma once

#include <cstdint>
#include <string>

#include "ecu/log.hpp"

namespace ecu {

struct Limits {
  float rpm_limit = 6000.0f;
  float temp_limit_c = 110.0f;
  float current_limit_a = 150.0f;
};

struct Config {
  uint32_t config_version = 1;
  int hz = 10;
  LogLevel log_level = LogLevel::INFO;
  Limits limits;
};

// Parse "key=value" lines ('#' comments, blank lines ignored). Unknown keys are
// reported through `err` and cause a false return. Recognised keys:
// hz, rpm_limit, temp_limit_c, current_limit_a, log_level, config_version.
bool parseConfigText(const std::string& text, Config& cfg, std::string& err);
bool loadConfigFile(const std::string& path, Config& cfg, std::string& err);

// Render as key=value text (the inverse of parseConfigText).
std::string configToText(const Config& cfg);
bool saveConfigFile(const std::string& path, const Config& cfg, std::string& err);

}  // namespace ecu
