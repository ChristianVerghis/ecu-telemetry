// Simulated motor / battery / thermal / vibration / GPS plant model.
// Fully deterministic for a given seed (own PRNG, no <random> so results do not
// depend on the standard library implementation).
#pragma once

#include <cstdint>
#include <string>

#include "ecu/config.hpp"
#include "ecu/protocol.hpp"

namespace ecu {

enum class Profile { CITY, HIGHWAY, IDLE, STRESS };
bool parseProfile(const std::string& s, Profile& out);
const char* profileName(Profile p);

// Small deterministic PRNG (xorshift64*) with uniform / gaussian helpers.
class Rng {
 public:
  explicit Rng(uint64_t seed);
  uint64_t next();
  double uniform();                 // [0,1)
  double uniform(double a, double b);
  double gaussian();                // mean 0, sd 1 (Box-Muller)

 private:
  uint64_t s_;
};

// Tunable plant parameters (defaults are a ~100 V, 6000 rpm traction motor).
struct PlantParams {
  double rpm_max = 6000.0;         // rpm at full throttle
  double rpm_tau_s = 1.5;          // first-order lag time constant
  double idle_current_a = 5.0;     // controller quiescent draw
  double current_per_throttle = 120.0;
  double accel_current_gain = 40.0;  // extra A per (1000 rpm/s) of acceleration
  double v_empty = 88.0, v_full = 100.0;  // open-circuit voltage vs SoC
  double pack_r_ohm = 0.03;        // internal resistance
  double pack_capacity_as = 360000.0;  // 100 Ah
  double winding_r_ohm = 0.05;     // I^2 R heating
  double thermal_c_j_per_k = 600.0;
  double convection_w_per_k = 10.0;
  double ambient_c = 25.0;
  double vib_base_g = 0.15, vib_rpm_gain_g = 0.4, vib_noise_g = 0.04;
  double vib_spike_rate_hz = 0.02;  // random spikes per second
  double gear_ratio = 6.0, wheel_circ_m = 2.0;
  double route_radius_m = 500.0;   // GPS loop route
  double lat0 = 37.7749, lon0 = -122.4194;
  // Detection thresholds not covered by Limits
  double undervoltage_v = 80.0;
  double vibration_high_g = 2.0;
  // Fault-injection effects
  double overtemp_heater_w = 2000.0;
  double overcurrent_extra_a = 60.0;
  double undervoltage_drop_v = 20.0;
  double vibration_inject_g = 2.5;
};

class Physics {
 public:
  Physics(uint32_t seed, Profile profile, bool gps_enabled = true, PlantParams params = PlantParams{});

  // Advance the simulation by dt seconds and refresh the sensor readings.
  void step(double dt_s);

  // Faults injected from outside (FAULT_INJECT / --fault). Bits in this mask are
  // both reported and, where meaningful, alter the plant behaviour.
  void setInjectedFaults(uint32_t flags);
  uint32_t injectedFaults() const { return injected_; }
  // COMM_DEGRADED is owned by the agent (TCP link state).
  void setCommDegraded(bool degraded) { comm_degraded_ = degraded; }
  void setLimits(const Limits& l) { limits_ = l; }
  const Limits& limits() const { return limits_; }

  // Latest sensor readings / derived state (valid after step()).
  const Telemetry& sample() const { return sample_; }
  uint32_t faultFlags() const { return sample_.fault_flags; }
  // Flags detected by the device's own threshold checks (subset of faultFlags()).
  uint32_t detectedFaults() const { return detected_; }
  DeviceState state() const { return sample_.state; }
  double simTime() const { return t_; }
  double throttle() const { return throttle_; }
  double trueTemp() const { return temp_; }

 private:
  double throttleDemand(double t) const;
  void updateFlagsAndState();

  Profile profile_;
  bool gps_enabled_;
  PlantParams p_;
  Limits limits_;
  Rng rng_;

  // Plant state
  double t_ = 0;
  double throttle_ = 0;
  double rpm_ = 0;
  double current_ = 0;
  double voltage_ = 0;
  double soc_ = 0.9;
  double temp_;
  double vib_spike_ = 0;      // decaying spike amplitude
  double heading_rad_ = 0;
  double x_m_ = 0, y_m_ = 0;  // ENU displacement from (lat0, lon0)

  uint32_t injected_ = 0;
  uint32_t detected_ = 0;
  bool comm_degraded_ = false;
  bool stuck_valid_ = false;
  float stuck_temp_ = 0;      // frozen reading while SENSOR_STUCK

  Telemetry sample_;
};

}  // namespace ecu
