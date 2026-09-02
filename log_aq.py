"""
Log SEN65 air quality data with PCF8523 timestamps to a CSV file.

A new file is created on each run, named after the first sample's timestamp
(e.g. 20260611_143000.csv).  One row is appended per second containing:
    PM1.0, PM2.5, PM4.0, PM10  (µg/m³)
    VOC index, NOx index        (dimensionless, 1–500)

This is the headless alternative to main.py, which does the same logging
behind a web dashboard. Both take time from the PCF8523: the ESP32 clock is
seeded from it at startup and timestamps then come from the ESP32, so the
I2C bus is not disturbed once per sample.

Wiring (ESP32-C3):
    SDA -> GPIO 5
    SCL -> GPIO 6
    SEN65 at 0x6B, PCF8523 at 0x68
"""

import time
from machine import I2C, Pin, RTC

from pcf8523 import PCF8523
from sen65 import SEN65

_CSV_HEADER = "timestamp,PM1.0_ug_m3,PM2.5_ug_m3,PM4.0_ug_m3,PM10_ug_m3,VOC_index,NOx_index\n"


def _fmt(value):
    return "" if value is None else "{:.1f}".format(value)


def _timestamp():
    y, mo, d, h, mi, s = time.localtime()[:6]
    return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(y, mo, d, h, mi, s)


def _filename():
    y, mo, d, h, mi, s = time.localtime()[:6]
    return "{:04d}{:02d}{:02d}_{:02d}{:02d}{:02d}.csv".format(y, mo, d, h, mi, s)


def main():
    i2c = I2C(0, scl=Pin(6), sda=Pin(5), freq=100_000)

    rtc = PCF8523(i2c)
    sen = SEN65(i2c)

    if rtc.lost_power():
        print("WARNING: PCF8523 oscillator stopped — timestamps will be wrong.")
        print("         Set the clock with test_pcf8523.py, or run main.py and")
        print("         use the dashboard's RTC sync button.")
    else:
        RTC().datetime(rtc.datetime())
    if rtc.battery_low():
        print("WARNING: PCF8523 backup battery is low — replace the coin cell.")

    sen.device_reset()
    sen.start_measurement()
    print("SEN65 started. Waiting for first sample...")

    # Wait for the first sample before opening the file so its name reflects
    # when data actually starts, not when the script booted.
    while not sen.data_ready():
        time.sleep_ms(50)

    fname = _filename()
    with open(fname, "w") as f:
        f.write(_CSV_HEADER)
    print("Logging to", fname)

    try:
        while True:
            # data_ready() is still True from the wait above on the first
            # pass; subsequent passes poll until the sensor has a fresh sample.
            while not sen.data_ready():
                time.sleep_ms(50)

            m = sen.read_measured_values()

            row = "{},{},{},{},{},{},{}\n".format(
                _timestamp(),
                _fmt(m.pm1p0),
                _fmt(m.pm2p5),
                _fmt(m.pm4p0),
                _fmt(m.pm10p0),
                _fmt(m.voc_index),
                _fmt(m.nox_index),
            )

            with open(fname, "a") as f:
                f.write(row)

            print(row, end="")

    except KeyboardInterrupt:
        sen.stop_measurement()
        print("\nLogging stopped.")


main()
