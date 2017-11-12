from machine import UART
from machine import Pin
import pycom
import gc

__version__ = '1.0.1'


PIN_MASK = 0b1010
COMM_PIN = 'P10'

PIN_WAKE = 0
TIMER_WAKE = 1 << 4
POWER_ON_WAKE = 1 << 5


class DeepSleep:

    WPUA_ADDR = const(0x09)
    OPTION_REG_ADDR = const(0x0E)
    IOCAP_ADDR = const(0x1A)
    IOCAN_ADDR = const(0x1B)

    WAKE_STATUS_ADDR = const(0x40)
    MIN_BAT_ADDR = const(0x41)
    SLEEP_TIME_ADDR = const(0x42)
    CTRL_0_ADDR = const(0x45)

    EXP_RTC_PERIOD = const(7000)

    def __init__(self):
        self.uart = UART(1, baudrate=10000, pins=(COMM_PIN, ))
        self.clk_cal_factor = 1
        self.uart.read()
        # enable the weak pull-ups control
        self.clearbits(OPTION_REG_ADDR, 1 << 7)

    def _send(self, data):
        self.uart.write(bytes(data))

    def _start(self):
        self.uart.sendbreak(20)
        self._send([0x55])

    def _magic(self, address, and_val, or_val, xor_val, expected=None):
        self._start()
        self._send([address, and_val & 0xFF, or_val & 0xFF, xor_val & 0xFF])
        if expected is None:
            return self.uart.read()
        else:
            if expected > 0:
                return self.uart.read(expected)

    def _add_to_pin_mask(self, mask, pin):
        if pin == 'P10' or pin == 'G17':
            mask |= 0x01
        elif pin == 'P17' or pin == 'G31':
            mask |= 0x02
        elif pin == 'P18' or pin == 'G30':
            mask |= 0x08
        else:
            raise ValueError('Invalid Pin specified: {}'.format(pin))
        return mask

    def _create_pin_mask(self, pins):
        mask = 0
        if type(pins) is str:
            mask = self._add_to_pin_mask(mask, pins)
        else:
            for pin in pins:
                mask = self._add_to_pin_mask(mask, pin)
        return mask & PIN_MASK

    def poke(self, address, value):
        self._magic(address, 0, value, 0)

    def peek(self, address):
        return self._magic(address, 0xFF, 0, 0)[6]

    def setbits(self, address, mask):
        self._magic(address, 0xFF, mask, 0)

    def clearbits(self, address, mask):
        self._magic(address, ~mask, 0, 0)

    def togglebits(self, address, mask):
        self._magic(address, 0xFF, 0, mask)

    def calibrate(self):
        """ The microcontroller will send the value of CTRL_0 after setting the bit
            and then will send the following pattern through the data line:

               val | 1 | 0 | 1*| 0 | 1*| 0 | 1
               ms  | 1 | 1 | 1 | 1 | 8 | 1 | -

            The idea is to measure the real life duration of periods marked with *
            and substract them. That will remove any errors common to both measurements
            The result is 7 ms as generated by the PIC LF clock.
            It can be used to scale any future sleep value. """

        # setbits, but limit the number of received bytes to avoid confusion with pattern
        self._magic(CTRL_0_ADDR, 0xFF, 1 << 2, 0, 0)
        self.uart.deinit()
        self._pulses = pycom.pulses_get(COMM_PIN, 50)
        self.uart = UART(1, baudrate=10000, pins=(COMM_PIN, ))
        try:
            self.clk_cal_factor = (self._pulses[4][1] - self._pulses[1][1]) / EXP_RTC_PERIOD
        except:
            pass
        if self.clk_cal_factor > 1.25 or self.clk_cal_factor < 0.75:
            self.clk_cal_factor = 1

    def enable_auto_poweroff(self):
        self.setbits(CTRL_0_ADDR, 1 << 1)

    def enable_pullups(self, pins):
        mask = self._create_pin_mask(pins)
        self.setbits(WPUA_ADDR, mask)

    def disable_pullups(self, pins):
        mask = self._create_pin_mask(pins)
        self.clearbits(WPUA_ADDR, mask)

    def enable_wake_on_raise(self, pins):
        mask = self._create_pin_mask(pins)
        self.setbits(IOCAP_ADDR, mask)

    def disable_wake_on_raise(self, pins):
        mask = self._create_pin_mask(pins)
        self.clearbits(IOCAP_ADDR, mask)

    def enable_wake_on_fall(self, pins):
        mask = self._create_pin_mask(pins)
        self.setbits(IOCAN_ADDR, mask)

    def disable_wake_on_fall(self, pins):
        mask = self._create_pin_mask(pins)
        self.clearbits(IOCAN_ADDR, mask)

    def get_wake_status(self):
        # bits as they are returned from PIC:
        #   0: PIN 0 value after awake
        #   1: PIN 1 value after awake
        #   2: PIN 2 value after awake
        #   3: PIN 3 value after awake
        #   4: TIMEOUT
        #   5: POWER ON

        wake_r = self.peek(WAKE_STATUS_ADDR)
        return {'wake': wake_r & (TIMER_WAKE | POWER_ON_WAKE),
                'P10': wake_r & 0x01, 'P17': (wake_r & 0x02) >> 1,
                'P18': (wake_r & 0x08) >> 3}

    def set_min_voltage_limit(self, value):
        # voltage value passed in volts (e.g. 3.6) and round it to the nearest integer
        value = int(((256 * 2.048) + (value / 2)) / value)
        self.poke(MIN_BAT_ADDR, value)

    def go_to_sleep(self, seconds):
        gc.collect()
        while True:
            try:
                self.calibrate()
            except Exception:
                pass

            # the 1.024 factor is because the PIC LF operates at 31 KHz
            # WDT has a frequency divider to generate 1 ms
            # and then there is a binary prescaler, e.g., 1, 2, 4 ... 512, 1024 ms
            # hence the need for the constant

            # round to the nearest integer
            seconds = int((seconds / (1.024 * self.clk_cal_factor)) + 0.5)
            self.poke(SLEEP_TIME_ADDR, (seconds >> 16) & 0xFF)
            self.poke(SLEEP_TIME_ADDR + 1, (seconds >> 8) & 0xFF)
            self.poke(SLEEP_TIME_ADDR + 2, seconds & 0xFF)
            self.setbits(CTRL_0_ADDR, 1 << 0)

    def hw_reset(self):
        self.setbits(CTRL_0_ADDR, 1 << 4)
