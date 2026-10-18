// Volture on the ESP32: the adapters between ESPHome and the pure logic in controller.h.
// Same role as volture/car.py + read_shelly in Python: read the car's state, parse the Shelly reply.
// The control cycle itself is volture::Loop (controller.h); esp32.yaml calls it every poll.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <optional>
#include <string>

#include "esp_timer.h"
#include "esphome/components/json/json_util.h"
#include "esphome/core/alloc_helpers.h"
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

// This device's settings: the volture: block of secrets.yaml, copied in at boot (esp32.yaml on_boot).
// The web page's settings (headroom, the no-reading fail-safe) override them every cycle (volture_cycle).
inline volture::Config &config() {
  static volture::Config instance;
  return instance;
}

inline volture::LoopSettings &loop_settings() {
  static volture::LoopSettings instance;
  return instance;
}

// The one control loop of this device; first used after on_boot has filled the settings.
inline volture::Loop &loop() {
  static volture::Loop instance(config(), loop_settings());
  return instance;
}

// True once every poll_s seconds (the page's Poll interval); the interval timer itself ticks every second.
inline bool poll_due(float poll_s) {
  static double last = -1e9;
  const double now = now_s();
  if (now - last < poll_s - 0.05) {  // 50 ms slack: the 1 s timer is not exact
    return false;
  }
  last = now;
  return true;
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

// Whether the charging cable is in; nullopt while the car has not reported it (e.g. out of Bluetooth range).
inline std::optional<bool> plugged() {
  auto *cable = find<esphome::binary_sensor::BinarySensor>(esphome::App.get_binary_sensors(), CHARGER);
  if (cable == nullptr || !cable->has_state()) {
    return std::nullopt;
  }
  return cable->state;
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
  state.plugged = plugged();
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

// Every field of the Shelly EM Gen1 /emeter/0 reply ({"power":142.17,"reactive":-411.31,"pf":-0.33,
// "voltage":230.10,"is_valid":true,"total":102.6,"total_returned":5.0}); NAN where a field is missing.
struct ShellyEm {
  bool valid = false;           // is_valid: the Shelly trusts its own measurement
  float power_w = NAN;          // active power (W)
  float reactive_var = NAN;     // reactive power (var)
  float pf = NAN;               // power factor
  float voltage_v = NAN;        // grid voltage (V)
  float total_wh = NAN;         // energy consumed since the Shelly's counter reset (Wh)
  float total_returned_wh = NAN;  // energy returned to the grid (Wh)
};

// nullopt when the body is not the expected JSON (no "power").
inline std::optional<ShellyEm> parse_shelly(const std::string &body) {
  std::optional<ShellyEm> shelly;
  esphome::json::parse_json(body, [&shelly](JsonObject root) -> bool {
    if (!root["power"].is<float>()) {
      return false;
    }
    auto number = [&root](const char *key) { return root[key].is<float>() ? root[key].as<float>() : NAN; };
    shelly = ShellyEm{root["is_valid"] | false, number("power"), number("reactive"), number("pf"),
                      number("voltage"), number("total"), number("total_returned")};
    return true;
  });
  return shelly;
}

// A true/false setting from secrets.yaml as substituted into esp32.yaml ("True", "true", "yes"...).
inline bool flag(const std::string &text) {
  std::string lower;
  for (char c : text) {
    lower += static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  }
  return lower == "true" || lower == "yes" || lower == "on" || lower == "1";
}

// Amps the car may be drawing: the higher of its reported setpoint and Volture's own belief, so a
// command the car has not confirmed yet already counts. A car drawing less than its setpoint (tapering
// near full) only makes the estimate high, which errs on the safe side.
inline int car_amps(const std::optional<volture::CarState> &observed, const volture::Controller &ctrl) {
  const int reported = observed && observed->charging ? observed->amps : 0;
  const int believed = ctrl.charging ? ctrl.amps : 0;
  return std::max(reported, believed);
}

// What the controller needs from the Shelly: the WHOLE load it must keep under the contracted power.
// shelly_includes_tesla (secrets.yaml): true when the clamp is on the main feed and sees the car too;
// false when the Shelly is on the house circuit only, and the car's draw (amps x volts) is added.
inline volture::Reading reading_of(const ShellyEm &shelly, bool includes_tesla, int amps_of_car) {
  volture::Reading reading{shelly.power_w, shelly.valid, std::nullopt};
  if (!std::isnan(shelly.voltage_v)) {
    reading.voltage_v = shelly.voltage_v;
  }
  if (!includes_tesla) {
    reading.power_w += amps_of_car * volture::effective_voltage(config(), reading);
  }
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

// ---- Grafana push (issue #12): one line per cycle in which something changed ----
// Lines are buffered and sent once a minute, each with its own timestamp (from SNTP, used only
// here, never by the control logic). Grafana Cloud ignores text fields, so texts travel as labels
// on a second line (metric volture_state_value). Health values (heap, signals) ride along but do
// not count as a change on their own.

struct Sample {
  std::optional<volture::Reading> reading;
  std::optional<volture::CarState> car;
  float car_current_a = NAN;  // what the car actually draws ("Charger Current")
  float car_power_kw = NAN;   // "Charger Power"
  int amps = 0;               // Volture's decided amps (0: stopped)
  bool control = false;
  std::optional<volture::Command> last_sent;  // last command sent to the car
  int attempts = 0;                           // sends of the pending command (0: none pending)
  std::string reason, event, answer, status;  // decision, outcome, firmware's last_command, charging text
};

// Same content, health values aside: nothing worth a new line.
inline bool same(const Sample &a, const Sample &b) {
  auto power = [](const Sample &s) { return s.reading && s.reading->valid ? s.reading->power_w : -1.0; };
  auto car_key = [](const Sample &s) {
    return s.car ? (s.car->charging ? 1000 : 0) + s.car->amps + (s.car->plugged.value_or(false) ? 10000 : 0) : -1;
  };
  auto sent_key = [](const Sample &s) { return s.last_sent ? (s.last_sent->charging ? 1000 : 0) + s.last_sent->amps : -1; };
  return power(a) == power(b) && car_key(a) == car_key(b) && a.amps == b.amps && a.control == b.control &&
         sent_key(a) == sent_key(b) && a.attempts == b.attempts && a.reason == b.reason && a.event == b.event &&
         a.answer == b.answer && a.status == b.status &&
         std::isnan(a.car_current_a) == std::isnan(b.car_current_a) &&
         (std::isnan(a.car_current_a) || a.car_current_a == b.car_current_a);
}

// Line-protocol tag value: spaces, commas and '=' must be escaped; empty -> "none".
inline std::string tag(const std::string &text) {
  if (text.empty()) {
    return "none";
  }
  std::string out;
  for (char c : text) {
    if (c == ' ' || c == ',' || c == '=') {
      out += '\\';
    }
    out += c;
  }
  return out;
}

// Two lines for one sample at unix time `ns` (nanoseconds). Missing values are left out.
inline std::string sample_lines(const Sample &s, unsigned heap_free, unsigned heap_block, float wifi_dbm,
                                float ble_dbm, long long ns) {
  std::string line = "volture amps=" + std::to_string(s.amps) + ",control=" + (s.control ? "1" : "0");
  char buf[64];
  auto field = [&line, &buf](const char *name, double value, const char *format) {
    if (!std::isnan(value)) {
      std::snprintf(buf, sizeof(buf), format, value);
      line += std::string(",") + name + "=" + buf;
    }
  };
  if (s.reading) {
    line += std::string(",shelly_valid=") + (s.reading->valid ? "1" : "0");
    if (s.reading->valid) {
      field("house_w", s.reading->power_w, "%.0f");
      if (s.reading->voltage_v) {
        field("house_v", *s.reading->voltage_v, "%.1f");
        field("house_a", s.reading->power_w / *s.reading->voltage_v, "%.2f");
      }
    }
  }
  if (s.car) {
    line += std::string(",car_charging=") + (s.car->charging ? "1" : "0") + ",car_amps=" + std::to_string(s.car->amps);
    if (s.car->plugged) {
      line += std::string(",car_plugged=") + (*s.car->plugged ? "1" : "0");
    }
    field("car_battery_pct", s.car->battery_pct.value_or(NAN), "%.0f");
    field("car_limit_pct", s.car->limit_pct.value_or(NAN), "%.0f");
  }
  field("car_current_a", s.car_current_a, "%.1f");
  field("car_power_kw", s.car_power_kw, "%.2f");
  if (s.last_sent) {
    line += std::string(",sent_charging=") + (s.last_sent->charging ? "1" : "0") +
            ",sent_amps=" + std::to_string(s.last_sent->amps);
  }
  line += ",sent_attempts=" + std::to_string(s.attempts) + ",heap_free=" + std::to_string(heap_free) +
          ",heap_block=" + std::to_string(heap_block);
  field("wifi_dbm", wifi_dbm, "%.0f");
  field("ble_dbm", ble_dbm, "%.0f");
  const std::string stamp = " " + std::to_string(ns) + "\n";
  return line + stamp + "volture_state,reason=" + tag(s.reason) + ",event=" + tag(s.event) +
         ",answer=" + tag(s.answer) + ",status=" + tag(s.status) + " value=1" + stamp;
}

// Lines waiting for the next push. Bounded: when a push keeps failing, the oldest are dropped
// rather than eating the memory the car link needs.
constexpr size_t MAX_BUFFER = 8 * 1024;

struct Telemetry {
  std::optional<Sample> previous;
  double previous_at = -1e9;  // now_s() of the last buffered sample
  std::optional<volture::Command> last_sent;
  std::string buffer;
};

inline Telemetry &telemetry() {
  static Telemetry instance;
  return instance;
}

// Buffer a sample if something changed, or if a minute passed (so Grafana never shows the
// series as stale: it drops series not seen for 5 minutes). At most one every MIN_SAMPLE_S: the
// cycle runs every second, and one line per second would overflow MAX_BUFFER between pushes.
constexpr double MIN_SAMPLE_S = 5.0;

inline void record(const Sample &sample, double now, unsigned heap_free, unsigned heap_block, float wifi_dbm,
                   float ble_dbm, long long unix_ns) {
  Telemetry &t = telemetry();
  if (now - t.previous_at < MIN_SAMPLE_S) {
    return;
  }
  if (t.previous && same(*t.previous, sample) && now - t.previous_at < 60.0) {
    return;
  }
  t.buffer += sample_lines(sample, heap_free, heap_block, wifi_dbm, ble_dbm, unix_ns);
  if (t.buffer.size() > MAX_BUFFER) {
    const size_t cut = t.buffer.find('\n', t.buffer.size() - MAX_BUFFER);
    t.buffer.erase(0, cut == std::string::npos ? t.buffer.size() : cut + 1);
  }
  t.previous = sample;
  t.previous_at = now;
}

// An HTTPS push needs mbedTLS's 16 KB receive buffer in one block plus ~20 KB more for the handshake.
// Starting one with less ran the heap out (abort) when several unread pages held memory, so the push
// waits for the next minute instead (the buffer keeps its lines, bounded by MAX_BUFFER).
constexpr size_t PUSH_MIN_FREE = 45 * 1024;
constexpr size_t PUSH_MIN_BLOCK = 32 * 1024;

inline bool memory_for_push() {
  return heap_caps_get_free_size(MALLOC_CAP_8BIT) >= PUSH_MIN_FREE &&
         heap_caps_get_largest_free_block(MALLOC_CAP_8BIT) >= PUSH_MIN_BLOCK;
}

// "Basic base64(user:token)" for the Authorization header; built once, kept for the program's life.
inline const char *basic_auth(const std::string &user, const std::string &token) {
  static const std::string header = [&] {
    const std::string credentials = user + ":" + token;
    return "Basic " + esphome::base64_encode(reinterpret_cast<const uint8_t *>(credentials.data()), credentials.size());
  }();
  return header.c_str();
}

template<typename Entity, typename List> float value_of(const List &entities, const char *name) {
  Entity *entity = find<Entity>(entities, name);
  return entity != nullptr && entity->has_state() ? entity->state : NAN;
}

inline std::string text_of(const char *name) {
  auto *entity = find<esphome::text_sensor::TextSensor>(esphome::App.get_text_sensors(), name);
  return entity != nullptr && entity->has_state() ? entity->state : std::string();
}

// The car firmware creates ~90 entities: every control the paired key allows plus every reading.
// The web page and the API show only this project's entities (those in a section of esp32.yaml) and the car's
// charging readings (names starting "Charg"); everything else becomes internal. Why: (1) no car control
// can be tapped by accident (the owner uses the Tesla app; this loop drives the car through the entities
// directly, which internal entities still allow); (2) the web server serializes and queues every visible
// entity for each open page, which ran the heap out (abort in AsyncEventSourceResponse::deferrable_send_state).
// The car readings that stay go to a "Tesla" section, so every value on the page says where it comes from.
constexpr uint64_t TESLA_GROUP = 0x7e51a;  // any id not used by esp32.yaml's sorting_groups (hashes of their ids)

// This project's entities are the ones esp32.yaml placed in a page section (a web_server: block).
inline bool ours(esphome::web_server::WebServer *web, esphome::EntityBase *entity) {
  return web->sorting_entitys_.contains(entity);
}

// The one car control kept: pairing (setup guide, step 7) starts from it. A stray tap only asks the car
// to pair, which still needs the NFC key card on the console.
constexpr const char *PAIR_BUTTON = "Pair BLE Key";

template<typename List> void arrange(esphome::web_server::WebServer *web, const List &entities, bool read_only) {
  for (auto *entity : entities) {
    if (ours(web, entity)) {
      continue;
    }
    const auto &name = entity->get_name();
    if ((read_only && name.starts_with("Charg")) || name == PAIR_BUTTON) {
      web->add_entity_config(entity, 50, TESLA_GROUP);
    } else {
      entity->set_internal(true);
    }
  }
}

inline void arrange_page(esphome::web_server::WebServer *web) {
  using esphome::App;
  web->add_sorting_group(TESLA_GROUP, "Tesla", 0);
  arrange(web, App.get_binary_sensors(), true);
  arrange(web, App.get_sensors(), true);
  arrange(web, App.get_text_sensors(), true);
  arrange(web, App.get_switches(), false);
  arrange(web, App.get_buttons(), false);
  arrange(web, App.get_numbers(), false);
  arrange(web, App.get_selects(), false);
  arrange(web, App.get_texts(), false);
  arrange(web, App.get_locks(), false);
  arrange(web, App.get_covers(), false);
  arrange(web, App.get_climates(), false);
}

// The page's Summary, from what the car reports it draws. NAN where it cannot be known.
struct Summary {
  float total_w;  // house + Tesla
  float tesla_w;  // car current (as the car reports it) x Shelly voltage
  float house_w;  // everything but the car
};

inline Summary summary(float shelly_power_w, float shelly_voltage_v, float car_current_a, std::optional<bool> car_plugged,
                       bool includes_tesla) {
  float car_a = car_current_a;
  // No current reported and the cable not known to be in: the car is unplugged or out of Bluetooth
  // range, which almost always means not at home (owner's choice: count it as 0 W).
  if (std::isnan(car_a) && !car_plugged.value_or(false)) {
    car_a = 0;
  }
  const float tesla_w = car_a * shelly_voltage_v;
  if (includes_tesla) {
    return Summary{shelly_power_w, tesla_w, shelly_power_w - tesla_w};
  }
  return Summary{shelly_power_w + tesla_w, tesla_w, shelly_power_w};
}

// Publish only when the value changes: every publish is an event sent to every open page.
inline void publish_changed(esphome::sensor::Sensor *sensor, float value) {
  const bool same = sensor->has_state() &&
                    ((std::isnan(value) && std::isnan(sensor->state)) || value == sensor->state);
  if (!same) {
    sensor->publish_state(value);
  }
}

// Daily reboot (secrets.yaml scheduled_reboot_hour; -1 = never), called every hour on the hour.
// Skipped while the cable is plugged in: whether Volture paused the car lives only in RAM
// (volture::Controller::paused_by_us), so a reboot then would leave that charge never resumed.
// Unknown cable state (car out of Bluetooth range, i.e. not at home) counts as unplugged.
inline void reboot_if_due(int reboot_hour, int hour_now) {
  if (reboot_hour < 0 || hour_now != reboot_hour) {
    return;
  }
  if (plugged().value_or(false)) {
    ESP_LOGI("volture", "scheduled reboot skipped: car plugged in");
    return;
  }
  ESP_LOGI("volture", "scheduled reboot");
  esphome::App.safe_reboot();
}

}  // namespace volture_device
