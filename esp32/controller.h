// Volture control logic in C++, for the ESP32. Pure: standard library only, no ESPHome, no I/O.
//
// This is a line-by-line port of volture/controller.py and volture/confirm.py. Both versions must
// behave identically: tests/test_controller.py runs every scenario against the Python version and
// against this one (through tests/cpp/controller_cli.cpp). Change both, or the tests fail.
#pragma once

#include <algorithm>
#include <cmath>
#include <limits>
#include <optional>
#include <utility>

namespace volture {

// A Shelly voltage outside this range is treated as a glitch: fall back to Config::fallback_voltage_v.
constexpr double PLAUSIBLE_VOLTAGE_MIN_V = 180.0;
constexpr double PLAUSIBLE_VOLTAGE_MAX_V = 260.0;

struct Config {
  double contracted_power_w = 4600.0;  // utility limit (20 A at 230 V): never exceed
  double headroom_w = 230.0;           // margin below it for loads that switch on between readings
  double fallback_voltage_v = 230.0;   // used when the Shelly gives no plausible voltage
  int min_amps = 5;                    // lowest current the car accepts
  int max_amps = 32;                   // charger / car maximum
  int ramp_up_step_amps = 1;           // amps added per ramp-up step
  double ramp_up_every_s = 15.0;       // minimum time between ramp-up steps
  double resume_after_s = 60.0;        // power must stay low this long before restarting
  double stop_after_invalid_s = 30.0;  // no trustworthy reading for this long -> stop

  double working_limit_w() const { return contracted_power_w - headroom_w; }
};

struct Reading {
  double power_w;
  bool valid = true;
  std::optional<double> voltage_v;
};

struct Command {
  bool charging;
  int amps;
  bool operator==(const Command &other) const { return charging == other.charging && amps == other.amps; }
  bool operator!=(const Command &other) const { return !(*this == other); }
};

inline double effective_voltage(const Config &cfg, const Reading &reading) {
  if (reading.voltage_v && *reading.voltage_v >= PLAUSIBLE_VOLTAGE_MIN_V &&
      *reading.voltage_v <= PLAUSIBLE_VOLTAGE_MAX_V) {
    return *reading.voltage_v;
  }
  return cfg.fallback_voltage_v;
}

class Controller {
 public:
  explicit Controller(Config cfg = Config()) : cfg_(cfg) {}

  bool charging = false;
  int amps = 0;
  // True only after Volture itself stopped a charge (power over the limit, no reading):
  // the one case where it may start the car again.
  bool paused_by_us = false;

  // A change made outside Volture (Tesla app, schedule, the car itself): take it as is.
  // A start is taken over (amps rise to whatever fits); a stop stays a stop.
  void follow_user(bool car_charging, int car_amps) {
    adopt(car_charging, car_amps);
    paused_by_us = false;
  }

  // Align the controller's belief with the car's reported state.
  void adopt(bool car_charging, int car_amps) {
    charging = car_charging;
    amps = car_charging ? car_amps : 0;
    // A stale "low power since" would restart charging at once instead of after resume_after_s.
    low_since_.reset();
  }

  // car_can_charge: false when the car is unplugged or at its battery charge limit.
  std::optional<Command> tick(double now, const std::optional<Reading> &reading, bool car_can_charge = true) {
    const Config &c = cfg_;
    if (!reading || !reading->valid) {
      low_since_.reset();
      if (!invalid_since_) {
        invalid_since_ = now;
      }
      if (!charging) {
        return std::nullopt;
      }
      if (now - *invalid_since_ >= c.stop_after_invalid_s) {
        return stop_(now, true);
      }
      if (amps > c.min_amps) {
        return set_(c.min_amps, now);
      }
      return std::nullopt;
    }
    invalid_since_.reset();
    const double p = reading->power_w;
    const double volts = effective_voltage(c, *reading);

    if (!charging) {
      // Over the contracted power while we believe the car is stopped: the car may be charging
      // without us. A stop to an idle car is harmless, so send one, at most every ramp_up_every_s.
      // Not resumable: Volture never starts a charge it did not know about.
      if (p > c.contracted_power_w) {
        low_since_.reset();
        if (now - last_change_ >= c.ramp_up_every_s) {
          return stop_(now, paused_by_us);
        }
        return std::nullopt;
      }
      // Restart only a charge Volture stopped itself, if the car can take charge and
      // house + minimum charge fits the ceiling.
      if (paused_by_us && car_can_charge && p + c.min_amps * volts <= c.working_limit_w()) {
        if (!low_since_) {
          low_since_ = now;
        }
        if (now - *low_since_ >= c.resume_after_s) {
          low_since_.reset();
          return set_(c.min_amps, now);
        }
      } else {
        low_since_.reset();
      }
      return std::nullopt;
    }

    if (p > c.working_limit_w()) {
      const int delta = static_cast<int>(std::ceil((p - c.working_limit_w()) / volts));
      int target = amps - delta;
      if (target < c.min_amps) {
        if (amps == c.min_amps && p > c.contracted_power_w) {
          return stop_(now, true);
        }
        target = c.min_amps;
      }
      if (target != amps) {
        return set_(target, now);
      }
      return std::nullopt;
    }

    const int step = std::min(c.ramp_up_step_amps, c.max_amps - amps);
    if (step > 0 && p + step * volts <= c.working_limit_w() && now - last_change_ >= c.ramp_up_every_s) {
      return set_(amps + step, now);
    }
    return std::nullopt;
  }

 private:
  Command set_(int new_amps, double now) {
    charging = true;
    amps = new_amps;
    paused_by_us = false;
    last_change_ = now;
    return Command{true, new_amps};
  }

  // resumable: Volture stopped a charge it knew of, so it may restart it later.
  Command stop_(double now, bool resumable) {
    charging = false;
    amps = 0;
    paused_by_us = resumable;
    last_change_ = now;
    return Command{false, 0};
  }

  Config cfg_;
  double last_change_ = -std::numeric_limits<double>::infinity();
  std::optional<double> low_since_;
  std::optional<double> invalid_since_;
};

// ---- Confirming commands from the car's reported state (port of volture/confirm.py) ----

struct CarState {
  bool charging;
  int amps;
  std::optional<bool> plugged;        // charge cable connected
  std::optional<double> battery_pct;  // state of charge
  std::optional<double> limit_pct;    // the car's charge limit (set in the car / Tesla app)
};

struct Pending {
  Command cmd;
  double sent_at;
  int attempts = 1;
};

enum class Action { WAIT, CONFIRMED, RESEND, ADOPT, UNKNOWN };

inline bool matches(const Command &cmd, const CarState &state) {
  // Amps only matter while charging: a stopped car keeps its last setpoint.
  return state.charging == cmd.charging && (!cmd.charging || state.amps == cmd.amps);
}

// Same charging on/off and, while charging, same amps (battery % etc. ignored).
inline bool same_drive(const CarState &a, const CarState &b) {
  return a.charging == b.charging && (!a.charging || a.amps == b.amps);
}

// The car changed, and not to what Volture asked for: someone else changed it.
inline bool external_change(const std::optional<Pending> &pending, const std::optional<CarState> &last,
                            const std::optional<CarState> &observed) {
  if (!observed || !last || same_drive(*observed, *last)) {
    return false;
  }
  return !pending || !matches(pending->cmd, *observed);
}

// False when starting is pointless: cable unplugged or battery at its charge limit.
// Unknown values do not block: the car itself refuses what it cannot do.
inline bool can_charge(const std::optional<CarState> &state) {
  if (!state) {
    return true;
  }
  if (state->plugged.has_value() && !*state->plugged) {
    return false;
  }
  if (state->battery_pct && state->limit_pct) {
    return *state->battery_pct < *state->limit_pct;
  }
  return true;
}

// Next step for an unconfirmed command, and what stays pending afterwards.
inline std::pair<Action, std::optional<Pending>> reconcile(const Pending &pending,
                                                           const std::optional<CarState> &observed, double now,
                                                           double timeout_s, int max_attempts) {
  if (observed && matches(pending.cmd, *observed)) {
    return {Action::CONFIRMED, std::nullopt};
  }
  if (now - pending.sent_at < timeout_s) {
    return {Action::WAIT, pending};
  }
  if (pending.attempts < max_attempts) {
    return {Action::RESEND, Pending{pending.cmd, now, pending.attempts + 1}};
  }
  if (!observed) {
    return {Action::UNKNOWN, std::nullopt};
  }
  return {Action::ADOPT, std::nullopt};
}

}  // namespace volture
