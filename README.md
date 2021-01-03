# Home Assistant custom components

This repository hosts very custom plugins for [Home Assistant](https://www.home-assistant.io/).

## BLE Environmental Sensor Service for Mi Thermometer

The excellent alternative firmware for [Mi Thermometer](https://github.com/atc1441/ATC_MiThermometer) advertises environmental values over BLE:
- temperature
- humidity
- battery level

This plugin integrates those data into Home Assistant.

The plugin is built around two components:
- a bridge that collects BLE advertisements and forward them as JSON data over a TCP connection
- a Home Assistant sensor integration that connects to the bridge and provides thermometer data to the Home Assistant engine

The bridge, which is not automatically started up, has been written to decouple the BLE receiver from the Home Assistant server. They may be on different hosts, and accessing the HCI device may not be always easy from within a Docker container or a non-Linux host.

### Notes

- This plugin is a very early stage, and is provided as is - my very first Home Assistant component... 
- The plugin does not follow the Home Assistant recommendation for integrating components: the HW communication component (here, the bridge) is not published as a PyPI package - I do not want to pollute PyPI with quick and dirty projects such as this one.

### Debugging

It is possible to get the bridge's output JSON stream with
```sh
nc <server> <port>
# example
nc localhost 9999
```

A python client is also provided along the bridge: `thermcli.py`.
It has been used to develop the initial CLI version of the Home Assistant plugin.

