#!/usr/bin/env python
# -*- coding: UTF-8 -*-

from __future__ import print_function
import pylcdsysinfo as plcd
import argparse, sys, configparser, time, usb.core
from collections import OrderedDict

#-------------------------------------------------------------------------------

class Icon(object):
  def __init__(self, slot, rawfile):
    self.slot = slot
    self.rawfile = rawfile

  def write_to_flash(self, lcd):
    with open(self.rawfile, 'rb') as f:
      lcd.write_image_to_flash(self.slot, f.read())

class Image(Icon):
  def __init__(self, slot, rawfile):
    super(Image, self).__init__(slot, rawfile)
    self.slot = plcd.large_image_indexes[slot]

class State(object):
  def __init__(self, image, color):
    self.image = image
    self.color = color

class Problem(object):
  def __init__(self, state, descr):
    self.state = state
    self.descr = descr

  def is_state(self, state):
    return self.state == state

  def __eq__(self, other):
    if isinstance(other, Problem):
      return self.state == other.state and self.descr == other.descr
    return NotImplemented

  def __str__(self):
    return "Problem({}, \"{}\")".format(self.state, self.descr)

#-------------------------------------------------------------------------------

class NagiosLCD(object):
  current_problems = []
  lines = [plcd.TextLines.LINE_1, plcd.TextLines.LINE_2, plcd.TextLines.LINE_3,
      plcd.TextLines.LINE_4, plcd.TextLines.LINE_5, plcd.TextLines.LINE_6]
  images = {
      'UP':      Icon(10, 'images/up.bmp'),
      'DOWN':    Icon(11, 'images/down.bmp'),
      'WARNING': Icon(12, 'images/warning.bmp'),
      'UNKNOWN': Icon(13, 'images/unknown.bmp'),
      'SPLASH':  Image(0, 'images/splash.bmp'),
  }

  states = OrderedDict()
  states['DOWN'] = states['UNREACHABLE'] = State(images['DOWN'], plcd.TextColours.RED)
  states['CRITICAL'] = states['WARNING'] = State(images['WARNING'], plcd.TextColours.YELLOW)
  states['UNKNOWN'] = State(images['UNKNOWN'], plcd.TextColours.PURPLE)
  states['UP'] = states['OK'] = State(images['UP'], plcd.TextColours.GREEN)

  def __init__(self, index = 0):
    self.lcd_index = index
    self.attach()

  def attach(self):
    self.lcd = plcd.LCDSysInfo(self.lcd_index)
    self.lcd.set_brightness(255)
    self.lcd.save_brightness(127, 255)
    self.lcd.dim_when_idle(False)
    # display splash to hide the icon that appears after the first command
    #self.display_icon(0, self.images['SPLASH'])
    self.reset_problems()

  def detach(self):
    del self.lcd

  def write_image(self, image):
    if not isinstance(image, Icon) and not isinstance(image, Image):
      raise Exception("Invalid image datatype")
    image.write_to_flash(self.lcd)

  def flash_images(self):
    for name, image in self.images.items():
      print("Flashing image '{}' (slot={})...".format(name, image.slot))
      self.write_image(image)

  def display_icon(self, pos, image):
    self.lcd.display_icon(pos, image.slot)

  def display_problem(self, line, problem):
    print_verbose("Displaying problem {} on line {}...".format(str(problem), line))
    state = self.states[problem.state]
    self.display_icon(line * 8, state.image)
    self.lcd.display_text_on_line(line + 1, problem.descr, True,
        plcd.TextAlignment.LEFT, state.color)

  def clear_lines(self, lines):
    self.lcd.clear_lines(lines, plcd.BackgroundColours.BLACK)

  def reset_problems(self):
    self.current_problems = []

  def display_all_up(self):
    print_verbose("No more problems. Displaying splash")
    self.lcd.clear_lines(plcd.TextLines.ALL, plcd.BackgroundColours.LIGHT_GREY)
    self.display_icon(0, self.images['SPLASH'])
    self.lcd.display_text_on_line(5, "ALL UP", False, plcd.TextAlignment.CENTRE,
        plcd.TextColours.GREEN)
    self.reset_problems()

  def display_problems(self, problems):
    # display splash
    if len(problems) == 0:
      self.display_all_up()
      return

    # sort problems by defined states
    new_problems = []
    for state in self.states.items():
      for problem in problems:
        if not problem.is_state(state[0]):
          continue
        new_problems.append(problem)

    # clear splash
    if len(self.current_problems) == 0:
      self.clear_lines(plcd.TextLines.ALL)

    # display new problems only
    for i in range(len(new_problems)):
      new_problem = new_problems[i]
      if len(self.current_problems) > i and new_problem == self.current_problems[i]:
        continue
      self.display_problem(i, new_problem)

    # clear old problems
    clear_lines = 0
    for i in range(len(new_problems), len(self.current_problems)):
      clear_lines |= self.lines[i]
    if clear_lines > 0:
      self.clear_lines(clear_lines)

    self.current_problems = new_problems

#-------------------------------------------------------------------------------

class Fetcher(object):
  def __init__(self, config):
    self.sleep = config.getint('sleep', 60)

  def fetch(self):
    raise NotImplementedError

  def _skip(self, d):
    #if d['in_scheduled_downtime']:
    #  return True
    #if d['is_flapping']:
    #  return True
    #if d['notifications_enabled']:
    #  return True
    #if d['has_been_acknowledged']:
    #  return True
    if d['state_type'] == 'SOFT':
      (attempt, max_attempts) = d['attempts'].split('/')
      if int(attempt) == 1 and int(max_attempts) > 1:
        return True
    return False

  def parse(self, data):
    problems = []
    for d in data['host_status']:
      if self._skip(d):
        continue
      problems.append(Problem(d['status'], d['host_display_name']))

    for d in data['service_status']:
      if self._skip(d):
        continue
      problems.append(Problem(d['status'],
        "{} {}".format(d['host_display_name'], d['service_display_name'])))
    return problems

  def do_sleep(self):
    time.sleep(self.sleep)

class JSONFile_Fetcher(Fetcher):
  def __init__(self, config):
    global json
    import json

    super(JSONFile_Fetcher, self).__init__(config)
    self.file = config.get('file', None)
    if self.file is None:
      raise RuntimeError("Invalid file")

  def fetch(self):
    print_verbose("Fetching data from {}...".format(self.file))
    return json.load(open(self.file))

class HTTP_Fetcher(Fetcher):
  def __init__(self, config):
    global urllib, json
    import urllib.request, urllib.error, json, socket

    super(HTTP_Fetcher, self).__init__(config)
    self.url = config.get('url', None)
    if self.url is None:
      raise RuntimeError("Invalid http url")

    if config.get('username', None) and config.get('password', None):
      passman = urllib.request.HTTPPasswordMgrWithDefaultRealm()
      passman.add_password(None, self.url, config.get('username'), config.get('password'))
      opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(passman))
      urllib.request.install_opener(opener)

    socket.setdefaulttimeout(config.getint('timeout', 10))

  def fetch(self):
    try:
      print_verbose("Fetching data from {}...".format(self.url))
      r = urllib.request.urlopen(self.url)
    except urllib.error.HTTPError as e:
      if e.code >= 400:
        raise RuntimeError(e)
      return None
    data = r.read().decode(r.headers.get_param('charset', 'utf-8'))
    return json.loads(data)['status']

class MYSQL_Fetcher(Fetcher):
  def __init__(self, config):
    global MySQLdb, sqlex
    import MySQLdb, MySQLdb.cursors, _mysql_exceptions as sqlex

    super(MYSQL_Fetcher, self).__init__(config)
    kwargs = eval("dict({})".format(config.get('dsn', '')))
    kwargs['cursorclass'] = MySQLdb.cursors.DictCursor
    try:
      self.db = MySQLdb.connect(**kwargs)
    except sqlex.Error as e:
      raise RuntimeError(e)

  def fetch(self):
    data = {
        'host_status': [],
        'service_status': [],
    }

    try:
      print_verbose("Fetching data from db...")
      cursor = self.db.cursor()
      cursor.execute("""
        SELECT
          h.display_name as host_display_name,
          IF(hs.current_state = 0, 'UP', IF(hs.current_state = 1, 'DOWN', 'UNREACHABLE')) as status,
          IF(hs.state_type = 0, 'SOFT', 'HARD') as state_type,
          CONCAT(hs.current_check_attempt, '/', hs.max_check_attempts) as attempts,
          hs.scheduled_downtime_depth > 0 as in_scheduled_downtime,
          hs.is_flapping,
          hs.notifications_enabled,
          hs.problem_has_been_acknowledged as has_been_acknowledged
        FROM icinga_hoststatus AS hs
          JOIN icinga_instances as i USING (instance_id)
          JOIN icinga_objects AS obj ON (obj.object_id = hs.host_object_id)
          JOIN icinga_hosts AS h USING (host_object_id)
        HAVING status != 'UP'
      """)
      for row in cursor:
        if self._skip(row):
          continue
        data.setdefault('host_status', []).append(row)
      cursor.close()

      cursor = self.db.cursor()
      cursor.execute("""
        SELECT
          s.display_name as service_display_name,
          h.display_name as host_display_name,
          IF(ss.current_state = 0, 'OK', IF(ss.current_state = 1, 'WARNING', IF(ss.current_state = 2, 'CRITICAL', 'UNKNOWN'))) as status,
          IF(ss.state_type = 0, 'SOFT', 'HARD') as state_type,
          CONCAT(ss.current_check_attempt, '/', ss.max_check_attempts) as attempts,
          ss.scheduled_downtime_depth > 0 as in_scheduled_downtime,
          ss.is_flapping,
          ss.notifications_enabled,
          ss.problem_has_been_acknowledged as has_been_acknowledged
        FROM icinga_servicestatus AS ss
          JOIN icinga_instances as i USING (instance_id)
          JOIN icinga_objects AS obj ON (obj.object_id = ss.service_object_id)
          JOIN icinga_services AS s USING (service_object_id)
          JOIN icinga_hosts AS h USING (host_object_id)
        HAVING status != 'OK'
      """)
      for row in cursor:
        if self._skip(row):
          continue
        data.setdefault('service_status', []).append(row)
      cursor.close()
    except sqlex.Error as e:
      raise RuntimeError(e)
    return data

#-------------------------------------------------------------------------------

def wait_for_lcd_attach(lcd):
  sleep = 60
  print("LCD removed from usb. Trying to reattach every {} seconds".format(sleep),
      file=sys.stderr)
  while True:
    try:
      lcd.attach()
      print("LCD attached again", file=sys.stderr)
      return True
    except IOError as e:
      pass
    time.sleep(sleep)

def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('-v', '--verbose', action='store_true', help='be verbose')
  parser.add_argument('-f', '--file', metavar='cfgfile', default="nagios-lcd.conf", help='path to config file')
  parser.add_argument('-p', '--protocol', metavar='protocol', help='protocol to use')
  parser.add_argument('flash_images', nargs='?', choices=['flash_images'], help='write images to lcd flash')
  args = parser.parse_args()

  global print_verbose
  print_verbose = print if args.verbose else lambda *a, **k: None

  try:
    config = configparser.ConfigParser()
    config['LCD'] = {}
    config['HTTP'] = {}
    config.read(args.file)
  except Exception as e:
    print("Error while parsing configuration file: " + str(e), file=sys.stderr)
    sys.exit(1)

  try:
    lcd = NagiosLCD(config['LCD'].getint('index', 0))
  except IOError as e:
    print("Error: " + str(e), file=sys.stderr)
    sys.exit(1)

  if args.flash_images:
    try:
      print("Flashing images to devices...");
      lcd.flash_images()
      print("Flashing done")
    except IOError as e:
      print("Error: " + str(e), file=sys.stderr)
    sys.exit(0)

  fetchers = {
    'HTTP':  HTTP_Fetcher,
    'MYSQL': MYSQL_Fetcher,
    'JSONFILE': JSONFile_Fetcher
  }
  protocol = args.protocol if args.protocol else config['LCD'].get('protocol', 'HTTP')
  print_verbose("Protocol is set to {}".format(protocol))
  try:
    config = config[protocol] if config.has_section(protocol) else {}
    fetcher = fetchers[protocol](config)
  except KeyError as e:
    print("Invalid or unsupported protocol: " + str(e), file=sys.stderr)
    sys.exit(0)
  except RuntimeError as e:
    print("Unable to initialize protocol: " + str(e), file=sys.stderr)
    sys.exit(0)

  while True:
    try:
      problems = fetcher.parse(fetcher.fetch())
      lcd.display_problems(problems)
    except NotImplementedError:
      raise
    except (ValueError, RuntimeError) as e:
      print("Error: " + str(e), file=sys.stderr)
    except usb.core.USBError as e:
      print("USB Error: " + str(e), file=sys.stderr)
      lcd.detach()
      wait_for_lcd_attach(lcd)
    fetcher.do_sleep()

if __name__ == '__main__':
  main()
