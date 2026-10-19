# -*- coding: utf-8 -*-
"""
실시간 ECG 파형 뷰어 (실험 전 전극 상태 확인용)

  python monitor.py                # 피코 자동 탐색
  python monitor.py --port COM9

창 구성
  위:   심박수, 최근 2초 ADC 진폭(pp), lead-off, 수신 샘플 수, 경과 시간
  중간: 최근 6초 파형 (R-peak 에 세로 눈금)
  아래: 진폭(pp) 추세 5분  ->  시간에 따라 진폭이 줄어드는지 바로 보인다

  python monitor.py --notch 60          # 노치 지정 f[:Q[:횟수]] (기본은 experiment.py 의 DEFAULT_NOTCHES)

키:  a  자동 스케일 / 전체 범위(0..65535) 전환
     f  노치 필터 켜기/끄기 (화면 표시에만 적용, 저장 데이터는 원시 그대로)
     ESC 종료

상단에 최근 2초의 주 성분 주파수와 클리핑 비율(ADC 끝값에 붙은 샘플 %)을 보여준다.
클리핑이 0% 가 아니면 증폭기가 포화된 것이라 어떤 필터로도 복구되지 않는다.

원시 데이터는 data/monitor_<일시>/ecg.csv 에 그대로 저장된다 (analyze.py 로 분석 가능).
"""

import argparse
import math
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from experiment import SensorReader, RPeakDetector, find_pico_port, now, DEFAULT_NOTCHES, parse_notches, notches_to_text   # noqa: E402

WINDOW_S = 6.0            # 파형 표시 구간
TREND_S = 300.0           # 진폭 추세 구간
BG = (12, 12, 16)
FG = (235, 235, 235)
DIM = (120, 120, 130)
GRID = (40, 40, 50)
TRACE = (90, 200, 160)
PEAK = (255, 120, 90)
WARN = (255, 90, 90)


def notch_coeffs(f0, fs, q=6.0):
    """2차 IIR 노치 (Audio EQ cookbook). 반환 (b0,b1,b2,a1,a2), a0 정규화."""
    w0 = 2 * math.pi * f0 / fs
    alpha = math.sin(w0) / (2 * q)
    c = math.cos(w0)
    a0 = 1 + alpha
    return (1 / a0, -2 * c / a0, 1 / a0, -2 * c / a0, (1 - alpha) / a0)


def apply_notches(x, coeffs_list):
    y = x
    for (b0, b1, b2, a1, a2) in coeffs_list:        # 사양의 횟수만큼 이미 펼쳐져 있다
        out = np.empty_like(y)
        x1 = x2 = y1 = y2 = 0.0
        for i in range(len(y)):
            xi = y[i]
            yi = b0 * xi + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2, x1, y2, y1 = x1, xi, y1, yi
            out[i] = yi
        y = out
    return y


def dominant_freq(x, fs, lo=1.0, hi=150.0):
    """최근 구간의 주 성분 주파수와 그 비중(전체 AC 파워 대비)."""
    x = x - x.mean()
    if len(x) < fs or not np.any(x):
        return None, 0.0
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    f = np.fft.rfftfreq(len(x), 1 / fs)
    band = (f >= lo) & (f <= min(hi, fs / 2))
    if not band.any():
        return None, 0.0
    i = np.argmax(spec[band])
    share = spec[band][i] / (spec[band].sum() + 1e-12)
    return float(f[band][i]), float(share)


class PrintLogger:
    def event(self, name, set_idx="", period="", depth="", detail="", t=None):
        print(f"[{(now() if t is None else t):8.2f}s] {name} {detail}")


class ReplayReader(threading.Thread):
    """저장된 ecg.csv 를 실시간으로 재생해 SensorReader 처럼 보이게 한다 (--replay)."""

    def __init__(self, path, notches=DEFAULT_NOTCHES, speed=1.0):
        super().__init__(daemon=True)
        self.notches = tuple(notches)
        arr = np.genfromtxt(path, delimiter=",", skip_header=1)
        self.t_src, self.adc, self.lo = arr[:, 2], arr[:, 3], arr[:, 4]
        self.fs = int(round((len(self.t_src) - 1) / (self.t_src[-1] - self.t_src[0])))
        self.speed = speed
        self.port = "replay:" + os.path.basename(os.path.dirname(path))
        self.sync_rtt_ms = 0.0
        self.recent = deque(maxlen=2 * self.fs)
        self.det = None
        self.last_t = None
        self.lead_off = False
        self.n_samples = 0
        self.n_gaps = 0
        self.error = None
        self.stop_flag = threading.Event()

    def handshake(self):
        self.det = RPeakDetector(self.fs, self.notches)

    def run(self):
        t0 = now()
        base = self.t_src[0]
        for i in range(len(self.adc)):
            while not self.stop_flag.is_set() and (self.t_src[i] - base) / self.speed > now() - t0:
                time.sleep(0.002)
            if self.stop_flag.is_set():
                return
            t = t0 + (self.t_src[i] - base) / self.speed
            self.recent.append(self.adc[i])
            self.last_t = t
            self.n_samples += 1
            self.lead_off = bool(self.lo[i])
            if not self.lead_off:
                self.det.push(t, self.adc[i])
        self.error = "재생 끝"

    def close(self):
        self.stop_flag.set()


def main():
    ap = argparse.ArgumentParser(description="실시간 ECG 뷰어")
    ap.add_argument("--port")
    ap.add_argument("--outdir", default="data")
    ap.add_argument("--notch", default=notches_to_text(DEFAULT_NOTCHES),
                    help="노치 목록 f[:Q[:횟수]], 쉼표 구분 (기본 %(default)s). 표시와 HR 추정에만 적용")
    ap.add_argument("--replay", help="피코 대신 저장된 ecg.csv 를 재생")
    args = ap.parse_args()
    notch_freqs = parse_notches(args.notch)

    if args.replay:
        outdir = None
        reader = ReplayReader(args.replay, notch_freqs)
        print(f"재생: {args.replay}")
    else:
        port = args.port or find_pico_port()
        outdir = os.path.join(args.outdir, "monitor_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
        os.makedirs(outdir, exist_ok=True)
        print(f"피코 연결: {port}   저장: {outdir}")
        reader = SensorReader(port, outdir, PrintLogger(), notch_freqs)
    reader.handshake()
    fs = reader.fs
    n_win = int(WINDOW_S * fs)
    reader.recent = deque(maxlen=n_win)          # 파형 버퍼를 표시 구간 길이로
    reader.start()
    print(f"  fs={fs} Hz  sync rtt={reader.sync_rtt_ms:.1f} ms")

    import pygame
    pygame.init()
    screen = pygame.display.set_mode((1280, 720), pygame.RESIZABLE)
    pygame.display.set_caption("ECG monitor")
    fname = next((c for c in ["malgungothic", "malgun gothic", "nanumgothic", "gulim"] if pygame.font.match_font(c)), None)
    f_big = pygame.font.SysFont(fname, 56)
    f_mid = pygame.font.SysFont(fname, 26)
    f_small = pygame.font.SysFont(fname, 18)
    clock = pygame.time.Clock()

    autoscale = True
    notch = True
    notch_coeffs_list = [notch_coeffs(f0, fs, q) for f0, q, n in notch_freqs if 0 < f0 < fs / 2 for _ in range(n)]
    notch_label = ",".join(f"{f0:g}" for f0, q, n in notch_freqs)
    last_fft = 0.0
    dom_f, dom_share, clip_pct = None, 0.0, 0.0
    peaks = deque(maxlen=64)          # 최근 R-peak 시각
    last_peak_seen = -1.0
    trend = deque()                   # (t, pp)
    last_trend = 0.0
    t0 = now()

    running = True
    while running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE:
                    running = False
                elif ev.key == pygame.K_a:
                    autoscale = not autoscale
                elif ev.key == pygame.K_f:
                    notch = not notch
        W, H = screen.get_size()
        screen.fill(BG)

        # ---- 데이터 스냅샷
        buf = np.fromiter(reader.recent, dtype=float, count=len(reader.recent))
        det = reader.det
        if det is not None and det.last_peak > last_peak_seen:
            peaks.append(det.last_peak)
            last_peak_seen = det.last_peak
        hr = det.hr() if det else None
        t_now = reader.last_t if reader.last_t is not None else now()
        pp = None
        if len(buf) >= fs:
            tail = buf[-2 * fs:]
            pp = float(tail.max() - tail.min())
            if now() - last_trend >= 1.0:
                trend.append((now(), pp))
                last_trend = now()
                while trend and now() - trend[0][0] > TREND_S:
                    trend.popleft()
            if now() - last_fft >= 0.5:
                dom_f, dom_share = dominant_freq(tail, fs)
                clip_pct = 100.0 * float(((tail < 200) | (tail > 65300)).mean())
                last_fft = now()

        # ---- 상단 상태
        y = 16
        screen.blit(f_big.render(f"HR {hr:5.1f}" if hr else "HR  ---", True, FG), (24, y))
        col = FG
        if pp is not None and (tail.min() < 500 or tail.max() > 65000):
            col = WARN
        screen.blit(f_mid.render(f"진폭 pp {int(pp) if pp is not None else '---'}", True, col), (320, y + 8))
        screen.blit(f_mid.render(f"{int(tail.min())}..{int(tail.max())}" if pp is not None else "", True, DIM), (320, y + 38))
        if dom_f is not None:
            screen.blit(f_mid.render(f"주 성분 {dom_f:5.1f} Hz ({dom_share * 100:.0f}%)", True, FG), (560, y + 8))
        screen.blit(f_mid.render(f"클리핑 {clip_pct:.1f}%", True, WARN if clip_pct > 0.5 else DIM), (560, y + 38))
        if reader.lead_off:
            screen.blit(f_mid.render("LEAD OFF", True, WARN), (860, y + 8))
        elif reader.last_t is not None and now() - reader.last_t > 2.0:
            screen.blit(f_mid.render("NO DATA", True, WARN), (860, y + 8))
        if reader.error:
            screen.blit(f_small.render("ERROR " + reader.error, True, WARN), (860, y + 40))
        info = f"samples {reader.n_samples}  gaps {reader.n_gaps}  fs {fs}  경과 {int(now() - t0) // 60:02d}:{int(now() - t0) % 60:02d}"
        screen.blit(f_small.render(info, True, DIM), (W - 24 - f_small.size(info)[0], y + 4))
        mode = f"[a] {'자동 스케일' if autoscale else '전체 범위'}   [f] 노치 {notch_label} Hz {'ON' if notch else 'OFF'}   [ESC] 종료"
        screen.blit(f_small.render(mode, True, DIM), (W - 24 - f_small.size(mode)[0], y + 28))

        # ---- 파형
        top, bottom = 90, int(H * 0.72)
        left, right = 24, W - 24
        pygame.draw.rect(screen, GRID, (left, top, right - left, bottom - top), 1)
        for k in range(1, int(WINDOW_S)):
            x = left + (right - left) * k / WINDOW_S
            pygame.draw.line(screen, GRID, (x, top), (x, bottom))
        if len(buf) > 2:
            sig = buf
            if notch and notch_coeffs_list:
                sig = apply_notches(sig - sig.mean(), notch_coeffs_list) + sig.mean()
            if autoscale:
                lo, hi = float(sig.min()), float(sig.max())
                if hi - lo < 50:
                    mid = (hi + lo) / 2
                    lo, hi = mid - 25, mid + 25
                pad = (hi - lo) * 0.08
                lo, hi = lo - pad, hi + pad
            else:
                lo, hi = 0.0, 65535.0
            n = len(sig)
            xs = left + (right - left) * (np.arange(n) + (n_win - n)) / n_win
            ys = bottom - (bottom - top) * (sig - lo) / (hi - lo)
            pts = np.column_stack([xs, ys]).astype(int)
            if len(pts) > 1:
                pygame.draw.lines(screen, TRACE, False, pts.tolist(), 2)
            # R-peak 눈금 (최근 WINDOW_S 초 안의 것만)
            for tp in peaks:
                age = t_now - tp + 0.09       # 검출기 지연(적분창 150 ms 의 절반 등) 보정
                if 0 <= age <= WINDOW_S:
                    x = right - (right - left) * age / WINDOW_S
                    pygame.draw.line(screen, PEAK, (x, top), (x, top + 14), 2)
            screen.blit(f_small.render(f"{int(hi)}", True, DIM), (left + 4, top + 2))
            screen.blit(f_small.render(f"{int(lo)}", True, DIM), (left + 4, bottom - 20))
        else:
            screen.blit(f_mid.render("데이터 대기 중...", True, DIM), (left + 20, (top + bottom) // 2))

        # ---- 진폭 추세
        t_top, t_bot = bottom + 30, H - 24
        pygame.draw.rect(screen, GRID, (left, t_top, right - left, t_bot - t_top), 1)
        screen.blit(f_small.render(f"진폭(pp) 추세, 최근 {int(TREND_S // 60)}분", True, DIM), (left + 6, t_top + 4))
        if len(trend) > 1:
            tv = np.array(trend)
            vmax = max(100.0, float(tv[:, 1].max()) * 1.1)
            xs = right - (right - left) * (now() - tv[:, 0]) / TREND_S
            ys = t_bot - (t_bot - t_top) * tv[:, 1] / vmax
            pts = np.column_stack([xs, ys]).astype(int)
            pygame.draw.lines(screen, TRACE, False, pts.tolist(), 2)
            screen.blit(f_small.render(f"{int(vmax)}", True, DIM), (right - 60, t_top + 4))

        pygame.display.flip()
        clock.tick(30)

    reader.close()
    pygame.quit()
    if outdir:
        print(f"저장: {outdir}")


if __name__ == "__main__":
    main()
