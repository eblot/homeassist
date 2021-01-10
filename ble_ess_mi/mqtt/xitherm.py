#!/usr/bin/env python3

"""Xiomi Thermometer BLE MQTT publisher."""

# Decoder for https://github.com/atc1441/ATC_MiThermometer
# Require Bleson https://github.com/TheCellule/python-bleson
# Require Paho-MQTT https://pypi.org/project/paho-mqtt/

from argparse import ArgumentParser, FileType
from configparser import ConfigParser
from json import dumps as jdumps
from logging import (DEBUG, ERROR, Formatter, Logger, NullHandler,
                     StreamHandler, getLogger)
from pprint import pprint
from socket import gethostname
from struct import unpack as sunpack, calcsize as scalc
from sys import exit as sysexit, modules, stderr
from time import sleep
from traceback import print_exc
from typing import Iterable, Optional, Set, Union
# workaround to prevent Bleson to setup up a logging basicConfig
Logger.root.addHandler(NullHandler())
from bleson import get_provider, logger, Observer
from paho.mqtt.client import Client, connack_string


TRUE_BOOLEANS = ['on', 'high', 'true', 'enable', 'enabled', 'yes', '1']
"""String values evaluated as true boolean values"""

FALSE_BOOLEANS = ['off', 'low', 'false', 'disable', 'disabled', 'no', '0']
"""String values evaluated as false boolean values"""


class MqttClient(Client):

    def __init__(self, *args, **kwargs):
        self._log = getLogger('xitherm.mqtt')
        super().__init__(*args, **kwargs)
    
    def connect(self, host: str, port: int,
                username: Optional[str] = None,
                password: Optional[str] = None) -> None:
        self.enable_logger(self._log)
        self._log.info('Connect to %s:%d', host, port)
        if username or password:
            self.username_pw_set(username, password)
        self.loop_start()
        super().connect(host, port)

    def disconnect(self):
        super().disconnect()
        super().loop_stop()

    def on_connect(self, client, userdata, flags, rc):
        if not rc:
            self._log.info('Connected')
        else:
            self._log.error('Cannot connect: %s', connack_string(rc))

    #def on_disconnect(self, client, userdata, rc):
    #    self._log.info('Disconnected')

    #def on_publish(self, client, userdata, mid):
    #    self._log.info('Published')


class XiaomiThermometer(Observer):

    # Byte 1-2 service (ESS)
    # Byte 3-8 mac in correct order
    # Byte 9-10 Temperature in int16  : 00 92   (deci degres)
    # Byte 11 Humidity in percent : 40
    # Byte 12 Battery in percent : 51
    # Byte 13-14 Battery in mV uint16_t: 0b 7b  2.939 (mV)
    # Byte 15 frame packet counter

    XIAOMI_OUI = 0xA4C138
    ESS = 0x181a  # Environmental Sensing Service
    SERVICE_FMT = 'H6shBBHB'

    def __init__(self, mqtt: MqttClient):
        self._log = getLogger('xitherm.ble')
        adapter = get_provider().get_adapter()
        super().__init__(adapter)
        self.on_advertising_data = self._handle_advertisement
        self._mqtt = mqtt
        self._publish: Set[str] = set()
        host = gethostname()
        self._source = f'xitherm/{host}'
        self._last_packet: Optional[int] = None

    def start(self, publish: Iterable[str], force_all_msgs: bool = False):
        self._publish.update(publish)
        self._all_msgs = force_all_msgs
        super().start()

    # --- BLESON API ---
    def _handle_advertisement(self, adv):
        data = adv.service_data
        if not data:
            return
        type_fmt = f'<{self.SERVICE_FMT[0]}'
        size = scalc(type_fmt)
        if len(data) < size:
            return
        service, = sunpack(type_fmt, data[:size])
        data = data[size:]
        if service != self.ESS:
            return
        data_fmt = f'>{self.SERVICE_FMT[1:]}'
        size = scalc(data_fmt)
        if len(data) < size:
            self._log.warning('to short')
            return
        macbytes, temp, humi, bat, batv, packet = sunpack(data_fmt, data)
        if packet == self._last_packet and not self._all_msgs:
            return
        self._last_packet = packet
        mac = sum([b << (o << 3) for o, b in enumerate(reversed(macbytes))])
        oui = mac >> 24
        if oui != self.XIAOMI_OUI:
            return
        temp = float(temp)/10.0
        batv = float(batv)/1000.0
        payload = {
             'mac': adv.address.address,
             'temperature_C': temp,
             'humidity': humi,
             'battery': bat,
             'battery_v': batv,
             'packet': packet,
             'rssi': adv.rssi,
        }
        if 'event' in self._publish:
            jstr = f'{jdumps(payload)}\n'
            jbytes = jstr.encode()
            self._mqtt.publish(f'{self._source}/events', jbytes)
        if 'device' in self._publish:
            mac = adv.address.address
            for name, val in payload.items():
                if name == 'mac':
                    continue
                self._mqtt.publish(f'{self._source}/devices/{mac}/{name}', val)


def configure_logger(verbosity: int, debug: bool) -> None:
    """Configure logger format and verbosity.

       :param verbosity: the verbosity level
       :param debug: debug mode
    """
    loglevel = max(DEBUG, ERROR - (10 * (verbosity or 0)))
    loglevel = min(ERROR, loglevel)
    if debug:
        formatter = Formatter('%(asctime)s.%(msecs)03d %(levelname)-7s '
                              '%(name)-16s %(funcName)s[%(lineno)4d] '
                              '%(message)s', '%H:%M:%S')
    else:
        formatter = Formatter('%(message)s')
    log = getLogger('xitherm')
    log.setLevel(loglevel)
    handler = StreamHandler(stderr)
    handler.setFormatter(formatter)
    log.addHandler(handler)


def to_bool(value: Union[str, int, bool]) -> bool:
    """Parse a string and convert it into a boolean value if possible.

       Input value may be:
       - a string with an integer value, if `prohibit_int` is not set
       - a boolean value
       - a string with a common boolean definition

       :param value: the value to parse and convert
       :raise ValueError: if the input value cannot be converted into an bool
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        raise ValueError("Invalid boolean value: '%d'", value)
    if value.lower() in TRUE_BOOLEANS:
        return True
    if value.lower() in FALSE_BOOLEANS:
        return False
    raise ValueError('"Invalid boolean value: "%s"' % value)


def main() -> None:
    """Main routine"""
    debug = False
    try:
        module = modules[__name__]
        argparser = ArgumentParser(description=module.__doc__)

        conf = argparser.add_argument_group(title='Config')
        conf.add_argument('-c', '--config', type=FileType('rt'),
                           help='Configuration file')
        conf.add_argument('-f', '--force', action='store_true', default=None,
                           help='Forward duplicate messages')

        extra = argparser.add_argument_group(title='Mqtt')
        extra.add_argument('host', nargs='?',
                           help='MQTT server')
        extra.add_argument('-p', '--port', type=int,
                           help='MQTT port')
        extra.add_argument('-u', '--user',
                           help='MQTT username')
        extra.add_argument('-P', '--password',
                           help='MQTT password')
        extra.add_argument('-e', '--event', action='store_true', default=None,
                           help='MQTT publish events (JSON payload')
        extra.add_argument('-D', '--device', action='store_true', default=None,
                           help='MQTT publish device values (scalar payload)')

        extra = argparser.add_argument_group(title='Extras')
        extra.add_argument('-v', '--verbose', action='count', default=0,
                           help='Increase verbosity')
        extra.add_argument('-d', '--debug', action='store_true',
                           help='Enable debug mode')

        args = argparser.parse_args()
        vargs = dict()
        if args.config:
            cfg = ConfigParser()
            cfg.read_file(args.config)
            for section in cfg.sections():
                for opt in cfg.options(section):
                    try:
                        getattr(args, opt)
                    except AttributeError as exc:
                        raise ValueError(f'Unkown config [{section}] {opt}')
                    vargs[opt] = cfg.get(section, opt)
        for opt, val in vars(args).items():
            if val is not None or opt not in vargs:
                vargs[opt] = val
        debug = vargs['debug']
        configure_logger(vargs['verbose'], debug)
        for bval in ('event', 'device', 'force'):
            vargs[bval] = to_bool(vargs[bval])
        if isinstance(vargs['host'], list):
            vargs['host'] = vargs['host'][0]
        elif vargs['host'] is None:
            argparser.error('Host is not defined')
        if isinstance(vargs['port'], str):
            try:
                vargs['port'] = int(vargs['port'])
            except ValueError as exc:
                raise ValueError ('Invalid port value') from exc
        mqtt = MqttClient()
        mqtt.connect(vargs['host'], vargs['port'],
                     vargs['user'],vargs['password'])
        therm = XiaomiThermometer(mqtt)
        publish = [kind for kind in ('event', 'device')]
        if not publish:
            argparser.error('Nothing to publish')
        therm.start(publish)
        while True:
            # loop until Ctrl-C
            sleep(0.5)
        rc = 0
    except (IOError, OSError, ValueError) as exc:
        print('Error: %s' % exc, file=stderr)
        if debug:
            print_exc(chain=False, file=stderr)
        rc = 1
    except KeyboardInterrupt:
        rc = 2
    try:
        therm.stop()
    except Exception:
        pass
    try:
        mqtt.disconnect()
    except Exception:
        pass
    sysexit(rc)


if __name__ == '__main__':
    main()
