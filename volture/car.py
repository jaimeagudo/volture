"""Real car adapter: drives the Tesla through the ESP32 over the ESPHome native API.

aioesphomeapi's ReconnectLogic keeps one encrypted connection alive in the background, so
neither reading the car's state nor sending a command ever waits on a (re)connect:
`observe()` returns the last reported state, `apply()` raises at once when offline.
The ESP32 only forwards commands over BLE; they reach the car only if paired and in range.
"""
from __future__ import annotations

import logging
from typing import Optional

from aioesphomeapi import (APIClient, BinarySensorState, NumberState, ReconnectLogic, SensorState,
                           SwitchState, TextSensorState)
from zeroconf.asyncio import AsyncZeroconf

from .confirm import CarState
from .controller import Command

# object_id of the entities exposed by the yoziru/esphome-tesla-ble firmware (listed from the flashed ESP32).
# "charger" names TWO entities with the same key: a switch (charging on/off) and a binary sensor
# (cable connected), so states are told apart by type as well as key.
AMPS_ENTITY = "charging_amps"         # number: charging current setpoint (A)
CHARGER_ENTITY = "charger"            # switch: charging on/off; binary sensor: cable connected
BATTERY_ENTITY = "battery"            # sensor: state of charge (%)
LIMIT_ENTITY = "charging_limit"       # number: the car's charge limit (%)
STATUS_ENTITY = "charging"            # text: Charging, Complete, Disconnected... (logged only)
LAST_COMMAND_ENTITY = "last_command"  # text: firmware's outcome of the last BLE command (logged only)
LOGGED_TEXTS = (STATUS_ENTITY, LAST_COMMAND_ENTITY)
# State types Volture reads. Others (covers, climate, locks...) have no single `.state` value: ignored.
READ_STATES = (BinarySensorState, NumberState, SensorState, SwitchState, TextSensorState)
ESPHOME_API_PORT = 6053

log = logging.getLogger("volture.car")


class EsphomeCar:
    def __init__(self, host: str, noise_psk: str):
        self.host, self.noise_psk = host, noise_psk
        self.client: Optional[APIClient] = None  # created in start(): it needs a running event loop
        self.connected = False
        self.keys: dict[str, int] = {}
        self.states: dict[tuple[type, int], object] = {}  # (state type, key) -> value; absent = unknown
        self.reconnect: Optional[ReconnectLogic] = None
        self.zeroconf: Optional[AsyncZeroconf] = None

    async def start(self) -> None:
        # One mDNS (zeroconf) instance for the whole run. Without it, aioesphomeapi 46.3.0 (pinned by
        # esphome) adds its reconnect listener to one instance and removes it from a new one whenever
        # resolving volture.local takes > 2 s: the connection then hangs and stop() raises KeyError.
        self.zeroconf = AsyncZeroconf()
        self.client = APIClient(self.host, ESPHOME_API_PORT, None, noise_psk=self.noise_psk,
                                zeroconf_instance=self.zeroconf)
        self.reconnect = ReconnectLogic(client=self.client, on_connect=self._on_connect,
                                        on_disconnect=self._on_disconnect)
        await self.reconnect.start()

    async def stop(self) -> None:
        try:
            if self.reconnect is not None:
                await self.reconnect.stop()
            if self.client is not None:
                await self.client.disconnect(force=True)
        except Exception as e:  # shutting down anyway: never mask the reason we are stopping
            log.warning("ESP32 disconnect: %r", e)
        if self.zeroconf is not None:
            await self.zeroconf.async_close()

    async def _on_connect(self) -> None:
        try:
            entities, _services = await self.client.list_entities_services()
            self.keys = {entity.object_id: entity.key for entity in entities}
            missing = {AMPS_ENTITY, CHARGER_ENTITY} - self.keys.keys()
            if missing:
                log.error("ESP32 firmware lacks entities %s", sorted(missing))
                return
            self.client.subscribe_states(self._on_state)
            self.connected = True
            log.info("ESP32 connected")
        except Exception as e:
            # e.g. weak Wi-Fi: handshake passes, entity list is lost. ReconnectLogic retries.
            log.warning("ESP32 setup failed: %r", e)
            await self.client.disconnect(force=True)

    async def _on_disconnect(self, expected: bool) -> None:
        self.connected = False
        self.states.clear()
        log.warning("ESP32 disconnected")

    def _on_state(self, state) -> None:
        if not isinstance(state, READ_STATES):
            return
        slot = (type(state), state.key)
        if getattr(state, "missing_state", False):
            self.states.pop(slot, None)
            return
        if isinstance(state, TextSensorState) and self.states.get(slot) != state.state:
            name = next((n for n in LOGGED_TEXTS if self.keys.get(n) == state.key), None)
            if name:
                log.info("car %s: %s", name, state.state)
        self.states[slot] = state.state

    def _value(self, kind: type, entity: str):
        key = self.keys.get(entity)
        return None if key is None else self.states.get((kind, key))

    async def observe(self) -> Optional[CarState]:
        charging = self._value(SwitchState, CHARGER_ENTITY)
        amps = self._value(NumberState, AMPS_ENTITY)
        if not self.connected or charging is None or amps is None:
            return None
        plugged = self._value(BinarySensorState, CHARGER_ENTITY)
        battery = self._value(SensorState, BATTERY_ENTITY)
        limit = self._value(NumberState, LIMIT_ENTITY)
        return CarState(bool(charging), int(round(float(amps))),
                        None if plugged is None else bool(plugged),
                        None if battery is None else float(battery),
                        None if limit is None else float(limit))

    async def apply(self, cmd: Command) -> None:
        if not self.connected:
            raise ConnectionError("ESP32 not connected")
        if cmd.charging:
            # Amps first, so charging never (re)starts at a stale, higher setpoint.
            self.client.number_command(self.keys[AMPS_ENTITY], float(cmd.amps))
        self.client.switch_command(self.keys[CHARGER_ENTITY], cmd.charging)
