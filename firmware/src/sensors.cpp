#include "ecu/sensors.hpp"

#include <algorithm>
#include <cstdlib>
#include <filesystem>
#include <fstream>

namespace fs = std::filesystem;

namespace ecu {
namespace {

std::string readText(const std::string& path) {
  std::ifstream f(path);
  if (!f) return "";
  std::string s;
  std::getline(f, s);
  while (!s.empty() && (s.back() == '\n' || s.back() == '\r' || s.back() == ' ')) s.pop_back();
  return s;
}

bool readDouble(const std::string& path, double& out) {
  std::string s = readText(path);
  if (s.empty()) return false;
  char* end = nullptr;
  out = std::strtod(s.c_str(), &end);
  return end && end != s.c_str();
}

std::vector<fs::path> children(const fs::path& dir) {
  std::vector<fs::path> out;
  std::error_code ec;
  for (const auto& e : fs::directory_iterator(dir, ec)) out.push_back(e.path());
  std::sort(out.begin(), out.end());
  return out;
}

bool startsWith(const std::string& s, const std::string& p) { return s.compare(0, p.size(), p) == 0; }
bool endsWith(const std::string& s, const std::string& p) {
  return s.size() >= p.size() && s.compare(s.size() - p.size(), p.size(), p) == 0;
}

}  // namespace

SysfsSensors::SysfsSensors(std::string root, double fan_rpm_max) : root_(std::move(root)), fan_rpm_max_(fan_rpm_max) {}

bool SysfsSensors::probe() {
  temps_.clear();
  tj_path_.clear();
  vin_path_.clear();
  iin_path_.clear();
  fan_path_.clear();
  gpu_load_path_.clear();

  // Thermal zones: keep the ones that actually report (cv* zones on Orin do not).
  for (const auto& z : children(root_ + "/sys/class/thermal")) {
    if (!startsWith(z.filename().string(), "thermal_zone")) continue;
    double t;
    if (!readDouble((z / "temp").string(), t)) continue;
    std::string type = readText((z / "type").string());
    temps_.emplace_back(type, (z / "temp").string());
    if (type == "tj-thermal") tj_path_ = (z / "temp").string();
  }

  // INA3221 power monitor: the channel labelled VDD_IN is the board's input rail.
  for (const auto& dev : children(root_ + "/sys/bus/i2c/drivers/ina3221")) {
    for (const auto& hw : children(dev / "hwmon")) {
      for (int i = 1; i <= 3 && vin_path_.empty(); ++i) {
        std::string base = (hw / ("in" + std::to_string(i))).string();
        if (readText(base + "_label") == "VDD_IN") {
          vin_path_ = base + "_input";
          iin_path_ = (hw / ("curr" + std::to_string(i) + "_input")).string();
        }
      }
    }
  }

  for (const auto& hw : children(root_ + "/sys/devices/platform/pwm-fan/hwmon")) {
    double v;
    if (readDouble((hw / "pwm1").string(), v)) { fan_path_ = (hw / "pwm1").string(); break; }
  }

  for (const auto& dev : children(root_ + "/sys/devices/platform/bus@0")) {
    if (!endsWith(dev.filename().string(), ".gpu")) continue;
    double v;
    if (readDouble((dev / "load").string(), v)) { gpu_load_path_ = (dev / "load").string(); break; }
  }

  return !temps_.empty() || !vin_path_.empty();
}

SensorReading SysfsSensors::read() const {
  SensorReading r;
  double v;
  bool any = false;
  if (!tj_path_.empty() && readDouble(tj_path_, v)) {
    r.temp_c = static_cast<float>(v / 1000.0);
    any = true;
  } else {
    double hottest = -1e9;
    for (const auto& [type, path] : temps_)
      if (readDouble(path, v) && v > hottest) hottest = v;
    if (hottest > -1e9) { r.temp_c = static_cast<float>(hottest / 1000.0); any = true; }
  }
  if (!vin_path_.empty() && readDouble(vin_path_, v)) {
    r.voltage_v = static_cast<float>(v / 1000.0);
    any = true;
    double a;
    if (readDouble(iin_path_, a)) r.current_a = static_cast<float>(a / 1000.0);
    r.power_w = r.voltage_v * r.current_a;
  }
  if (!fan_path_.empty() && readDouble(fan_path_, v)) {
    r.fan_pct = static_cast<float>(100.0 * v / 255.0);
    r.rpm = static_cast<float>(v / 255.0 * fan_rpm_max_);
  }
  if (!gpu_load_path_.empty() && readDouble(gpu_load_path_, v)) r.gpu_pct = static_cast<float>(v / 10.0);  // 0..1000
  r.ok = any;
  return r;
}

std::string SysfsSensors::describe() const {
  std::string s = "sensors:";
  s += " temp=" + (tj_path_.empty() ? (temps_.empty() ? std::string("none") : "hottest of " + std::to_string(temps_.size()) + " zones")
                                     : std::string("tj-thermal"));
  s += " rail=" + (vin_path_.empty() ? std::string("none") : std::string("INA3221 VDD_IN"));
  s += " fan=" + (fan_path_.empty() ? std::string("none") : std::string("pwm-fan"));
  s += " gpu=" + (gpu_load_path_.empty() ? std::string("none") : std::string("load"));
  return s;
}

}  // namespace ecu
