#!/usr/bin/env python3

"""Demo client for xitherm server."""

from argparse import ArgumentParser
from json import loads as jloads
from logging import DEBUG, ERROR, Formatter, Logger, StreamHandler, getLogger
from pprint import pprint
from socket import gethostbyname, socket, timeout, AF_INET, SOCK_STREAM
from sys import exit as sysexit, modules, stderr
from threading import Lock, Thread
from time import sleep, time as now
from traceback import print_exc
from typing import Any, Dict, List, Optional, Set, Union

class MiTempClient:

    DEFAULT_HISTORY = 120.0
    """Default delay for discarding unseen device."""

    def __init__(self, address: str, port: Union[str, int],
                 history: Union[float, int, None] = None):
        self._log = getLogger('xitherm.client')
        self._address = address
        self._port = port
        self._history = history if history != 0 else self.DEFAULT_HISTORY
        self._devices: Dict[str, Dict[str, Any]] = dict()
        self._lock = Lock()
        self._thread = Thread(target=self._run, name='MiTempClient',
                              daemon=True)
        self._resume = False
        self._thread.start()

    def stop(self):
        if not self._resume:
            self._log.warning('Not started')
            return
        self._resume = False
        self._log.debug('Waiting for completion')
        self._thread.join()
        self._log.info('Bye')

    @property
    def thermometers(self) -> Set[str]:
        return set(self._devices)

    def get_thermometer(self, mac: str) -> Optional[Dict[str, Any]]:
        mac = mac.upper()
        with self._lock:
            return self._devices.get(mac, None)

    def _run(self):
        try:
            gethostbyname(self._address)
        except OSError:
            self._log.error('No such server: %s', self._address)
            return
        try:
            if not isinstance(self._port, int):
                self._port = int(self._port)
            if not 1024 <= self._port < 65536:
                raise ValueError
        except ValueError:
            self._log.error('No such server: %s', self._port)
            return
        self._resume = True
        buf = bytearray()
        next_pruning = now() + self._history/2.0 if self._history else None
        while self._resume:
            try:
                with socket(AF_INET, SOCK_STREAM) as sock:
                    sock.connect((self._address, self._port))
                    sock.settimeout(1.0)
                    while self._resume:
                        if next_pruning and now() >= next_pruning:
                            self._prune(self._history)
                            next_pruning = now() + self._history/2.0
                        try:
                            data = sock.recv(512)
                        except timeout as exc:
                            continue
                        if not data:
                            raise ValueError('Connection lost')
                        buf.extend(data)
                        while True:
                            parts = buf.split(b'\n', 1)
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
        if 'mac' not in data:
            return
        data['timestamp'] = now()
        with self._lock:
            self._devices[data['mac'].upper()] = data

    def _prune(self, delay: float):
        ts = now()
        oldest = ts-delay
        self._log.debug('pruning unseen devices for %.1fs', delay)
        with self._lock:
            candidates = {mac for mac in self._devices
                          if self._devices[mac]['timestamp'] < oldest}
            for mac in candidates:
                self._log.info('Pruning %s, not seen for %.3fs',
                               mac, ts-self._devices[mac]['timestamp'])
                del self._devices[mac]


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


def show_temperatures(client, macs: List[str], timeout: Union[float, int]):
    log = getLogger('xitherm.show')
    if timeout:
        timeout = now() + float(timeout)
    ts = dict()
    if macs:
        fmacs = [mac.upper() for mac in macs]
    while not timeout or now() < timeout:
        if not macs:
            fmacs = client.thermometers
        devices = list(filter(None, [client.get_thermometer(mac)
                                     for mac in fmacs]))
        if not devices:
            sleep(1)
            continue
        update = False
        for device in devices:
            mac = device['mac']
            last = ts.get(mac, 0)
            if device['timestamp'] == last:
                continue
            ts[mac] = device['timestamp']
            pprint(device)
            update = True
        if not update:
            sleep(1)


def main() -> None:
    """Main routine"""
    debug = False
    try:
        module = modules[__name__]
        argparser = ArgumentParser(description=module.__doc__)

        server = argparser.add_argument_group(title='Client')
        server.add_argument('-a', '--address', required=True,
                            help='Connect to specified address')
        server.add_argument('-p', '--port', type=int, default=9999,
                            help='Connect to specified port (default: 9999)')
        server.add_argument('-t', '--timeout', type=float, default=None,
                            help='Auto stop after timeout seconds')
        server.add_argument('-m', '--mac', action='append', default=[],
                            help='Show thermometers (by BLE MAC address)')
        server.add_argument('-f', '--prune', type=float,
                            help='Set the delay before pruning unseen devices')

        extra = argparser.add_argument_group(title='Extras')
        extra.add_argument('-v', '--verbose', action='count', default=0,
                           help='Increase verbosity')
        extra.add_argument('-d', '--debug', action='store_true',
                           help='Enable debug mode')

        args = argparser.parse_args()
        debug = args.debug
        configure_logger(args.verbose, debug)
        client = MiTempClient(args.address, args.port, args.prune)
        show_temperatures(client, args.mac, args.timeout or 0)
        client.stop()

    except (IOError, OSError, ValueError) as exc:
        print('Error: %s' % exc, file=stderr)
        if debug:
            print_exc(chain=False, file=stderr)
        sysexit(1)
    except KeyboardInterrupt:
        sysexit(2)


if __name__ == '__main__':
    main()
