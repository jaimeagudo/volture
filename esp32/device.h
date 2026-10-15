// Volture on the ESP32: the adapters between ESPHome and the pure logic in controller.h.
// Same role as volture/car.py + read_shelly in Python: read the car's state, parse the Shelly reply.
// The control cycle itself is volture::Loop (controller.h); esp32.yaml calls it every poll.
#pragma once

#include <optional>
#include <string>

#include "esp_timer.h"
#include "esphome/components/json/json_util.h"
#include "esphome/core/application.h"
#include "controller.h"

namespace volture_device {

// Entity names in the yoziru/esphome-tesla-ble firmware (components/tesla_ble_vehicle/__init__.py).
// Its entities have generated ids, so they are found by name. "Charger" names two entities:
// a switch (charging on/off) and a binary sensor (cable connected).
constexpr const char *CHARGER = "Charger";
constexpr const char *CHARGING_AMPS = "Charging Amps";
constexpr const char *CHARGING_LIMIT = "Charging Limit";
constexpr const char *BATTERY = "Battery";

// Seconds since boot, from a 64-bit microsecond timer: millis() wraps after 49 days, this does not.
inline double now_s() { return static_cast<double>(esp_timer_get_time()) / 1e6; }

// The one control loop of this device, with the defaults of Config / LoopSettings.
// TODO: settings from the live settings page instead of the defaults.
inline volture::Loop &loop() {
  static volture::Loop instance;
  return instance;
}

template<typename Entity, typename List> Entity *find(const List &entities, const char *name) {
  for (Entity *entity : entities) {
    if (entity->get_name() == name) {
      return entity;
    }
  }
  return nullptr;
}

inline esphome::switch_::Switch *charger_switch() {
  return find<esphome::switch_::Switch>(esphome::App.get_switches(), CHARGER);
}

// The car's state as the firmware last reported it; nullopt until charging and amps are known.
inline std::optional<volture::CarState> observe() {
  auto *charging = charger_switch();
  auto *amps = find<esphome::number::Number>(esphome::App.get_numbers(), CHARGING_AMPS);
  if (charging == nullptr || amps == nullptr || !amps->has_state()) {
    return std::nullopt;
  }
  volture::CarState state{charging->state, static_cast<int>(amps->state + 0.5f), std::nullopt, std::nullopt,
                          std::nullopt};
  auto *plugged = find<esphome::binary_sensor::BinarySensor>(esphome::App.get_binary_sensors(), CHARGER);
  if (plugged != nullptr && plugged->has_state()) {
    state.plugged = plugged->state;
  }
  auto *battery = find<esphome::sensor::Sensor>(esphome::App.get_sensors(), BATTERY);
  if (battery != nullptr && battery->has_state()) {
    state.battery_pct = battery->state;
  }
  auto *limit = find<esphome::number::Number>(esphome::App.get_numbers(), CHARGING_LIMIT);
  if (limit != nullptr && limit->has_state()) {
    state.limit_pct = limit->state;
  }
  return state;
}

// Shelly EM Gen1 /emeter/0 JSON -> Reading. nullopt when the body is not the expected JSON.
inline std::optional<volture::Reading> parse_shelly(const std::string &body) {
  std::optional<volture::Reading> reading;
  esphome::json::parse_json(body, [&reading](JsonObject root) -> bool {
    if (!root["power"].is<float>()) {
      return false;
    }
    volture::Reading parsed{root["power"].as<double>(), root["is_valid"] | false, std::nullopt};
    if (root["voltage"].is<float>()) {
      parsed.voltage_v = root["voltage"].as<double>();
    }
    reading = parsed;
    return true;
  });
  return reading;
}

// Send a command through the firmware's entities, as the Tesla app's equivalent buttons would.
// Amps first, so charging never (re)starts at a stale, higher setpoint. No start/stop when the
// switch already shows it: the car rejects a start while charging ("is_charging").
inline bool send(const volture::Command &cmd) {
  auto *sw = charger_switch();
  auto *amps = find<esphome::number::Number>(esphome::App.get_numbers(), CHARGING_AMPS);
  if (sw == nullptr || amps == nullptr) {
    return false;
  }
  if (cmd.charging) {
    amps->make_call().set_value(static_cast<float>(cmd.amps)).perform();
  }
  if (sw->state != cmd.charging) {
    if (cmd.charging) {
      sw->turn_on();
    } else {
      sw->turn_off();
    }
  }
  return true;
}

}  // namespace volture_device
