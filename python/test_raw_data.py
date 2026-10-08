# raw_red.py — MAX30102 Red LED raw 값 출력 (MicroPython)
# 칩이 2.5 ms마다 측정(400 sps)하고 4개씩 평균 → 10 ms마다 1개 값(100 Hz)

from machine import Pin, SoftI2C
import utime
#import max30102 as drv
#drv.STORAGE_QUEUE_SIZE = 32          # 드라이버 버퍼 확대 (샘플 유실 방지)
from max30102 import MAX30102

SDA_pin = 4
SCL_pin = 5
i2c_freq = 400000

i2c = SoftI2C(sda=Pin(SDA_pin), scl=Pin(SCL_pin), freq=i2c_freq)
sensor = MAX30102(i2c=i2c)

sensor.setup_sensor()

'''
sensor.setup_sensor(
    led_mode=1,          # Red LED만 사용
    adc_range=16384,
    sample_rate=400,     # 2.5 ms마다 측정
    sample_avg=4,        # 4개 평균 → 10 ms마다 1개
    pulse_width=411,
    led_power=0x24,      # 약 7 mA
)
'''

'''
try:
    while True:
        sensor.check()                       # 칩에 쌓인 값 가져오기
        while sensor.available():
            print(sensor.pop_red_from_storage())
        utime.sleep_ms(5)                          # 10 ms보다 짧게 쉬어서 놓치지 않게
except KeyboardInterrupt:
    sensor.shutdown()
'''

while True:
    # The check() method has to be continuously polled, to check if
    # there are new readings into the sensor's FIFO queue. When new
    # readings are available, this function will put them into the storage.
    sensor.check()

    # Drain all queued samples — check() may add multiple per call.
    while sensor.available():
        # Access the storage FIFO and gather the readings (integers)
        red_sample = sensor.pop_red_from_storage()
        ir_sample = sensor.pop_ir_from_storage()

        # Print the acquired data (can be plot with Arduino Serial Plotter)
        print(red_sample, ",", ir_sample)