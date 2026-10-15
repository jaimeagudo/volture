// Line protocol around esp32/controller.h so tests/test_controller.py can run every scenario against
// the C++ version too. One request per stdin line, one reply per stdout line. "-" means None.
//
//   config <key>=<value> ...                -> ok             (before any other command)
//   tick <now> <power|-> <valid> <volts|-> <can_charge>   -> none | cmd <charging> <amps>
//                                              (power "-": no reading, like an unreachable Shelly)
//   adopt|follow_user <charging> <amps>     -> ok
//   voltage <fallback_v> <volts|->          -> <volts used>   (effective_voltage)
//   external <pending|-> <car|-> <car|->    -> 0|1
//   can_charge <car|->                      -> 0|1
//   reconcile <pending> <car|-> <now> <timeout_s> <max_attempts> -> <ACTION> <pending|->
//   step <now> <power|-> <valid> <volts|-> <car|-> <control>   -> <resend|-> <command|-> <event> <reason> <dry_run>
//                                              (Loop: one control cycle; cmd tokens are charging,amps)
// where car = charging,amps,plugged,battery,limit   pending = charging,amps,sent_at,attempts
#include <cstdio>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "../../esp32/controller.h"

using namespace volture;

namespace {

std::vector<std::string> split(const std::string &text, char sep) {
  std::vector<std::string> parts;
  std::stringstream stream(text);
  std::string part;
  while (std::getline(stream, part, sep)) {
    parts.push_back(part);
  }
  return parts;
}

std::optional<double> opt_double(const std::string &token) {
  return token == "-" ? std::nullopt : std::optional<double>(std::stod(token));
}

std::optional<bool> opt_bool(const std::string &token) {
  return token == "-" ? std::nullopt : std::optional<bool>(token == "1");
}

std::optional<CarState> parse_car(const std::string &token) {
  if (token == "-") {
    return std::nullopt;
  }
  auto f = split(token, ',');
  return CarState{f.at(0) == "1", std::stoi(f.at(1)), opt_bool(f.at(2)), opt_double(f.at(3)), opt_double(f.at(4))};
}

std::optional<Pending> parse_pending(const std::string &token) {
  if (token == "-") {
    return std::nullopt;
  }
  auto f = split(token, ',');
  return Pending{Command{f.at(0) == "1", std::stoi(f.at(1))}, std::stod(f.at(2)), std::stoi(f.at(3))};
}

void apply_config(Config &cfg, const std::string &key, const std::string &value) {
  if (key == "contracted_power_w") { cfg.contracted_power_w = std::stod(value); }
  else if (key == "headroom_w") { cfg.headroom_w = std::stod(value); }
  else if (key == "fallback_voltage_v") { cfg.fallback_voltage_v = std::stod(value); }
  else if (key == "min_amps") { cfg.min_amps = std::stoi(value); }
  else if (key == "max_amps") { cfg.max_amps = std::stoi(value); }
  else if (key == "ramp_up_step_amps") { cfg.ramp_up_step_amps = std::stoi(value); }
  else if (key == "ramp_up_every_s") { cfg.ramp_up_every_s = std::stod(value); }
  else if (key == "resume_after_s") { cfg.resume_after_s = std::stod(value); }
  else if (key == "stop_after_invalid_s") { cfg.stop_after_invalid_s = std::stod(value); }
  else { throw std::invalid_argument("unknown config key " + key); }
}

const char *action_name(Action action) {
  switch (action) {
    case Action::WAIT: return "WAIT";
    case Action::CONFIRMED: return "CONFIRMED";
    case Action::RESEND: return "RESEND";
    case Action::ADOPT: return "ADOPT";
    case Action::UNKNOWN: return "UNKNOWN";
  }
  return "?";
}

}  // namespace

int main() {
  Config cfg;
  std::optional<Controller> ctrl;
  std::optional<Loop> loop;
  std::string line;
  while (std::getline(std::cin, line)) {
    try {
      std::istringstream in(line);
      std::string op;
      in >> op;
      std::string reply = "ok";
      if (op == "config") {
        std::string pair;
        while (in >> pair) {
          auto kv = split(pair, '=');
          apply_config(cfg, kv.at(0), kv.at(1));
        }
      } else if (op == "voltage" || op == "external" || op == "can_charge" || op == "reconcile") {
        std::vector<std::string> args;
        std::string token;
        while (in >> token) {
          args.push_back(token);
        }
        if (op == "voltage") {
          Config fallback;
          fallback.fallback_voltage_v = std::stod(args.at(0));
          std::ostringstream out;
          out << effective_voltage(fallback, Reading{0.0, true, opt_double(args.at(1))});
          reply = out.str();
        } else if (op == "external") {
          reply = external_change(parse_pending(args.at(0)), parse_car(args.at(1)), parse_car(args.at(2))) ? "1" : "0";
        } else if (op == "can_charge") {
          reply = can_charge(parse_car(args.at(0))) ? "1" : "0";
        } else {
          auto [action, left] = reconcile(*parse_pending(args.at(0)), parse_car(args.at(1)), std::stod(args.at(2)),
                                          std::stod(args.at(3)), std::stoi(args.at(4)));
          std::ostringstream out;
          out << action_name(action) << " ";
          if (left) {
            out << (left->cmd.charging ? 1 : 0) << "," << left->cmd.amps << "," << left->sent_at << "," << left->attempts;
          } else {
            out << "-";
          }
          reply = out.str();
        }
      } else if (op == "step") {
        if (!loop) {
          loop.emplace(cfg);
        }
        std::string now, power, valid, volts, car, control;
        in >> now >> power >> valid >> volts >> car >> control;
        std::optional<Reading> reading;
        if (power != "-") {
          reading = Reading{std::stod(power), valid == "1", opt_double(volts)};
        }
        Cycle cycle = loop->step(std::stod(now), reading, parse_car(car), control == "1");
        auto token = [](const std::optional<Command> &cmd) {
          return cmd ? std::to_string(cmd->charging ? 1 : 0) + "," + std::to_string(cmd->amps) : std::string("-");
        };
        reply = token(cycle.resend) + " " + token(cycle.command) + " " + cycle.event + " " + loop->ctrl.reason +
                " " + (cycle.dry_run ? "1" : "0");
      } else {
        if (!ctrl) {
          ctrl.emplace(cfg);  // the controller is built on its first use, after any config line
        }
        if (op == "tick") {
          std::string now, power, valid, volts, can;
          in >> now >> power >> valid >> volts >> can;
          std::optional<Reading> reading;
          if (power != "-") {
            reading = Reading{std::stod(power), valid == "1", opt_double(volts)};
          }
          auto cmd = ctrl->tick(std::stod(now), reading, can == "1");
          reply = cmd ? "cmd " + std::to_string(cmd->charging ? 1 : 0) + " " + std::to_string(cmd->amps) : "none";
        } else if (op == "adopt" || op == "follow_user") {
          int charging_flag, amps;
          in >> charging_flag >> amps;
          if (op == "adopt") {
            ctrl->adopt(charging_flag == 1, amps);
          } else {
            ctrl->follow_user(charging_flag == 1, amps);
          }
        } else {
          reply = "error unknown op " + op;
        }
      }
      std::cout << reply << std::endl;
    } catch (const std::exception &e) {
      std::cout << "error " << e.what() << std::endl;  // the Python side turns this into a failure
    }
  }
  return 0;
}
