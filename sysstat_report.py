#!/usr/bin/env python3
# -*- coding: utf-8 -*-

""" Generate and send a sysstat mail report. """

import argparse
import bz2
import calendar
import contextlib
import datetime
import email.mime.image
import email.mime.multipart
import email.mime.text
import email.utils
import enum
import inspect
import itertools
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time


ReportType = enum.Enum("ReportType", ("DAILY", "WEEKLY", "MONTHLY"))
SysstatDataType = enum.Enum("SysstatDataType", ("CPU", "MEM", "SWAP", "NET", "IO"))

HAS_OPTIPNG = shutil.which("optipng") is not None


def get_total_memory_mb():
  """ Return total amount of system RAM in MB. """
  output = subprocess.check_output(("free", "-m"), universal_newlines=True)
  output = output.splitlines()
  mem_line = next(itertools.dropwhile(lambda x: not x.startswith("Mem:"), output))
  total_mem = int(tuple(filter(None, map(str.strip, mem_line.split(" "))))[1])
  logging.getLogger().info("Total amount of memory: %u MB" % (total_mem))
  return total_mem


def get_max_network_speed():
  """ Get maximum Ethernet network interface speed in Mb/s. """
  max_speed = -1
  interfaces = os.listdir("/sys/class/net")
  assert(len(interfaces) > 1)
  for interface in interfaces:
    if interface == "lo":
      continue
    filepath = "/sys/class/net/%s/speed" % (interface)
    with open(filepath, "rt") as f:
      new_speed = int(f.read())
    logging.getLogger().debug("Speed of interface %s: %u Mb/s" % (interface, new_speed))
    max_speed = max(max_speed, new_speed)
  logging.getLogger().info("Maximum interface speed: %u Mb/s" % (max_speed))
  return max_speed


def get_reboot_times():
  """ Return a list of datetime.datetime representing machine reboot times. """
  reboot_times = []
  for i in range(1, -1, -1):
    log_filepath = "/var/log/wtmp%s" % (".%u" % (i) if i != 0 else "")
    if os.path.isfile(log_filepath):
      cmd = ("last", "-R", "reboot", "-f", log_filepath)
      output = subprocess.check_output(cmd, universal_newlines=True)
      output = output.splitlines()[0:-2]
      date_regex = re.compile(".*boot\s*(.*) - .*$")
      for l in output:
        date_str = date_regex.match(l).group(1).strip()
        # TODO remove fixed year
        date = datetime.datetime.strptime(date_str + " %u" % (datetime.date.today().year), "%a %b %d %H:%M %Y")
        reboot_times.append(date)
  return reboot_times


def format_email(exp, dest, subject, header_text, img_filepaths, alternate_text_filepaths):
  """ Format a MIME email with attached images and alternate text, and return email code. """
  msg = email.mime.multipart.MIMEMultipart("related")
  msg["Subject"] = subject
  msg["From"] = exp
  msg["To"] = dest

  # html
  html = "<html><head></head><body>"
  if header_text is not None:
    html += "<pre>%s</pre><br>" % (header_text)
  html += "<br>".join("<img src=\"cid:img%u\">" % (i) for i in range(len(img_filepaths)))
  html = email.mime.text.MIMEText(html, "html")

  # alternate text
  alternate_texts = []
  for alternate_text_filepath in alternate_text_filepaths:
    with open(alternate_text_filepath, "rt") as alternate_text_file:
      alternate_texts.append(alternate_text_file.read())
  if header_text is not None:
    text = "%s\n" % (header_text)
  else:
    text = ""
  text += "\n".join(alternate_texts)
  text = email.mime.text.MIMEText(text)

  msg_alt = email.mime.multipart.MIMEMultipart("alternative")
  msg_alt.attach(text)
  msg_alt.attach(html)
  msg.attach(msg_alt)

  for i, img_filepath in enumerate(img_filepaths):
    with open(img_filepath, "rb") as img_file:
      msg_img = email.mime.image.MIMEImage(img_file.read())
    msg_img.add_header("Content-ID", "<img%u>" % (i))
    msg.attach(msg_img)

  return msg.as_string()


def bz_decompress(bz2_filepath, new_filepath):
  logging.getLogger().debug("Decompressing '%s' to '%s'..." % (bz2_filepath, new_filepath))
  with bz2.open(bz2_filepath, "rb") as bz2_file, \
          open(new_filepath, "wb") as new_file:
    shutil.copyfileobj(bz2_file, new_file)


class SysstatData:

  """ Source of system stats. """

  def __init__(self, report_type, temp_dir):
    assert(report_type in ReportType)
    self.sa_filepaths = []
    today = datetime.date.today()

    if report_type is ReportType.DAILY:
      date = today - datetime.timedelta(days=1)
      self.sa_filepaths.append("/var/log/sysstat/sa%02u" % (date.day))

    elif report_type is ReportType.WEEKLY:
      for i in range(7, 0, -1):
        date = today - datetime.timedelta(days=i)
        filepath = date.strftime("/var/log/sysstat/%Y%m/sa%d")
        if not os.path.isfile(filepath):
          bz2_filepath = "%s.bz2" % (filepath)
          filepath = os.path.join(temp_dir, os.path.basename(filepath))
          bz_decompress(bz2_filepath, filepath)
        self.sa_filepaths.append(filepath)

    elif report_type is ReportType.MONTHLY:
      if today.month == 1:
        year = today.year - 1
        month = 12
      else:
        year = today.year
        month = today.month - 1
      for day in range(1, calendar.monthrange(year, month)[1] + 1):
        filepath = "/var/log/sysstat/%04u%02u/sa%02u" % (year, month, day)
        if not os.path.isfile(filepath):
          bz2_filepath = "%s.bz2" % (filepath)
          filepath = os.path.join(temp_dir, os.path.basename(filepath))
          bz_decompress(bz2_filepath, filepath)
        self.sa_filepaths.append(filepath)

  def generateData(self, dtype, output_filepath):
    """
    Generate data to plot (';' separated values).

    Return indexes of columns to use in output, and a dictionary of name -> filepath output datafiles if the provided
    output file had to be split.
    """
    assert(dtype in SysstatDataType)
    net_output_filepaths = {}

    for sa_filepath in self.sa_filepaths:
      cmd = ["sadf", "-d", "-U", "--"]
      if dtype is SysstatDataType.CPU:
        cmd.append("-u")
      elif dtype is SysstatDataType.MEM:
        cmd.append("-r")
      elif dtype is SysstatDataType.SWAP:
        cmd.append("-S")
      elif dtype is SysstatDataType.NET:
        cmd.extend(("-n", "DEV"))
      elif dtype is SysstatDataType.IO:
        cmd.append("-b")
      cmd.append(sa_filepath)
      with open(output_filepath, "ab") as output_file:
        subprocess.check_call(cmd, stdout=output_file)

    if dtype is SysstatDataType.NET:
      # split file by interface
      with open(output_filepath, "rt") as output_file:
        next(output_file)  # skip first line
        for line in output_file:
          itf = line.split(";", 5)[3]
          if itf in net_output_filepaths:
            # not a new interface
            break
          base_filename, ext = os.path.splitext(output_filepath)
          net_output_filepaths[itf] = "%s_%s%s" % (base_filename, itf, ext)
      logging.getLogger().debug("Found %u network interfaces: %s" % (len(net_output_filepaths), ", ".join(net_output_filepaths)))
      with contextlib.ExitStack() as ctx:
        itf_files = {}
        for itf, itf_filepath in net_output_filepaths.items():
          itf_files[itf] = ctx.enter_context(open(itf_filepath, "wt"))
        with open(output_filepath, "rt") as output_file:
          for line in output_file:
            itf = line.split(";", 5)[3]
            if itf in itf_files:
              itf_files[itf].write(line)

    if dtype is SysstatDataType.CPU:
      # hostname;interval;timestamp;CPU;%user;%nice;%system;%iowait;%steal;%idle
      indexes = (3, 5, 6, 7, 8, 9, 10)
    elif dtype is SysstatDataType.MEM:
      # hostname;interval;timestamp;kbmemfree;kbmemused;%memused;kbbuffers;kbcached;kbcommit;%commit;kbactive;kbinact;kbdirty
      indexes = (3, 5, 7, 8, 9, 11, 13)
    elif dtype is SysstatDataType.SWAP:
      # hostname;interval;timestamp;kbswpfree;kbswpused;%swpused;kbswpcad;%swpcad
      indexes = (3, 6)
    elif dtype is SysstatDataType.NET:
      # hostname;interval;timestamp;IFACE;rxpck/s;txpck/s;rxkB/s;txkB/s;rxcmp/s;txcmp/s;rxmcst/s;%ifutil
      indexes = (3, 7, 8)
    elif dtype is SysstatDataType.IO:
      # hostname;interval;timestamp;tps;rtps;wtps;bread/s;bwrtn/s
      indexes = (3, 7, 8)

    return indexes, net_output_filepaths


class Plotter:

  """ Class to plot with GNU Plot. """

  def __init__(self, report_type):
    assert(report_type in ReportType)
    self.report_type = report_type

  def plotToPng(self, data_filepaths, data_indexes, data_type, reboot_times, output_filepath, smooth, *, title,
                data_titles, ylabel, yrange):
    self.__plot(data_filepaths, data_indexes, data_type, reboot_times, output_filepath, smooth, False, title,
                data_titles, ylabel, yrange)

  def plotToText(self, data_filepaths, data_indexes, data_type, reboot_times, output_filepath, smooth, *, title,
                 data_titles, ylabel, yrange):
    self.__plot(data_filepaths, data_indexes, data_type, reboot_times, output_filepath, smooth, True, title,
                data_titles, ylabel, yrange)

  def __plot(self, data_filepaths, data_indexes, data_type, reboot_times, output_filepath, smooth, text, title,
             data_titles, ylabel, yrange):
    gnuplot_code = []

    # output setup
    if text:
      gnuplot_code.extend(("set terminal dumb 110,25",
                           "set output '%s'" % (output_filepath)))
    else:
      gnuplot_code.extend(("set terminal png size 780,400 font 'Liberation,9'",
                           "set output '%s'" % (output_filepath)))

    # input data setup
    gnuplot_code.extend(("set timefmt '%s'",
                         "set datafile separator ';'"))

    # title
    gnuplot_code.append("set title '%s'" % (title))

    # caption
    gnuplot_code.append("set key outside right samplen 3 spacing 1.75")

    # x axis setup
    gnuplot_code.extend(("set xdata time",
                         "set xlabel 'Time'"))
    if self.report_type is ReportType.MONTHLY:
      gnuplot_code.append("set xtics %u" % (60 * 60 * 24 * 2))  # 2 days
    now = datetime.datetime.now()
    if self.report_type is ReportType.DAILY:
      date_to = datetime.datetime(now.year, now.month, now.day)
      date_from = date_to - datetime.timedelta(days=1)
      format_x = "%R"
    elif self.report_type is ReportType.WEEKLY:
      date_to = datetime.datetime(now.year, now.month, now.day)
      date_from = date_to - datetime.timedelta(weeks=1)
      format_x = "%a %d/%m"
    elif self.report_type is ReportType.MONTHLY:
      today = datetime.date.today()
      if today.month == 1:
        year = today.year - 1
        month = 12
      else:
        year = today.year
        month = today.month - 1
      date_from = datetime.datetime(year, month, 1)
      date_to = datetime.datetime(year, month, calendar.monthrange(year, month)[1])
      format_x = "%d"
    date_from = date_from + datetime.timedelta(seconds=time.localtime().tm_gmtoff)
    date_to = date_to + datetime.timedelta(seconds=time.localtime().tm_gmtoff)
    gnuplot_code.append("set xrange[\"%s\":\"%s\"]" % (date_from.strftime("%s"), date_to.strftime("%s")))
    gnuplot_code.append("set format x '%s'" % (format_x))

    # y axis setup
    gnuplot_code.append("set ylabel '%s'" % (ylabel))
    if yrange is not None:
      yrange = list(str(r) if r is not None else "*" for r in yrange)
      gnuplot_code.append("set yrange [%s:%s]" % (yrange[0], yrange[1]))

    # reboot lines
    for reboot_time in reboot_times:
      reboot_time = reboot_time + datetime.timedelta(seconds=time.localtime().tm_gmtoff)
      if date_from <= reboot_time <= date_to:
        gnuplot_code.append("set arrow from \"%s\",graph 0 to \"%s\",graph 1 lt 0 nohead" % (reboot_time.strftime("%s"),
                                                                                             reboot_time.strftime("%s")))

    # plot
    assert(len(data_indexes) - 1 == len(data_titles))
    plot_cmds = []
    for data_file_nickname, data_filepath in data_filepaths.items():
      for data_index, data_title in zip(data_indexes[1:], data_titles):
        if data_type is SysstatDataType.MEM:
          # convert from KB to MB
          ydata = "($%u/1000)" % (data_index)
        elif data_type is SysstatDataType.NET:
          # convert from KB/s to Mb/s
          ydata = "($%u/125)" % (data_index)
        elif data_type is SysstatDataType.IO:
          # convert from block/s to MB/s
          ydata = "($%u*512/1000000)" % (data_index)
        else:
          ydata = str(data_index)
        if data_file_nickname:
          data_title = "%s_%s" % (data_file_nickname, data_title)
        plot_cmds.append("'%s' using ($%u+%u):%s %swith lines title '%s'" % (data_filepath,
                                                                             data_indexes[0],
                                                                             time.localtime().tm_gmtoff,
                                                                             ydata,
                                                                             "smooth csplines " if smooth else "",
                                                                             data_title))
    gnuplot_code.append("plot %s" % (", ".join(plot_cmds)))

    # run gnuplot
    gnuplot_code = ";\n".join(gnuplot_code) + ";"
    subprocess.check_output(("gnuplot",),
                            input=gnuplot_code,
                            universal_newlines=True)

    # output post processing
    if not text:
      if HAS_OPTIPNG:
        logging.getLogger().debug("Crunching '%s'..." % (output_filepath))
        subprocess.check_call(("optipng", "-quiet", "-o", "7", output_filepath))
    else:
      # remove first 2 bytes as they cause problems with emails
      with open(output_filepath, "rt") as output_file:
        output_file.seek(2)
        d = output_file.read()
      with open(output_filepath, "wt") as output_file:
        output_file.write(d)


if __name__ == "__main__":
  # parse args
  arg_parser = argparse.ArgumentParser(description=__doc__,
                                       formatter_class=argparse.ArgumentDefaultsHelpFormatter)
  arg_parser.add_argument("report_type",
                          choices=tuple(t.name.lower() for t in ReportType),
                          help="Type of report")
  arg_parser.add_argument("mail_from",
                          help="Mail sender")
  arg_parser.add_argument("mail_to",
                          help="Mail destination")
  arg_parser.add_argument("-v",
                          "--verbosity",
                          choices=("warning", "normal", "debug"),
                          default="normal",
                          dest="verbosity",
                          help="Level of output to display")
  args = arg_parser.parse_args()

  # setup logger
  logging_level = {"warning": logging.WARNING,
                   "normal": logging.INFO,
                   "debug": logging.DEBUG}
  logging.basicConfig(level=logging_level[args.verbosity],
                      format="%(asctime)s %(levelname)s %(message)s")

  # display warning if optipng is missing
  if not HAS_OPTIPNG:
    logging.getLogger().warning("optipng could not be found, PNG crunching will be disabled")

  # do the job
  report_type = ReportType[args.report_type.upper()]
  with tempfile.TemporaryDirectory(prefix="%s_" % (os.path.splitext(os.path.basename(inspect.getfile(inspect.currentframe())))[0])) as temp_dir:
    sysstat_data = SysstatData(report_type, temp_dir)
    plotter = Plotter(report_type)
    plot_args = {SysstatDataType.CPU: {"title": "CPU",
                                       "data_titles": ("user",
                                                       "nice",
                                                       "system",
                                                       "iowait",
                                                       "steal",
                                                       "idle"),
                                       "ylabel": "CPU usage (%)",
                                       "yrange": (0, 100)},
                 SysstatDataType.MEM: {"title": "Memory",
                                       "data_titles": ("used",
                                                       "buffers",
                                                       "cached",
                                                       "commit",
                                                       "active",
                                                       "dirty"),
                                       "ylabel": "Memory used (MB)",
                                       "yrange": (0, get_total_memory_mb())},
                 SysstatDataType.SWAP: {"title": "Swap",
                                        "data_titles": ("swpused",),
                                        "ylabel": "Swap usage (%)",
                                        "yrange": (0, 100)},
                 SysstatDataType.NET: {"title": "Network",
                                       "data_titles": ("rx",
                                                       "tx"),
                                       "ylabel": "Bandwith (Mb/s)",
                                       "yrange": (0, "%u<*" % (get_max_network_speed()))},
                 SysstatDataType.IO: {"title": "IO",
                                      "data_titles": ("read",
                                                      "wrtn"),
                                      "ylabel": "Activity (MB/s)",
                                      "yrange": (0, None)}}

    png_filepaths = []
    txt_filepaths = []

    reboot_times = get_reboot_times()

    for data_type in SysstatDataType:
      # data
      logging.getLogger().info("Extracting %s data..." % (data_type.name))
      data_filepath = os.path.join(temp_dir, "%s.csv" % (data_type.name.lower()))
      indexes, data_filepaths = sysstat_data.generateData(data_type, data_filepath)
      if not data_filepaths:
        data_filepaths = {"": data_filepath}

      # png
      logging.getLogger().info("Generating %s PNG report..." % (data_type.name))
      png_filepaths.append(os.path.join(temp_dir, "%s.png" % (data_type.name.lower())))
      plotter.plotToPng(data_filepaths,
                        indexes,
                        data_type,
                        reboot_times,
                        png_filepaths[-1],
                        report_type is not ReportType.DAILY,
                        **plot_args[data_type])

      # text
      logging.getLogger().info("Generating %s text report..." % (data_type.name))
      txt_filepaths.append(os.path.join(temp_dir, "%s.txt" % (data_type.name.lower())))
      plotter.plotToText(data_filepaths,
                         indexes,
                         data_type,
                         reboot_times,
                         txt_filepaths[-1],
                         report_type is not ReportType.DAILY,
                         **plot_args[data_type])

    # send mail
    logging.getLogger().info("Formatting email...")
    email_data = format_email(args.mail_from,
                              args.mail_to,
                              "Sysstat %s report" % (report_type.name.lower()),
                              None,
                              png_filepaths,
                              txt_filepaths)

    real_mail_from = email.utils.parseaddr(args.mail_from)[1]
    real_mail_to = email.utils.parseaddr(args.mail_to)[1]
    logging.getLogger().info("Sending email from %s to %s..." % (real_mail_from, real_mail_to))
    subprocess.check_output(("sendmail", "-f", real_mail_from, real_mail_to),
                            input=email_data,
                            universal_newlines=True)