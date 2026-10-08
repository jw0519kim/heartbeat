# hr_red.py — MAX30102 RED LED 기반 심박수(IBI/BPM) 측정 (MicroPython)
#
# 처리 흐름:
#   raw(Red, 18bit) → 손가락/포화 검사 → DC 제거(HPF) → 저역통과(4 Hz, Butterworth)
#   → 부호 반전(박동 = 양의 피크) → 적응형 임계값 + 불응기 피크 검출
#   → 포물선 보간으로 피크 시각 보정 → IBI 이상치 제거 → BPM(중앙값)
#
# 출력(CSV): t_s, ibi_ms, bpm_inst, bpm_avg     ('#'으로 시작하는 줄은 상태 메시지)

from machine import Pin, SoftI2C
from utime import sleep_ms
import math

import max30102 as drv
drv.STORAGE_QUEUE_SIZE = 32   # 드라이버 기본값 4 → 폴링이 늦을 때 샘플 유실 방지 (인스턴스 생성 전에 설정)
from max30102 import MAX30102, MAX3010X_I2C_ADDRESS

# ===================== 설정 =====================
BOARD = "ESP32"                     # "ESP32" 또는 "PICO"
if BOARD == "ESP32":
    I2C_SDA, I2C_SCL = 21, 22
else:                               # Raspberry Pi Pico (GP4/GP5)
    I2C_SDA, I2C_SCL = 4, 5
I2C_FREQ = 400_000

SAMPLE_RATE = 400                   # 칩 샘플링(sps)
SAMPLE_AVG = 4                      # 칩 내부 평균 → 실효 100 Hz
LED_AMP = 0x24                      # Red LED 전류(약 7 mA). 포화되면 자동으로 낮춤
FINGER_TH = 30000                   # 이 값 미만이면 손가락 없음으로 판단 (환경에 맞게 조정)
SAT_TH = 250000                     # 18bit 최대 262143 근처 → 포화

LPF_FC = 4.0                        # 저역통과 차단주파수(Hz) — 240 bpm까지 통과
SETTLE_S = 1.5                      # 손가락 감지 후 필터 안정화 시간(s)
MIN_IBI_MS, MAX_IBI_MS = 300, 2000  # 200 bpm ~ 30 bpm
IBI_WINDOW = 8                      # 평균 BPM 계산용 IBI 개수
OUTLIER_RATIO = 0.30                # 중앙값 대비 ±30% 벗어나면 이상치

PLOT = False                        # True: 필터 신호만 출력(Thonny Plotter 확인용)


# ===================== 필터 =====================
class DCBlocker:
    """y[n] = x[n] - x[n-1] + R*y[n-1]  (fs=100 Hz, R=0.98 → 약 0.3 Hz 고역통과)"""
    def __init__(self, r=0.98):
        self.r = r
        self.reset()

    def reset(self):
        self.x1 = None
        self.y1 = 0.0

    def __call__(self, x):
        if self.x1 is None:          # 첫 샘플에서 계단 응답 방지
            self.x1 = x
            return 0.0
        y = x - self.x1 + self.r * self.y1
        self.x1, self.y1 = x, y
        return y


class LowPass:
    """2차 Butterworth 저역통과 (bilinear 변환)"""
    def __init__(self, fc, fs):
        k = math.tan(math.pi * fc / fs)
        s2 = math.sqrt(2)
        norm = 1.0 / (1.0 + s2 * k + k * k)
        self.b0 = k * k * norm
        self.b1 = 2.0 * self.b0
        self.b2 = self.b0
        self.a1 = 2.0 * (k * k - 1.0) * norm
        self.a2 = (1.0 - s2 * k + k * k) * norm
        self.reset()

    def reset(self):
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0

    def __call__(self, x):
        y = (self.b0 * x + self.b1 * self.x1 + self.b2 * self.x2
             - self.a1 * self.y1 - self.a2 * self.y2)
        self.x2, self.x1 = self.x1, x
        self.y2, self.y1 = self.y1, y
        return y


# ===================== 박동 검출 =====================
class BeatDetector:
    """3점 국소 최대 + 적응형 임계값(포락선의 50%) + 불응기.
    피크 시각은 포물선 보간으로 샘플 간격(10 ms)보다 정밀하게 추정."""
    def __init__(self, fs, decay=0.995, ratio=0.5):
        self.fs = fs
        self.decay = decay           # 포락선 감쇠(샘플당). 100 Hz에서 시정수 ≈ 2 s
        self.ratio = ratio
        self.refr = int(MIN_IBI_MS / 1000 * fs)
        self.reset(0.0)

    def reset(self, seed):
        self.p2 = self.p1 = seed
        self.env = 0.0
        self.last_peak = None

    def update(self, y, n):
        """n: 현재 샘플 번호. 박동이면 피크 시각(샘플 단위, 소수) 반환"""
        beat = None
        p2, p1 = self.p2, self.p1
        if p1 > p2 and p1 >= y and p1 > self.ratio * self.env:
            idx = n - 1
            if self.last_peak is None or idx - self.last_peak >= self.refr:
                denom = p2 - 2.0 * p1 + y
                delta = 0.5 * (p2 - y) / denom if denom != 0 else 0.0
                beat = idx + delta
                self.last_peak = idx
        self.env = max(y, self.env * self.decay)
        self.p2, self.p1 = p1, y
        return beat


def median(v):
    s = sorted(v)
    m = len(s) // 2
    return s[m] if len(s) % 2 else 0.5 * (s[m - 1] + s[m])


# ===================== 메인 =====================
def main():
    i2c = SoftI2C(sda=Pin(I2C_SDA), scl=Pin(I2C_SCL), freq=I2C_FREQ)
    if MAX3010X_I2C_ADDRESS not in i2c.scan():
        raise RuntimeError("MAX30102를 찾을 수 없음 (배선/주소 확인)")

    sensor = MAX30102(i2c=i2c)
    if not sensor.check_part_id():
        print("# 경고: Part ID 불일치 — 호환 칩인지 확인")

    # led_mode=1: Red 단독(Heart Rate 모드), 샘플당 3바이트
    sensor.setup_sensor(led_mode=1, adc_range=16384, sample_rate=SAMPLE_RATE,
                        led_power=LED_AMP, sample_avg=SAMPLE_AVG, pulse_width=411)
    fs = sensor.get_acquisition_frequency()
    print("# fs = {:.1f} Hz".format(fs))

    dcb = DCBlocker()
    lpf = LowPass(LPF_FC, fs)
    det = BeatDetector(fs)
    min_refr = int(MIN_IBI_MS / 1000 * fs)

    amp = LED_AMP
    n = 0
    finger = False
    settle = 0
    last_beat = None
    ibis = []
    rejects = 0

    def restart():
        nonlocal settle, last_beat
        dcb.reset(); lpf.reset()
        settle = int(SETTLE_S * fs)
        last_beat = None

    if not PLOT:
        print("t_s,ibi_ms,bpm_inst,bpm_avg")

    try:
        while True:
            sensor.check()
            while sensor.available():
                raw = sensor.pop_red_from_storage()
                n += 1

                # --- 손가락 감지 ---
                if raw < FINGER_TH:
                    if finger:
                        print("# 손가락 없음")
                        finger = False
                    continue
                if not finger:
                    print("# 손가락 감지 — 측정 시작 (움직이지 마세요)")
                    finger = True
                    ibis.clear(); rejects = 0
                    restart()

                # --- 포화 시 LED 전류 감소 ---
                if raw > SAT_TH and amp > 0x04:
                    amp -= 0x04
                    sensor.set_pulse_amplitude_red(amp)
                    print("# 포화 → LED amp = 0x{:02X}".format(amp))
                    restart()
                    continue

                # --- 필터링 (혈액량↑ → 반사광↓ 이므로 부호 반전) ---
                y = -lpf(dcb(raw))
                if PLOT:
                    print(int(y))

                if settle > 0:
                    settle -= 1
                    if settle == 0:
                        det.reset(y)
                    continue

                # --- 박동 검출 ---
                t_peak = det.update(y, n)
                if t_peak is None:
                    continue
                if last_beat is not None:
                    ibi = (t_peak - last_beat) / fs * 1000.0
                    ok = MIN_IBI_MS <= ibi <= MAX_IBI_MS
                    if ok and len(ibis) >= 3:
                        med = median(ibis)
                        ok = abs(ibi - med) / med <= OUTLIER_RATIO
                    if ok:
                        rejects = 0
                        ibis.append(ibi)
                        if len(ibis) > IBI_WINDOW:
                            ibis.pop(0)
                        med = median(ibis)
                        # 불응기를 직전 리듬의 60%로 갱신 (중복박/dicrotic notch 오검출 억제)
                        det.refr = max(min_refr, int(0.6 * med / 1000 * fs))
                        if not PLOT:
                            print("{:.3f},{:.0f},{:.1f},{:.1f}".format(
                                t_peak / fs, ibi, 60000.0 / ibi, 60000.0 / med))
                    else:
                        rejects += 1
                        if rejects >= 3:      # 실제 심박이 크게 변한 경우 기준 재설정
                            ibis.clear(); rejects = 0
                            det.refr = min_refr
                last_beat = t_peak

            sleep_ms(5)
    except KeyboardInterrupt:
        pass
    finally:
        sensor.shutdown()
        print("# 종료")


if __name__ == "__main__":
    main()