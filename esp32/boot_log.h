#pragma once
// The boot list in Grafana: one line per ESP32 boot, so restarts can be listed and matched with what the
// car and the house did, without a computer reading the device's log at the time.
//   volture_boot_at{reason, crash}: the boot's unix time (seconds).
//   reason: the "Reset reason" reading (power-on, software reset, panic, watchdog, brownout...).
//   crash: after a crash, its reason and the code addresses ESPHome's crash handler kept, to decode with
//          `addr2line -pfiaC -e firmware.elf <addresses>` and the firmware.elf of the build that crashed;
//          "none" otherwise. The handler keeps the record until an API client (`esphome logs`) has
//          received it, so a later reboot without one in between lists the same crash again.

#include <cstring>
#include <string>

#include "device.h"  // volture_device::tag
#ifdef USE_ESP32_CRASH_HANDLER
#include "esphome/components/esp32/crash_handler.h"
#endif

namespace volture_boot {

// Where the crash handler's log lines are collected while crash_summary() asks it to log them.
inline std::string *&capture() {
  static std::string *target = nullptr;
  return target;
}

// Every ERROR log line (esp32.yaml, logger: on_message). Keeps, from the crash handler's lines, the reason
// ("Reason: Abort") and the addresses ("Use: addr2line -pfiaC -e firmware.elf 0x4008EC6C ..."). The logger
// hands the main loop's lines over synchronously, so only crash_summary()'s own call lands here.
inline void on_error_log(const char *log_tag, const char *message) {
  if (capture() == nullptr || log_tag == nullptr || message == nullptr || std::strcmp(log_tag, "esp32.crash") != 0) {
    return;
  }
  const std::string line(message);
  for (const char *marker : {"Reason: ", "addr2line -pfiaC -e firmware.elf "}) {
    const size_t found = line.find(marker);
    if (found == std::string::npos) {
      continue;
    }
    const size_t start = found + std::strlen(marker);
    const size_t colour = line.find('\033', start);  // the log line ends with a colour reset
    capture()->append(capture()->empty() ? "" : " | ");
    capture()->append(line.substr(start, colour == std::string::npos ? std::string::npos : colour - start));
  }
}

// The previous boot's crash, reduced to its reason and addresses; empty when there was none.
inline std::string crash_summary() {
  std::string summary;
#ifdef USE_ESP32_CRASH_HANDLER
  if (!esphome::esp32::crash_handler_has_data()) {
    return summary;
  }
  capture() = &summary;
  esphome::esp32::crash_handler_log();
  capture() = nullptr;
#endif
  return summary;
}

// The boot's line, stamped with the boot's own time: unix time now minus the time since boot.
inline std::string boot_line(const std::string &reason, const std::string &crash, long long boot_unix_us) {
  return "volture_boot,reason=" + volture_device::tag(reason) + ",crash=" + volture_device::tag(crash) +
         " at=" + std::to_string(boot_unix_us / 1000000LL) + " " + std::to_string(boot_unix_us * 1000LL) + "\n";
}

}  // namespace volture_boot
