"""
MicroPython I2C driver for the Sensirion SEN65 environmental sensor.

Ported from the official Arduino library:
    https://github.com/Sensirion/arduino-i2c-sen65
(command IDs, execution delays and scaling factors taken from
 sensirion-driver-generator 1.7.0, product sen65, model 1.4.1).

The SEN65 reports particulate matter (PM1.0/2.5/4.0/10), relative humidity,
temperature, a VOC index and a NOx index.

Default I2C address is 0x6B.

Example
-------
    from machine import I2C, Pin
    from sen65 import SEN65

    i2c = I2C(0, scl=Pin(22), sda=Pin(21), freq=100_000)
    sen = SEN65(i2c)
    sen.start_measurement()
    ...
    if sen.data_ready():
        m = sen.read_measured_values()
        print(m.temperature, m.humidity)
"""

import time
import struct

try:
    from collections import namedtuple
except ImportError:  # pragma: no cover
    namedtuple = None

SEN65_DEFAULT_ADDR = 0x6B

# --- Command IDs (16-bit, big-endian on the wire) -------------------------
_CMD_START_MEASUREMENT          = 0x0021
_CMD_STOP_MEASUREMENT           = 0x0104
_CMD_GET_DATA_READY             = 0x0202
_CMD_READ_MEASURED_VALUES       = 0x0446   # ...as integers
_CMD_READ_NUMBER_CONC_VALUES    = 0x0316   # ...as integers
_CMD_START_FAN_CLEANING         = 0x5607
_CMD_ACTIVATE_SHT_HEATER        = 0x6765
_CMD_GET_PRODUCT_NAME           = 0xD014
_CMD_GET_SERIAL_NUMBER          = 0xD033
_CMD_READ_DEVICE_STATUS         = 0xD206
_CMD_READ_CLEAR_DEVICE_STATUS   = 0xD210
_CMD_DEVICE_RESET               = 0xD304

# Execution delays in milliseconds (command -> time before reading/next cmd)
_DELAY_START_MEASUREMENT_MS     = 50
_DELAY_STOP_MEASUREMENT_MS      = 1400
_DELAY_GENERIC_MS               = 20
_DELAY_DEVICE_RESET_MS          = 1200


if namedtuple is not None:
    Measurement = namedtuple(
        "Measurement",
        ("pm1p0", "pm2p5", "pm4p0", "pm10p0",
         "humidity", "temperature", "voc_index", "nox_index"),
    )
    NumberConcentration = namedtuple(
        "NumberConcentration",
        ("pm0p5", "pm1p0", "pm2p5", "pm4p0", "pm10p0"),
    )
else:  # fallback for ports without namedtuple
    Measurement = tuple
    NumberConcentration = tuple


def _crc8(data):
    """Sensirion CRC-8 (poly 0x31, init 0xFF) over two data bytes."""
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x31) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


class SEN65Error(Exception):
    """Raised on I2C communication or CRC failures."""
    pass


class SEN65:
    def __init__(self, i2c, address=SEN65_DEFAULT_ADDR):
        self.i2c = i2c
        self.address = address

    # --- low-level transport ---------------------------------------------
    def _send_command(self, command, delay_ms=0):
        self.i2c.writeto(self.address, struct.pack(">H", command))
        if delay_ms:
            time.sleep_ms(delay_ms)

    def _read_words(self, command, num_words, delay_ms=_DELAY_GENERIC_MS):
        """Send `command`, wait, then read `num_words` CRC-protected words.

        Returns a list of raw 16-bit integers (unsigned, big-endian).
        """
        self._send_command(command, delay_ms)
        raw = self.i2c.readfrom(self.address, num_words * 3)
        words = []
        for i in range(num_words):
            chunk = raw[i * 3:i * 3 + 2]
            crc = raw[i * 3 + 2]
            if _crc8(chunk) != crc:
                raise SEN65Error("CRC mismatch in word %d" % i)
            words.append((chunk[0] << 8) | chunk[1])
        return words

    @staticmethod
    def _to_signed(value):
        return value - 0x10000 if value & 0x8000 else value

    # --- measurement control ---------------------------------------------
    def start_measurement(self):
        """Enter continuous measurement mode (new data ~every 1 s)."""
        self._send_command(_CMD_START_MEASUREMENT, _DELAY_START_MEASUREMENT_MS)

    def stop_measurement(self):
        """Return to idle mode."""
        self._send_command(_CMD_STOP_MEASUREMENT, _DELAY_STOP_MEASUREMENT_MS)

    def device_reset(self):
        self._send_command(_CMD_DEVICE_RESET, _DELAY_DEVICE_RESET_MS)

    def start_fan_cleaning(self):
        self._send_command(_CMD_START_FAN_CLEANING, _DELAY_GENERIC_MS)

    def data_ready(self):
        """Return True if a new measurement is available to read."""
        words = self._read_words(_CMD_GET_DATA_READY, 1)
        # high byte is padding, low byte is the ready flag
        return (words[0] & 0xFF) != 0

    # --- measured values --------------------------------------------------
    def read_measured_values(self):
        """Read PM, humidity, temperature, VOC and NOx.

        Returns a Measurement namedtuple in physical units:
            pm1p0, pm2p5, pm4p0, pm10p0   in µg/m³
            humidity                       in %RH
            temperature                    in °C
            voc_index                      index points, 1-500, baseline 100
            nox_index                      index points, 1-500, baseline 1

        The two indices do NOT share a baseline. VOC rests near 100 and moves
        either way; NOx rests at 1 and only climbs when NOx is present, so a
        steady nox_index of 1.0 means "none detected", not a broken sensor.

        Both read 0 for roughly the first minute while the gas index
        algorithm establishes itself. Unavailable values are reported as None
        (the sensor returns the 0xFFFF / 0x7FFF sentinels during warm-up).
        """
        w = self._read_words(_CMD_READ_MEASURED_VALUES, 8)

        def u(raw, scale, sentinel=0xFFFF):
            return None if raw == sentinel else raw / scale

        def s(raw, scale, sentinel=0x7FFF):
            sv = self._to_signed(raw)
            return None if raw == sentinel else sv / scale

        return Measurement(
            pm1p0=u(w[0], 10.0),
            pm2p5=u(w[1], 10.0),
            pm4p0=u(w[2], 10.0),
            pm10p0=u(w[3], 10.0),
            humidity=s(w[4], 100.0),
            temperature=s(w[5], 200.0),
            voc_index=s(w[6], 10.0),
            nox_index=s(w[7], 10.0),
        )

    def read_number_concentration(self):
        """Read particle number concentration (#/cm³) per size bin."""
        w = self._read_words(_CMD_READ_NUMBER_CONC_VALUES, 5)

        def u(raw, sentinel=0xFFFF):
            return None if raw == sentinel else raw / 10.0

        return NumberConcentration(
            pm0p5=u(w[0]),
            pm1p0=u(w[1]),
            pm2p5=u(w[2]),
            pm4p0=u(w[3]),
            pm10p0=u(w[4]),
        )

    # --- device information ----------------------------------------------
    def _read_string(self, command):
        # 32 ASCII bytes => 16 CRC-protected words, NUL-terminated
        words = self._read_words(command, 16)
        chars = bytearray()
        for word in words:
            chars.append((word >> 8) & 0xFF)
            chars.append(word & 0xFF)
        nul = chars.find(b"\x00")
        if nul >= 0:
            chars = chars[:nul]
        return bytes(chars).decode("ascii")

    def product_name(self):
        return self._read_string(_CMD_GET_PRODUCT_NAME)

    def serial_number(self):
        return self._read_string(_CMD_GET_SERIAL_NUMBER)

    def device_status(self):
        """Return the 32-bit device status register as an int."""
        words = self._read_words(_CMD_READ_DEVICE_STATUS, 2)
        return (words[0] << 16) | words[1]