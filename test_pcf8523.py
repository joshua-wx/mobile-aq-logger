"""
Simple PCF8523 test: print the time once per second, and set it on demand.

Shares the I2C bus with the SEN65 (0x6B); the PCF8523 sits at 0x68.
Note that 0x68 is also the DS3231 address — only one of the two can be on
the bus at a time.

Wiring (ESP32-S3):
    PCF8523 VCC -> 3V3
    PCF8523 GND -> GND
    PCF8523 SDA -> GPIO 5
    PCF8523 SCL -> GPIO 6
    (fit the CR1220 backup cell, or the time is lost on every power cycle)
"""

import time
from machine import I2C, Pin

from pcf8523 import PCF8523, PCF8523_ADDR

# --- I2C setup (change pins to suit your board) --------------------------
i2c = I2C(0, scl=Pin(6), sda=Pin(5), freq=100_000)

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Set this True once to write the time, then flash again with it False so a
# reset doesn't keep overwriting the clock. (year, month, day, weekday,
# hour, minute, second, subsecond) -- weekday is recomputed for you.
#
# In normal use you don't need this at all: run main.py and press "Sync RTC
# to browser time" on the dashboard, which is accurate to the second.
SET_TIME = False
NEW_TIME = (2026, 9, 2, 0, 14, 30, 0, 0)

# True for a 12.5 pF crystal (Adafruit's breakout), False for 7 pF.
CAP_12P5PF = True


def main():
    if PCF8523_ADDR not in i2c.scan():
        raise SystemExit("PCF8523 not found at 0x%02X. Check wiring." % PCF8523_ADDR)

    # Constructing the driver also enables battery switch-over, which the
    # chip leaves off at power-on, and disables the 32 kHz CLKOUT.
    rtc = PCF8523(i2c, cap_12p5pf=CAP_12P5PF)

    if SET_TIME:
        rtc.datetime(NEW_TIME)
        print("Time set.")

    if rtc.lost_power():
        print("WARNING: oscillator stopped since the time was last set.")
        print("         The time below is not trustworthy -- set SET_TIME = True")
        print("         or use the dashboard's RTC sync button.\n")

    if rtc.battery_low():
        print("WARNING: backup battery is low -- replace the coin cell.\n")

    offset, per_minute = rtc.offset()
    print("Offset register: %d (%s)\n"
          % (offset, "every minute" if per_minute else "every two hours"))

    while True:
        year, month, day, weekday, hour, minute, second, _ = rtc.datetime()
        print("{:04d}-{:02d}-{:02d} {} {:02d}:{:02d}:{:02d}"
              .format(year, month, day, _DAYS[weekday], hour, minute, second))
        time.sleep(1)


if __name__ == "__main__":
    main()
