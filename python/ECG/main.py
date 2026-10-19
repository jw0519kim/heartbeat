# main.py  --  Raspberry Pi Pico 2 W, MicroPython
# AD8232 ECG streamer. The Pico only samples; all logic lives on the PC.
#
# Wiring (AD8232 breakout -> Pico 2 W)
#   3.3V  -> 3V3 (pin 36)
#   GND   -> GND
#   OUTPUT-> GP26 (ADC0, pin 31)
#   LO+   -> GP16
#   LO-   -> GP17
#
# Serial protocol (115200 8N1 over USB CDC, one line per message):
#   Pico -> PC :  "D,<seq>,<ticks_ms>,<adc16>,<lo>"   one per sample at FS Hz
#                 "SYNC,<ticks_ms>"                   reply to a SYNC request
#                 "INFO,fs=<FS>"                      reply to INFO
#   PC -> Pico :  "SYNC"   "START"   "STOP"   "INFO"
#
# Streaming starts only after START, so the Pico stays quiet while idle.

import sys
import select
import micropython
from array import array
from machine import ADC, Pin, Timer
from time import ticks_ms

micropython.alloc_emergency_exception_buf(100)

FS = 300                      # 300 Hz: 5샘플 이동평균이 60 Hz(와 120 Hz)를, 6샘플이 50 Hz를 정확히 노치
BUF_N = 1024                  # ring buffer length (power of two)
BUF_MASK = BUF_N - 1

ecg = ADC(26)
lo_plus = Pin(16, Pin.IN)
lo_minus = Pin(17, Pin.IN)

# Put the on-board SMPS into PWM mode: less ADC noise.
# On Pico W / Pico 2 W this pin lives on the wireless chip, not on GPIO23.
try:
    Pin("WL_GPIO1", Pin.OUT, value=1)
except Exception:
    try:
        Pin(23, Pin.OUT, value=1)
    except Exception:
        pass

# ring buffer filled from the timer ISR, drained in the main loop
buf_t = array("I", [0] * BUF_N)      # ticks_ms
buf_v = array("H", [0] * BUF_N)      # adc 16-bit
buf_lo = array("B", [0] * BUF_N)     # leads-off flag
head = 0
tail = 0
seq = 0
dropped = 0
streaming = False


def _sample(_t):
    global head, dropped
    v = ecg.read_u16()
    lo = 1 if (lo_plus.value() or lo_minus.value()) else 0
    nxt = (head + 1) & BUF_MASK
    if nxt == tail:
        dropped += 1
        return
    buf_t[head] = ticks_ms()
    buf_v[head] = v
    buf_lo[head] = lo
    head = nxt


tim = Timer()
poll = select.poll()
poll.register(sys.stdin, select.POLLIN)
out = sys.stdout
out.write("READY,fs=%d\n" % FS)      # 부팅 확인용 (Thonny 쉘에서 Stop/Restart 하면 보임)


def start():
    global head, tail, seq, dropped, streaming
    if streaming:
        return
    head = tail = seq = dropped = 0
    streaming = True
    tim.init(freq=FS, mode=Timer.PERIODIC, callback=_sample)


def stop():
    global streaming
    if not streaming:
        return
    tim.deinit()
    streaming = False


def handle(cmd):
    cmd = cmd.strip()
    if cmd == "SYNC":
        out.write("SYNC,%d\n" % ticks_ms())
    elif cmd == "START":
        start()
        out.write("OK,START\n")
    elif cmd == "STOP":
        stop()
        out.write("OK,STOP,dropped=%d\n" % dropped)
    elif cmd == "INFO":
        out.write("INFO,fs=%d,streaming=%d\n" % (FS, 1 if streaming else 0))


line = ""
while True:
    # drain samples
    while tail != head:
        i = tail
        out.write("D,%d,%d,%d,%d\n" % (seq, buf_t[i], buf_v[i], buf_lo[i]))
        seq += 1
        tail = (i + 1) & BUF_MASK
    # commands from the PC (non-blocking)
    if poll.poll(0):
        ch = sys.stdin.read(1)
        if ch == "\n" or ch == "\r":
            if line:
                handle(line)
            line = ""
        else:
            line += ch
