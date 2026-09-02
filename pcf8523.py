"""
MicroPython I2C driver for the NXP PCF8523 RTC.

The PCF8523 is a low-power real-time clock with battery backup and an
on-chip offset (calibration) register. This driver reads and sets the time,
configures the battery switch-over that the chip leaves *disabled* at
power-on, and reports whether the stored time is trustworthy.

Fixed I2C address is 0x68, so it shares a bus happily with a SEN65 (0x6B)
and a BME688 (0x76/0x77). Note that 0x68 is also the DS3231 address — the
two RTCs cannot sit on the same bus.

Time is exchanged as an 8-tuple in the same order as MicroPython's
machine.RTC().datetime():

    (year, month, day, weekday, hour, minute, second, subsecond)

so you can sync the MCU's internal RTC straight from it:

    import machine
    from pcf8523 import PCF8523
    rtc = PCF8523(i2c)
    machine.RTC().datetime(rtc.datetime())

weekday is 0=Monday .. 6=Sunday and is computed from the date automatically,
so you may pass any value (e.g. 0) in that slot when setting the time.
The PCF8523 has no readable sub-second register, so subsecond is always 0.

Two power-on defaults are worth knowing about, and __init__ fixes both
unless you tell it not to:

  * Battery switch-over is DISABLED out of reset (PM = 111). Until it is
    configured the coin cell does nothing and the clock loses time on every
    power cycle. init() sets standard-mode switch-over with battery-low
    detection (PM = 000), which is the right choice for a 3.3 V rail and a
    3 V cell.
  * CLKOUT runs at 32.768 kHz out of reset, which wastes backup-battery
    current for no benefit here, so init() disables it.
"""

import time

PCF8523_ADDR = 0x68

# Register map
_REG_CONTROL_1 = 0x00
_REG_CONTROL_2 = 0x01
_REG_CONTROL_3 = 0x02
_REG_SECONDS   = 0x03      # bit 7 = OS (oscillator stop flag)
_REG_MINUTES   = 0x04
_REG_HOURS     = 0x05
_REG_DAYS      = 0x06
_REG_WEEKDAYS  = 0x07
_REG_MONTHS    = 0x08
_REG_YEARS     = 0x09
_REG_OFFSET    = 0x0E
_REG_CLKOUT    = 0x0F

_OS_BIT       = 0x80       # seconds: oscillator stopped, time not reliable
_STOP_BIT     = 0x20       # control_1: freeze the time circuits
_MODE_1224    = 0x08       # control_1: 1 = 12-hour mode
_CAP_SEL_BIT  = 0x80       # control_1: 0 = 7 pF crystal, 1 = 12.5 pF
_HOUR_AMPM    = 0x20       # hours: PM flag when in 12-hour mode
_BLF_BIT      = 0x04       # control_3: battery low flag
_BSF_BIT      = 0x08       # control_3: battery switch-over flag
_PM_MASK      = 0xE0       # control_3: PM[2:0] in bits 7..5
_COF_MASK     = 0x38       # clkout: COF[2:0] in bits 5..3
_COF_DISABLED = 0x38       # COF = 111 -> CLKOUT high-Z
_OFFSET_MODE  = 0x80       # offset: 0 = correct every 2 h, 1 = every minute

_SW_RESET = 0x58           # written to control_1 to reset the chip

# Battery switch-over modes for the pm= argument (PM[2:0], pre-shifted).
PM_STANDARD      = 0x00    # switch-over in standard mode, low detect on
PM_DIRECT        = 0x20    # switch-over in direct mode, low detect on
PM_STANDARD_NOBL = 0x80    # switch-over in standard mode, low detect off
PM_DIRECT_NOBL   = 0xA0    # switch-over in direct mode, low detect off
PM_OFF           = 0xE0    # switch-over disabled (power-on default)


def _bcd2dec(value):
    return (value >> 4) * 10 + (value & 0x0F)


def _dec2bcd(value):
    return ((value // 10) << 4) | (value % 10)


def _weekday(year, month, day):
    """Day of week, 0=Monday .. 6=Sunday (Sakamoto's algorithm)."""
    t = (0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4)
    y = year - (1 if month < 3 else 0)
    w = (y + y // 4 - y // 100 + y // 400 + t[month - 1] + day) % 7
    # Sakamoto gives 0=Sunday; shift to 0=Monday.
    return (w + 6) % 7


class PCF8523Error(Exception):
    pass


class PCF8523:
    def __init__(self, i2c, address=PCF8523_ADDR, pm=PM_STANDARD,
                 clkout=False, cap_12p5pf=None):
        """Open the RTC and apply the settings the chip does not default to.

        pm          battery switch-over mode, one of the PM_* constants.
                    Pass None to leave the register alone.
        clkout      True leaves the CLKOUT square wave running as-is;
                    False (default) turns it off to save backup current.
        cap_12p5pf  quartz load capacitance: True for a 12.5 pF crystal,
                    False for 7 pF, None (default) to leave it unchanged.
                    Getting this wrong costs accuracy, so check the crystal
                    on your breakout — Adafruit's PCF8523 board is 12.5 pF.
        """
        self.i2c = i2c
        self.address = address
        self.init(pm=pm, clkout=clkout, cap_12p5pf=cap_12p5pf)

    # --- low-level --------------------------------------------------------
    def _read(self, reg, n=1):
        return self.i2c.readfrom_mem(self.address, reg, n)

    def _write(self, reg, data):
        if isinstance(data, int):
            data = bytes((data & 0xFF,))
        self.i2c.writeto_mem(self.address, reg, data)

    def _update(self, reg, mask, value):
        """Read-modify-write the bits of reg selected by mask."""
        cur = self._read(reg)[0]
        self._write(reg, (cur & ~mask) | (value & mask))

    # --- configuration ----------------------------------------------------
    def init(self, pm=PM_STANDARD, clkout=False, cap_12p5pf=None):
        """Apply the non-default settings described in __init__."""
        ctrl1 = self._read(_REG_CONTROL_1)[0]
        # Always run in 24-hour mode and never leave the clock stopped.
        new1 = ctrl1 & ~(_MODE_1224 | _STOP_BIT)
        if cap_12p5pf is not None:
            new1 = (new1 | _CAP_SEL_BIT) if cap_12p5pf else (new1 & ~_CAP_SEL_BIT)
        if new1 != ctrl1:
            self._write(_REG_CONTROL_1, new1)

        if pm is not None:
            self._update(_REG_CONTROL_3, _PM_MASK, pm)

        if not clkout:
            self._update(_REG_CLKOUT, _COF_MASK, _COF_DISABLED)

    def reset(self):
        """Software reset: restores every register to its power-on default.

        This re-disables battery switch-over, so call init() afterwards.
        """
        self._write(_REG_CONTROL_1, _SW_RESET)
        time.sleep_ms(10)

    # --- power / validity -------------------------------------------------
    def lost_power(self):
        """True if oscillator integrity is not guaranteed.

        Set at power-on and whenever the oscillator has stopped, so when it
        is True the stored time is not trustworthy and should be re-set.
        """
        return bool(self._read(_REG_SECONDS)[0] & _OS_BIT)

    def battery_low(self):
        """True if the backup cell has dropped below the detection threshold.

        Only meaningful when battery-low detection is enabled (PM_STANDARD
        or PM_DIRECT). The flag is not latched — it tracks the cell.
        """
        return bool(self._read(_REG_CONTROL_3)[0] & _BLF_BIT)

    def battery_switched(self):
        """True if the chip has run from the backup cell since last checked.

        Reading does not clear it; call clear_battery_switched() for that.
        """
        return bool(self._read(_REG_CONTROL_3)[0] & _BSF_BIT)

    def clear_battery_switched(self):
        self._update(_REG_CONTROL_3, _BSF_BIT, 0)

    def _clear_os(self, retries=5):
        """Clear the oscillator-stop flag, retrying while it sticks.

        The flag can only be cleared once the oscillator is actually
        running, which can take up to two seconds after power-up.
        """
        for _ in range(retries):
            sec = self._read(_REG_SECONDS)[0]
            if not (sec & _OS_BIT):
                return True
            self._write(_REG_SECONDS, sec & ~_OS_BIT)
            time.sleep_ms(500)
        return not bool(self._read(_REG_SECONDS)[0] & _OS_BIT)

    # --- time -------------------------------------------------------------
    def datetime(self, dt=None):
        """Get or set the time.

        Called with no argument: returns
            (year, month, day, weekday, hour, minute, second, subsecond)
        Called with that 8-tuple: sets the clock (weekday is recomputed,
        subsecond is ignored) and clears the oscillator-stop flag.
        """
        if dt is None:
            return self._get_datetime()
        self._set_datetime(dt)

    def _get_datetime(self):
        d = self._read(_REG_SECONDS, 7)
        second = _bcd2dec(d[0] & 0x7F)
        minute = _bcd2dec(d[1] & 0x7F)

        hreg = d[2]
        if self._read(_REG_CONTROL_1)[0] & _MODE_1224:   # 12-hour mode
            hour = _bcd2dec(hreg & 0x1F)
            if hreg & _HOUR_AMPM:                        # PM
                if hour != 12:
                    hour += 12
            elif hour == 12:                             # 12 AM -> 0
                hour = 0
        else:                                            # 24-hour mode
            hour = _bcd2dec(hreg & 0x3F)

        day = _bcd2dec(d[3] & 0x3F)
        month = _bcd2dec(d[5] & 0x1F)
        year = 2000 + _bcd2dec(d[6])

        # d[4] is the chip's own weekday counter, which is free-running and
        # only as correct as whoever last set it; derive it from the date.
        return (year, month, day, _weekday(year, month, day),
                hour, minute, second, 0)

    def _set_datetime(self, dt):
        year, month, day, _, hour, minute, second = dt[:7]

        if not (2000 <= year <= 2099):
            raise PCF8523Error("year out of range (2000-2099): %d" % year)

        # Freeze the time circuits so a rollover cannot land in the middle
        # of the write. STOP also clears the prescaler, so the new second
        # starts cleanly the moment it is released.
        ctrl1 = self._read(_REG_CONTROL_1)[0]
        self._write(_REG_CONTROL_1, ctrl1 | _STOP_BIT)
        try:
            buf = bytes((
                _dec2bcd(second) & 0x7F,               # bit 7 = 0 -> clear OS
                _dec2bcd(minute) & 0x7F,
                _dec2bcd(hour) & 0x3F,
                _dec2bcd(day) & 0x3F,
                (_weekday(year, month, day) + 1) % 7,  # chip uses 0=Sunday
                _dec2bcd(month) & 0x1F,
                _dec2bcd(year - 2000),
            ))
            self._write(_REG_SECONDS, buf)
        finally:
            self._write(_REG_CONTROL_1, ctrl1 & ~_STOP_BIT)

        self._clear_os()

    # --- calibration ------------------------------------------------------
    def offset(self, value=None, every_minute=False):
        """Get or set the offset (calibration) register.

        The value is a signed 7-bit count, -64..63. One step is 4.34 ppm in
        the default two-hourly correction mode (~0.375 s/day) and 4.069 ppm
        when every_minute=True. Positive values slow the clock down.

        Returns (value, every_minute) when read.
        """
        if value is None:
            raw = self._read(_REG_OFFSET)[0]
            mode = bool(raw & _OFFSET_MODE)
            off = raw & 0x7F
            return (off - 128 if off & 0x40 else off, mode)

        if not (-64 <= value <= 63):
            raise PCF8523Error("offset out of range (-64..63): %d" % value)
        raw = value & 0x7F
        if every_minute:
            raw |= _OFFSET_MODE
        self._write(_REG_OFFSET, raw)
