"""Support for BLE Environment Sensor for Custom Mi firmware."""
import logging
from json import loads as jloads
from socket import gethostbyname, socket, timeout, AF_INET, SOCK_STREAM
from threading import Lock, Thread
from time import sleep, time as now
from typing import Any, Dict, Optional, Union
import voluptuous as vol

from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import (
    CONF_HOST,
    CONF_MAC,
    CONF_MONITORED_CONDITIONS,
    CONF_NAME,
    CONF_PORT,
    DEVICE_CLASS_BATTERY,
    DEVICE_CLASS_HUMIDITY,
    DEVICE_CLASS_TEMPERATURE,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    TEMP_CELSIUS,
    VOLT,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import Entity

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "BLE Mi"
DEFAULT_PORT = 9999
DEFAULT_HISTORY = 120.0

# Sensor types are defined like: Name, units
SENSOR_TYPES = {
    "temperature": [DEVICE_CLASS_TEMPERATURE, "Temperature", TEMP_CELSIUS],
    "humidity": [DEVICE_CLASS_HUMIDITY, "Humidity", PERCENTAGE],
    "battery": [DEVICE_CLASS_BATTERY, "Battery", PERCENTAGE],
    "battery_v": [DEVICE_CLASS_BATTERY + "_v", "Battery", VOLT],
    "rssi": ["rssi", "Rssi", SIGNAL_STRENGTH_DECIBELS_MILLIWATT],
}

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_MAC): cv.string,
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
        vol.Optional("history", default=DEFAULT_HISTORY): cv.positive_float,
        vol.Optional(CONF_MONITORED_CONDITIONS, default=list(SENSOR_TYPES)): vol.All(
            cv.ensure_list, [vol.In(SENSOR_TYPES)]
        ),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    }
)


def setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the MiTempBLE sensor."""
    _LOGGER.debug("MiTempBLE running")

    devs = []

    client = CustomMiClient(config[CONF_HOST], config[CONF_PORT], config["history"])
    mac = config.get(CONF_MAC)
    for parameter in config[CONF_MONITORED_CONDITIONS]:
        device = SENSOR_TYPES[parameter][0]
        name = SENSOR_TYPES[parameter][1]
        unit = SENSOR_TYPES[parameter][2]

        prefix = config.get(CONF_NAME)
        if prefix:
            name = f"{prefix} {name}"

        devs.append(CustomMiBleSensor(client, mac, device, name, unit))

    add_entities(devs)


class CustomMiClient:

    DEFAULT_HISTORY = 120.0
    """Default delay for discarding unseen device."""

    def __init__(
        self,
        address: str,
        port: Union[str, int],
        history: Union[float, int, None] = None,
    ):
        self._log = _LOGGER
        self._address = address
        self._port = port
        self._history = history if history != 0 else self.DEFAULT_HISTORY
        self._values: Dict[str, Dict[str, Any]] = dict()
        self._lock = Lock()
        self._thread = Thread(target=self._run, name="CustomMiTcpClient", daemon=True)
        self._resume = False
        self._thread.start()

    def stop(self):
        if not self._resume:
            self._log.warning("Not started")
            return
        self._resume = False
        self._log.debug("Waiting for completion")
        self._thread.join()
        self._log.info("Bye")

    def get_thermometer(self, mac: str) -> Optional[Dict[str, Any]]:
        mac = mac.upper()
        with self._lock:
            return self._values.get(mac, None)

    def _run(self):
        try:
            gethostbyname(self._address)
        except OSError:
            self._log.error("No such server: %s", self._address)
            return
        try:
            if not isinstance(self._port, int):
                self._port = int(self._port)
            if not 1024 <= self._port < 65536:
                raise ValueError
        except ValueError:
            self._log.error("No such server: %s", self._port)
            return
        self._resume = True
        buf = bytearray()
        next_pruning = now() + self._history / 2.0 if self._history else None
        while self._resume:
            try:
                with socket(AF_INET, SOCK_STREAM) as sock:
                    sock.connect((self._address, self._port))
                    sock.settimeout(1.0)
                    while self._resume:
                        if next_pruning and now() >= next_pruning:
                            self._prune(self._history)
                            next_pruning = now() + self._history / 2.0
                        try:
                            data = sock.recv(512)
                        except timeout as exc:
                            continue
                        if not data:
                            raise ValueError("Connection lost")
                        buf.extend(data)
                        while True:
                            parts = buf.split(b"\n", 1)
                            if len(parts) == 1:
                                break
                            packet = parts[0]
                            buf = parts[1]
                            try:
                                self._decode_packet(packet)
                            except Exception as exc:
                                self._log.error(str(exc))
                                continue
            except Exception as exc:
                self._log.error(str(exc))
                # throttle any major issue
                sleep(5)

    def _decode_packet(self, packet: bytearray):
        data = jloads(packet.decode())
        if "mac" not in data:
            return
        data["timestamp"] = now()
        with self._lock:
            self._values[data["mac"].upper()] = data

    def _prune(self, delay: float):
        ts = now()
        oldest = ts - delay
        self._log.debug("pruning unseen devices for %.1fs", delay)
        with self._lock:
            candidates = {
                mac for mac in self._values if self._values[mac]["timestamp"] < oldest
            }
            for mac in candidates:
                self._log.info(
                    "Pruning %s, not seen for %.3fs",
                    mac,
                    ts - self._values[mac]["timestamp"],
                )
                del self._values[mac]


class CustomMiBleSensor(Entity):
    """Implementing the BLE ESS for Custom Mi FW sensor."""

    def __init__(self, client, mac, device, name, unit):
        """Initialize the sensor."""
        self._client = client
        self._mac = mac
        self._device = device
        self._unit = unit
        self._name = name
        self._state = None
        self.data = []

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def unit_of_measurement(self):
        """Return the units of measurement."""
        return self._unit

    @property
    def device_class(self):
        """Device class of this entity."""
        return self._device

    @property
    def force_update(self):
        """Force update."""
        return False

    def update(self):
        device = self._client.get_thermometer(self._mac)
        if not device:
            self._state = None
            self.data = []
            _LOGGER.warning("Device %s not seen", self._mac)
            return
        value = device.get(self._device, None)
        if not value:
            self._state = None
            self.data = []
            _LOGGER.warning("Device %s no %s value", self._mac, self._device)
        else:
            self._state = value
            _LOGGER.info(
                "Device %s, parameter %s, value %s",
                self._mac,
                self._device,
                self._state,
            )
            self.data = [value]

