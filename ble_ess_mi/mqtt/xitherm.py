#!/usr/bin/env python3

"""Xiomi Thermometer BLE MQTT publisher."""

# Decoder for https://github.com/atc1441/ATC_MiThermometer
# Require Bleson https://github.com/TheCellule/python-bleson

from argparse import ArgumentParser
from json import dumps as jdumps
from logging import (DEBUG, ERROR, Formatter, Logger, NullHandler,
                     StreamHandler, getLogger)
from pprint import pprint
from socket import gethostname
from struct import unpack as sunpack, calcsize as scalc
from sys import exit as sysexit, modules, stderr
from traceback import print_exc
from typing import Iterable, Optional, Set
# workaround to prevent Bleson to setup up a logging basicConfig
Logger.root.addHandler(NullHandler())
from bleson import get_provider, logger, Observer
from paho.mqtt.client import Client, connack_string


class MqttClient(Client):

    def __init__(self, *args, **kwargs):
        self._log = getLogger('xitherm.mqtt')
        super().__init__(*args, **kwargs)
    
    def connect(self, host: str, port: int, username: Optional[str] = None,
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

    def start(self, publish: Iterable[str]):
        self._publish.update(publish)
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


def main() -> None:
    """Main routine"""
    debug = False
    try:
        module = modules[__name__]
        argparser = ArgumentParser(description=module.__doc__)

        extra = argparser.add_argument_group(title='Mqtt')
        extra.add_argument('host', nargs=1,
                           help='MQTT server')
        extra.add_argument('-p', '--port', type=int, default=1883,
                           help='MQTT port')
        extra.add_argument('-u', '--user',
                           help='MQTT username')
        extra.add_argument('-P', '--password',
                           help='MQTT password')
        extra.add_argument('-e', '--event', action='store_true',
                           default=False,
                           help='MQTT publish events (JSON payload')
        extra.add_argument('-c', '--device', action='store_true',
                           default=False,
                           help='MQTT publish device values (scalar payload)')

        extra = argparser.add_argument_group(title='Extras')
        extra.add_argument('-v', '--verbose', action='count', default=0,
                           help='Increase verbosity')
        extra.add_argument('-d', '--debug', action='store_true',
                           help='Enable debug mode')

        args = argparser.parse_args()
        debug = args.debug
        configure_logger(args.verbose, debug)
        mqtt = MqttClient()
        mqtt.connect(args.host[0], args.port, args.user, args.password)
        therm = XiaomiThermometer(mqtt)
        publish = [kind for kind in ('event', 'device') if getattr(args, kind)]
        if not publish:
            argparser.error('Nothing to publish')
        therm.start(publish)
        from time import sleep
        sleep(30)
        therm.stop()
    except (IOError, OSError, ValueError) as exc:
        print('Error: %s' % exc, file=stderr)
        if debug:
            print_exc(chain=False, file=stderr)
        sysexit(1)
    except KeyboardInterrupt:
        sysexit(2)


if __name__ == '__main__':
    main()
