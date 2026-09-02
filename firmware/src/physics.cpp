#include "ecu/physics.hpp"

#include <algorithm>
#include <cmath>

namespace {
constexpr double kPi = 3.14159265358979323846;
}

namespace ecu {

// ---------------------------------------------------------------------------
// PRNG

Rng::Rng(uint64_t seed) : s_(seed ? seed * 0x9E3779B97F4A7C15ull : 0x9E3779B97F4A7C15ull) {}

uint64_t Rng::next() {
  // xorshift64*
  s_ ^= s_ >> 12;
  s_ ^= s_ << 25;
  s_ ^= s_ >> 27;
  return s_ * 0x2545F4914F6CDD1Dull;
}
double Rng::uniform() { return static_cast<double>(next() >> 11) * (1.0 / 9007199254740992.0); }
double Rng::uniform(double a, double b) { return a + (b - a) * uniform(); }
double Rng::gaussian() {
  double u1 = uniform();
  double u2 = uniform();
  if (u1 < 1e-300) u1 = 1e-300;
  return std::sqrt(-2.0 * std::log(u1)) * std::cos(2.0 * kPi * u2);
}

// ---------------------------------------------------------------------------

bool parseProfile(const std::string& s, Profile& out) {
  if (s == "city") out = Profile::CITY;
  else if (s == "highway") out = Profile::HIGHWAY;
  else if (s == "idle") out = Profile::IDLE;
  else if (s == "stress") out = Profile::STRESS;
  else return false;
  return true;
}

const char* profileName(Profile p) {
  switch (p) {
    case Profile::CITY: return "city";
    case Profile::HIGHWAY: return "highway";
    case Profile::IDLE: return "idle";
    case Profile::STRESS: return "stress";
  }
  return "?";
}

Physics::Physics(uint32_t seed, Profile profile, bool gps_enabled, PlantParams params)
    : profile_(profile), gps_enabled_(gps_enabled), p_(params), rng_(seed), temp_(params.ambient_c) {
  // Randomise the starting point on the loop so a fleet does not stack up.
  heading_rad_ = rng_.uniform(0, 2 * kPi);
  voltage_ = p_.v_empty + (p_.v_full - p_.v_empty) * soc_;
  updateFlagsAndState();
}

// Drive-cycle throttle demand (0..1) as a function of simulated time.
double Physics::throttleDemand(double t) const {
  switch (profile_) {
    case Profile::IDLE:
      return 0.0;
    case Profile::HIGHWAY:
      // Cruise around 70 % with slow undulation (grades, traffic).
      return 0.70 + 0.10 * std::sin(2 * kPi * t / 90.0) + 0.05 * std::sin(2 * kPi * t / 23.0);
    case Profile::STRESS:
      // Near-continuous heavy load with periodic full-throttle bursts.
      return (std::fmod(t, 40.0) < 8.0) ? 1.0 : 0.92;
    case Profile::CITY:
    default: {
      // 60 s stop-and-go cycle: accelerate 12 s, cruise 20 s, brake 8 s, stop 20 s.
      double ph = std::fmod(t, 60.0);
      if (ph < 12.0) return 0.55 * (ph / 12.0) + 0.1;
      if (ph < 32.0) return 0.45;
      if (ph < 40.0) return 0.45 * (1.0 - (ph - 32.0) / 8.0);
      return 0.0;
    }
  }
}

void Physics::step(double dt) {
  if (dt <= 0) return;
  t_ += dt;

  // --- Motor: throttle -> rpm via first-order lag ------------------------------
  throttle_ = std::clamp(throttleDemand(t_) + 0.01 * rng_.gaussian(), 0.0, 1.0);
  double rpm_target = throttle_ * p_.rpm_max;
  double rpm_prev = rpm_;
  rpm_ += (rpm_target - rpm_) * std::min(1.0, dt / p_.rpm_tau_s);
  if (rpm_ < 0) rpm_ = 0;
  double accel_rpm_s = (rpm_ - rpm_prev) / dt;

  // --- Current ∝ torque demand (throttle + acceleration component) -------------
  // Acceleration term is clamped so a cold-start step to full throttle does not
  // look like a short circuit (controller current limiting).
  double accel_a = std::min(20.0, p_.accel_current_gain * std::max(0.0, accel_rpm_s / 1000.0));
  current_ = p_.idle_current_a + p_.current_per_throttle * throttle_ + accel_a + 1.5 * rng_.gaussian();
  if (injected_ & F_OVERCURRENT) current_ += p_.overcurrent_extra_a;
  if (current_ < 0) current_ = 0;

  // --- Battery: V = V0(SoC) - I*R -----------------------------------------------
  soc_ -= current_ * dt / p_.pack_capacity_as;
  soc_ = std::clamp(soc_, 0.0, 1.0);
  double v0 = p_.v_empty + (p_.v_full - p_.v_empty) * soc_;
  if (injected_ & F_UNDERVOLTAGE) v0 -= p_.undervoltage_drop_v;
  voltage_ = v0 - current_ * p_.pack_r_ohm + 0.1 * rng_.gaussian();

  // --- Thermal: I^2 R heating vs convective cooling -----------------------------
  double heat_w = current_ * current_ * p_.winding_r_ohm;
  if (injected_ & F_OVERTEMP) heat_w += p_.overtemp_heater_w;
  double cool_w = p_.convection_w_per_k * (temp_ - p_.ambient_c);
  temp_ += (heat_w - cool_w) / p_.thermal_c_j_per_k * dt;

  // --- Vibration: baseline + rpm-dependent + random decaying spikes -------------
  if (rng_.uniform() < p_.vib_spike_rate_hz * dt) vib_spike_ += rng_.uniform(0.5, 1.5);
  vib_spike_ *= std::exp(-dt / 0.8);
  double rpm_frac = rpm_ / p_.rpm_max;
  double vib = p_.vib_base_g + p_.vib_rpm_gain_g * rpm_frac * rpm_frac + vib_spike_ +
               p_.vib_noise_g * std::fabs(rng_.gaussian());
  if (injected_ & F_VIBRATION_HIGH) vib += p_.vibration_inject_g;

  // --- Vehicle speed via gear ratio / wheel ------------------------------------
  double wheel_rps = rpm_ / 60.0 / p_.gear_ratio;
  double speed_mps = wheel_rps * p_.wheel_circ_m;
  double speed_kph = speed_mps * 3.6;

  // --- GPS: integrate heading & speed around a circular loop route -------------
  heading_rad_ += (speed_mps / p_.route_radius_m) * dt;
  if (heading_rad_ > 2 * kPi) heading_rad_ -= 2 * kPi;
  x_m_ += speed_mps * std::sin(heading_rad_) * dt;
  y_m_ += speed_mps * std::cos(heading_rad_) * dt;
  double lat = p_.lat0 + y_m_ / 111320.0;
  double lon = p_.lon0 + x_m_ / (111320.0 * std::cos(p_.lat0 * kPi / 180.0));
  if (gps_enabled_) {  // ~1.5 m position noise
    lat += 1.5 / 111320.0 * rng_.gaussian();
    lon += 1.5 / 111320.0 * rng_.gaussian();
  }

  // --- Sensor readings (what the ECU actually "measures") ----------------------
  float rpm_reading = static_cast<float>(rpm_ + 3.0 * rng_.gaussian());
  if (injected_ & F_ENCODER_FAULT) {
    // Noisy encoder with intermittent dropouts.
    if (rng_.uniform() < 0.10) rpm_reading = 0.0f;
    else rpm_reading = static_cast<float>(rpm_ * (1.0 + 0.15 * rng_.gaussian()));
    if (rpm_reading < 0) rpm_reading = 0;
  }

  float temp_reading = static_cast<float>(temp_ + 0.2 * rng_.gaussian());
  if (injected_ & F_SENSOR_STUCK) {
    if (!stuck_valid_) {
      stuck_temp_ = temp_reading;
      stuck_valid_ = true;
    }
    temp_reading = stuck_temp_;
  } else {
    stuck_valid_ = false;
  }

  sample_.rpm = rpm_reading;
  sample_.temp_c = temp_reading;
  sample_.current_a = static_cast<float>(current_);
  sample_.voltage_v = static_cast<float>(voltage_);
  sample_.vibration_g = static_cast<float>(vib);
  sample_.speed_kph = static_cast<float>(speed_kph);
  if (gps_enabled_ && !(injected_ & F_GPS_LOST)) {
    sample_.lat = lat;
    sample_.lon = lon;
  } else {
    sample_.lat = 0.0;
    sample_.lon = 0.0;
  }

  updateFlagsAndState();
}

void Physics::setInjectedFaults(uint32_t flags) {
  injected_ = flags & F_ALL;
  updateFlagsAndState();
}

// Threshold detection uses the *readings* (so a stuck sensor hides a real
// overtemp, exactly as it would on hardware). State is derived from flags.
void Physics::updateFlagsAndState() {
  uint32_t det = 0;
  if (sample_.temp_c > limits_.temp_limit_c) det |= F_OVERTEMP;
  if (sample_.current_a > limits_.current_limit_a) det |= F_OVERCURRENT;
  if (sample_.voltage_v > 0 && sample_.voltage_v < p_.undervoltage_v) det |= F_UNDERVOLTAGE;
  if (sample_.vibration_g > p_.vibration_high_g) det |= F_VIBRATION_HIGH;
  if (sample_.rpm > limits_.rpm_limit) det |= F_ENCODER_FAULT;  // implausible speed
  detected_ = det;

  uint32_t flags = injected_ | detected_;
  if (comm_degraded_) flags |= F_COMM_DEGRADED;
  sample_.fault_flags = flags;

  constexpr uint32_t kHardFaults = F_OVERTEMP | F_OVERCURRENT | F_UNDERVOLTAGE | F_ENCODER_FAULT;
  if (flags & kHardFaults) sample_.state = DeviceState::FAULT;
  else if (flags != 0) sample_.state = DeviceState::DEGRADED;
  else if (rpm_ < 50.0 && throttle_ < 0.02) sample_.state = DeviceState::IDLE;
  else sample_.state = DeviceState::RUNNING;
}

}  // namespace ecu
