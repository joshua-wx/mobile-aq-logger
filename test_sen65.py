"""
Simple SEN65 test: print all measured variables once per second.

Wiring (ESP32-C3):
    SEN65 VDD -> 3V3
    SEN65 GND -> GND
    SEN65 SDA -> GPIO 5
    SEN65 SCL -> GPIO 6

Adjust the I2C id / pins below for your board.
"""

import time
from machine import I2C, Pin

from sen65 import SEN65, SEN65_DEFAULT_ADDR

def fmt(value, unit, width=6):
    """Format a value that may be None during sensor warm-up."""
    if value is None:
        return "{:>{w}}".format("--", w=width) + " " + unit
    return "{:>{w}.1f} {}".format(value, unit, w=width)


# --- I2C setup (change pins to suit your board) --------------------------
i2c = I2C(0, scl=Pin(6), sda=Pin(5), freq=100_000)

sen = SEN65(i2c)
print(i2c.scan())

# Make sure we start from a known state.
sen.device_reset()

print("Product name :", sen.product_name())
print("Serial number:", sen.serial_number())

sen.start_measurement()
print("Measurement started. New data is available roughly every second.\n")

try:
    while True:
        # Wait until the sensor has a fresh sample ready.
        while not sen.data_ready():
            time.sleep_ms(50)

        m = sen.read_measured_values()
        n = sen.read_number_concentration()

        print("PM1.0 :", fmt(m.pm1p0, "ug/m3"),
              "| PM2.5 :", fmt(m.pm2p5, "ug/m3"),
              "| PM4.0 :", fmt(m.pm4p0, "ug/m3"),
              "| PM10  :", fmt(m.pm10p0, "ug/m3"))
        print("RH    :", fmt(m.humidity, "%RH "),
              "| Temp  :", fmt(m.temperature, "degC "),
              "| VOC   :", fmt(m.voc_index, "idx  "),
              "| NOx   :", fmt(m.nox_index, "idx"))
        print("N0.5  :", fmt(n.pm0p5, "#/cm3"),
              "| N1.0  :", fmt(n.pm1p0, "#/cm3"),
              "| N2.5  :", fmt(n.pm2p5, "#/cm3"),
              "| N4.0  :", fmt(n.pm4p0, "#/cm3"),
              "| N10   :", fmt(n.pm10p0, "#/cm3"))
        print("-" * 78)

        time.sleep(1)
except KeyboardInterrupt:
    sen.stop_measurement()
    print("\nMeasurement stopped.")
