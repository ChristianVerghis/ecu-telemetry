// Real sensor source: Linux sysfs/hwmon readings that stand in for the plant
// model's motor fields. Written for the Jetson Orin Nano (tegra thermal zones,
// the INA3221 power monitor, the PWM fan) but every path is discovered under a
// configurable root so tests run against a fixture tree. No dependencies.
#pragma once

#include <string>
#include <vector>

namespace ecu {

struct SensorReading {
  bool ok = false;
  float temp_c = 0;      // hottest zone, tj-thermal when present
  float voltage_v = 0;   // VDD_IN bus voltage
  float current_a = 0;   // VDD_IN current
  float power_w = 0;     // voltage * current
  float fan_pct = 0;     // PWM duty 0..100
  float rpm = 0;         // fan speed estimate: duty * fan_rpm_max (the board's one real motor)
  float gpu_pct = 0;     // GPU busy 0..100
};

class SysfsSensors {
 public:
  // `root` is prefixed to /sys and /proc paths ("" = the live system).
  explicit SysfsSensors(std::string root = "", double fan_rpm_max = 6000.0);
  // Discover paths. Returns true if at least a temperature or a power rail was found.
  bool probe();
  SensorReading read() const;
  // One line naming what probe() found, for the start-up log.
  std::string describe() const;

 private:
  std::string root_;
  double fan_rpm_max_;
  std::vector<std::pair<std::string, std::string>> temps_;  // (zone type, temp path)
  std::string tj_path_;
  std::string vin_path_, iin_path_;
  std::string fan_path_;
  std::string gpu_load_path_;
};

}  // namespace ecu
