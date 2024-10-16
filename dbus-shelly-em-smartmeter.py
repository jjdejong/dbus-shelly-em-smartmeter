#!/usr/bin/env python

# import normal packages
from gi.repository import GLib
import platform
import logging
import logging.handlers
import sys
import os
import time
import requests # for http GET
import configparser # for config/ini file

# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService

class DbusShellyemService:
  def __init__(self, servicename, paths, productname='Shelly EM', connection='Shelly HTTP JSON service'):
    config = self._getConfig()
    deviceinstance = int(config['DEFAULT'].get('Deviceinstance', 40))
    self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance), register=False)
    self._paths = paths

    logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))

    # Create the management objects, as specified in the ccgx dbus-api document
    self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
    self._dbusservice.add_path('/Mgmt/ProcessVersion', 'Unknown version, and running on Python ' + platform.python_version())
    self._dbusservice.add_path('/Mgmt/Connection', connection)

    # Create the generic objects
    self._dbusservice.add_path('/DeviceInstance', deviceinstance)
    self._dbusservice.add_path('/ProductId', 0xB034) # id needs to be assigned by Victron Support current value for testing
    self._dbusservice.add_path('/ProductName', productname)
    self._dbusservice.add_path('/CustomName', config['DEFAULT'].get('CustomName', ''))
    self._dbusservice.add_path('/FirmwareVersion', 0.1)
    self._dbusservice.add_path('/HardwareVersion', 0)
    self._dbusservice.add_path('/Serial', self._getShellySerial())
    self._dbusservice.add_path('/Connected', 1)
    self._dbusservice.add_path('/Role', 'pvinverter')
    
    # Create device specific objects
    self._dbusservice.add_path('/Position', int(config['DEFAULT'].get('Position', 0))) # normally only needed for pvinverter
    self._dbusservice.add_path('/Ac/MaxPower', float(config['DEFAULT'].get('MaxPower', 0.0))) # only needed for pvinverter

    # add path values to dbus
    for path, settings in self._paths.items():
      self._dbusservice.add_path(
        path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handlechangedvalue)

    # Register the service after adding all paths
    self._dbusservice.register()
    
    # last update
    self._lastUpdate = 0

    # add _update function 'timer'
    GLib.timeout_add(5000, self._update) # pause in ms before the next request

    # add _signOfLife 'timer' to get feedback in log every 5 minutes
    GLib.timeout_add(self._getSignOfLifeInterval() * 60 * 1000, self._signOfLife)

  def _getShellySerial(self):
    meter_data = self._getShellyData()
    serial = meter_data.get('mac', meter_data['sys'].get('mac'))
    if not serial:
      raise ValueError("Response does not contain 'mac' attribute")
    return serial

  def _getConfig(self):
    config = configparser.ConfigParser()
    config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
    return config

  def _getSignOfLifeInterval(self):
    config = self._getConfig()
    value = config['DEFAULT'].get('SignOfLifeLog', 0)
    return int(value)

  def _getShellyStatusUrl(self):
    config = self._getConfig()
    accessType = config['DEFAULT'].get('AccessType', 'OnPremise')

    if accessType == 'OnPremise':
      URL = "http://%s:%s@%s/" % (config['ONPREMISE'].get('Username', ''), config['ONPREMISE'].get('Password', ''), config['ONPREMISE'].get('Host', 'localhost'))
      if config['DEVICE'].get('Gen', 1) == 1:
        URL = URL + 'status'
      else:
        URL = URL + 'rpc/Shelly.GetStatus'
      URL = URL.replace(":@", "")
    else:
      raise ValueError("AccessType %s is not supported" % (config['DEFAULT'].get('AccessType')))
    return URL

  def _getShellyData(self):
    URL = self._getShellyStatusUrl()
    meter_r = requests.get(url=URL)
    # check for response
    if not meter_r:
      raise ConnectionError("No response from Shelly EM - %s" % (URL))
    meter_data = meter_r.json()
    # check for Json
    if not meter_data:
      raise ValueError("Converting response to JSON failed")
    return meter_data

  def _signOfLife(self):
    # logging.info("--- Start: sign of life ---")
    # logging.info("Last _update() call: %s" % (self._lastUpdate))
    # logging.info("Last '/Ac/Power': %s" % (self._dbusservice['/Ac/Power']))
    # logging.info("--- End: sign of life ---")
    return True

  def _update(self):
    try:
      # get data from Shelly em
      meter_data = self._getShellyData()

      config = self._getConfig()
      MeterNo = int(config['DEFAULT'].get('MeterNo', 0))

      if config['DEVICE'].get('Gen', 1) == 1:
        voltage = meter_data['emeters'][MeterNo]['voltage']
        power = meter_data['emeters'][MeterNo]['power']
        current = power / voltage
        energy_fwd = meter_data['emeters'][MeterNo]['total'] / 1000
        energy_ret = meter_data['emeters'][MeterNo]['total_returned'] / 1000
      else:
        # Detect PM or 1PM device
        d = meter_data.get('pm1:0', meter_data.get('switch:0', None))
        if d:
          voltage = d['voltage']
          current = d['current']
          power = d['apower']
          energy_fwd = d['aenergy'].get('total') / 1000
          energy_ret = d.get('ret_aenergy', d['aenergy']).get('total') / 1000

      # send data to DBus
      self._dbusservice['/Ac/L1/Voltage'] = voltage
      if config['DEFAULT'].get('GridOrPV') == 'grid':
        self._dbusservice['/Ac/L1/Energy/Forward'] = energy_fwd
        self._dbusservice['/Ac/L1/Energy/Reverse'] = energy_ret
      else: # pvinverter, implies CT is connected towards the PV inverter as a load
        current = -current
        power = -power
        self._dbusservice['/Ac/L1/Energy/Forward'] = energy_ret
        self._dbusservice['/Ac/L1/Energy/Reverse'] = energy_fwd

      self._dbusservice['/Ac/L1/Current'] = current
      self._dbusservice['/Ac/Power'] = self._dbusservice['/Ac/L1/Power'] = power
      self._dbusservice['/Ac/Energy/Forward'] = self._dbusservice['/Ac/L1/Energy/Forward']
      self._dbusservice['/Ac/Energy/Reverse'] = self._dbusservice['/Ac/L1/Energy/Reverse']

      # logging
      logging.debug("Consumption (/Ac/Power): %s" % (self._dbusservice['/Ac/Power']))
      logging.debug("Forward (/Ac/Energy/Forward): %s" % (self._dbusservice['/Ac/Energy/Forward']))
      logging.debug("Reverse (/Ac/Energy/Reverse): %s" % (self._dbusservice['/Ac/Energy/Reverse']))
      logging.debug("---")

      # update lastupdate vars
      self._lastUpdate = time.time()
    except Exception as e:
      logging.critical('Error at %s', '_update', exc_info=e)

    # return true, otherwise add_timeout will be removed from GObject - see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
    return True

  def _handlechangedvalue(self, path, value):
    logging.debug("someone else updated %s to %s" % (path, value))
    return True # accept the change

def getServiceConfig():
    config = configparser.ConfigParser()
    config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
    GridOrPV = config['DEFAULT'].get('GridOrPV', 'grid')
    return GridOrPV

def main():
  # configure logging
  log_file = "%s/current.log" % os.path.dirname(os.path.realpath(__file__))

  logging.basicConfig(
      format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
      datefmt='%Y-%m-%d %H:%M:%S',
      level=logging.INFO,  # Set to DEBUG for more detailed logs
      handlers=[
          logging.handlers.TimedRotatingFileHandler(
              log_file,
              when='midnight',
              backupCount=30
          ),
          logging.StreamHandler()
      ]
  )

  try:
    logging.info("Start")

    from dbus.mainloop.glib import DBusGMainLoop
    # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
    DBusGMainLoop(set_as_default=True)

    # formatting
    _kwh = lambda p, v: (str(round(v, 2)) + 'KWh')
    _a = lambda p, v: (str(round(v, 1)) + 'A')
    _w = lambda p, v: (str(round(v, 1)) + 'W')
    _v = lambda p, v: (str(round(v, 1)) + 'V')

    # start our main-service
    GridOrPV = getServiceConfig()
    pvac_output = DbusShellyemService(
      servicename='com.victronenergy.' + GridOrPV, # grid or pvinverter
      paths={
        '/Ac/Energy/Forward': {'initial': None, 'textformat': _kwh}, # energy bought from the grid
        '/Ac/Energy/Reverse': {'initial': None, 'textformat': _kwh}, # energy sold to the grid
        '/Ac/Power': {'initial': 0, 'textformat': _w},
        '/Ac/L1/Voltage': {'initial': 0, 'textformat': _v},
        '/Ac/L1/Current': {'initial': 0, 'textformat': _a},
        '/Ac/L1/Power': {'initial': 0, 'textformat': _w},
        '/Ac/L1/Energy/Forward': {'initial': None, 'textformat': _kwh},
        '/Ac/L1/Energy/Reverse': {'initial': None, 'textformat': _kwh},
      })

    logging.info('Connected to dbus, and switching over to GLib.MainLoop() (= event based)')
    mainloop = GLib.MainLoop()
    mainloop.run()
  except Exception as e:
    logging.critical('Error at %s', 'main', exc_info=e)
    
if __name__ == "__main__":
  main()
