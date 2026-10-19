// Finds the car's Bluetooth address from its VIN (esp32/car_finder.yaml wires it up).
#pragma once
#include <cstdint>
#include <cstdio>
#include <string>
#include <vin_utils.h>  // TeslaBLE::get_vin_advertisement_name, from the yoziru/esphome-tesla-ble firmware

namespace volture_car {

// The car advertises the name "S" + 16 hex characters of SHA1(VIN) + "C". Its other beacons (R, D, P) have
// other addresses and cannot be connected to, so the whole name, trailing letter included, must match.
// Computed once: this runs for every advertisement the ESP32 hears until the car is found.
inline bool is_car(const std::string &advertised_name, const char *vin) {
  static const std::string car_name = TeslaBLE::get_vin_advertisement_name(vin);
  return !car_name.empty() && advertised_name == car_name;
}

// 0xAABBCCDDEEFF -> "AA:BB:CC:DD:EE:FF" (what the log and the page show).
inline std::string mac_text(uint64_t mac) {
  char text[18];
  snprintf(text, sizeof(text), "%02X:%02X:%02X:%02X:%02X:%02X", (unsigned) (mac >> 40 & 0xFF),
           (unsigned) (mac >> 32 & 0xFF), (unsigned) (mac >> 24 & 0xFF), (unsigned) (mac >> 16 & 0xFF),
           (unsigned) (mac >> 8 & 0xFF), (unsigned) (mac & 0xFF));
  return text;
}

constexpr const char *SEARCHING = "Searching...";

// What the "Car" entry on the page says.
inline std::string status_text(uint64_t mac) {
  return mac == 0 ? std::string(SEARCHING) : mac_text(mac);
}

}  // namespace volture_car
