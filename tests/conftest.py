"""Runs every test twice: against the Python logic and against its C++ port (esp32/controller.h).

The `backend` fixture gives each test the same API either way: Controller(cfg), effective_voltage,
can_charge, external_change, reconcile. The C++ side is tests/cpp/controller_cli.cpp, compiled into
.tmp/ on first use and driven over a line protocol through stdin/stdout.
Without a C++ compiler the C++ variants are skipped, so the Python suite still runs anywhere.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from volture import confirm, controller
from volture.confirm import Action, CarState, Pending
from volture.controller import Command, Config, Reading

ROOT = Path(__file__).resolve().parent.parent
CLI_SOURCES = [ROOT / "tests/cpp/controller_cli.cpp", ROOT / "esp32/controller.h"]
CLI_BINARY = ROOT / ".tmp/controller_cli"

PYTHON = SimpleNamespace(
    name="python",
    Controller=controller.Controller,
    effective_voltage=controller.effective_voltage,
    can_charge=confirm.can_charge,
    external_change=confirm.external_change,
    reconcile=confirm.reconcile,
)


def build_cli() -> Optional[Path]:
    """Compile the C++ CLI if it is missing or older than its sources. None: no compiler here."""
    compiler = shutil.which("c++")
    if compiler is None:
        return None
    newest_source = max(path.stat().st_mtime for path in CLI_SOURCES)
    if CLI_BINARY.exists() and CLI_BINARY.stat().st_mtime >= newest_source:
        return CLI_BINARY
    CLI_BINARY.parent.mkdir(exist_ok=True)
    try:
        subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-o", str(CLI_BINARY),
                        str(CLI_SOURCES[0])], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"esp32/controller.h does not compile:\n{error.stderr}") from error
    return CLI_BINARY


# ---- Serialising the Python values into the CLI's line protocol ("-" means None) ----

def flag(value: bool) -> str:
    return "1" if value else "0"


def optional(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return flag(value)
    return str(value)


def car_token(state: Optional[CarState]) -> str:
    if state is None:
        return "-"
    return ",".join([flag(state.charging), str(state.amps), optional(state.plugged),
                     optional(state.battery_pct), optional(state.limit_pct)])


def pending_token(pending: Optional[Pending]) -> str:
    if pending is None:
        return "-"
    return f"{flag(pending.cmd.charging)},{pending.cmd.amps},{pending.sent_at},{pending.attempts}"


def parse_pending(token: str) -> Optional[Pending]:
    if token == "-":
        return None
    charging, amps, sent_at, attempts = token.split(",")
    return Pending(Command(charging == "1", int(amps)), float(sent_at), int(attempts))


class CliProcess:
    """One running controller_cli: send a request line, get the reply line."""

    def __init__(self, binary: Path):
        self.process = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        text=True, bufsize=1)

    def ask(self, line: str) -> str:
        try:
            self.process.stdin.write(line + "\n")
            reply = self.process.stdout.readline().strip()
        except (BrokenPipeError, OSError) as error:
            raise RuntimeError(f"controller_cli died on: {line}") from error
        if reply == "" or reply.startswith("error"):
            raise RuntimeError(f"controller_cli: {line!r} -> {reply!r}")
        return reply

    def close(self) -> None:
        self.process.stdin.close()
        self.process.wait(timeout=5)


class CppController:
    """Same API as volture.controller.Controller, answered by esp32/controller.h."""

    def __init__(self, cli: CliProcess, cfg: Optional[Config] = None):
        self.cli = cli
        settings = " ".join(f"{field.name}={getattr(cfg, field.name)}" for field in fields(Config)) if cfg else ""
        self.cli.ask(f"config {settings}")

    def tick(self, now: float, reading: Optional[Reading], car_can_charge: bool = True) -> Optional[Command]:
        if reading is None:
            request = f"tick {now} - 0 - {flag(car_can_charge)}"
        else:
            request = f"tick {now} {reading.power_w} {flag(reading.valid)} {optional(reading.voltage_v)} {flag(car_can_charge)}"
        reply = self.cli.ask(request).split()
        if reply[0] == "none":
            return None
        return Command(reply[1] == "1", int(reply[2]))

    def adopt(self, charging: bool, amps: int) -> None:
        self.cli.ask(f"adopt {flag(charging)} {amps}")

    def follow_user(self, charging: bool, amps: int) -> None:
        self.cli.ask(f"follow_user {flag(charging)} {amps}")


def cpp_backend(binary: Path, processes: list[CliProcess]) -> SimpleNamespace:
    def spawn() -> CliProcess:
        process = CliProcess(binary)
        processes.append(process)
        return process

    shared = spawn()  # stateless functions; each Controller gets its own process (its own state)

    def reconcile(pending: Pending, observed: Optional[CarState], now: float,
                  timeout_s: float, max_attempts: int) -> tuple[Action, Optional[Pending]]:
        action, left = shared.ask(f"reconcile {pending_token(pending)} {car_token(observed)} "
                                  f"{now} {timeout_s} {max_attempts}").split()
        return Action[action], parse_pending(left)

    return SimpleNamespace(
        name="cpp",
        Controller=lambda cfg=None: CppController(spawn(), cfg),
        effective_voltage=lambda cfg, reading: float(shared.ask(
            f"voltage {cfg.fallback_voltage_v} {optional(reading.voltage_v)}")),
        can_charge=lambda state: shared.ask(f"can_charge {car_token(state)}") == "1",
        external_change=lambda pending, last, observed: shared.ask(
            f"external {pending_token(pending)} {car_token(last)} {car_token(observed)}") == "1",
        reconcile=reconcile,
    )


@pytest.fixture(params=["python", "cpp"])
def backend(request):
    if request.param == "python":
        yield PYTHON
        return
    binary = build_cli()
    if binary is None:
        pytest.skip("no C++ compiler (c++) on PATH: C++ port not tested")
    processes: list[CliProcess] = []
    yield cpp_backend(binary, processes)
    for process in processes:
        process.close()
