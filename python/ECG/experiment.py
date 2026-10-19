# -*- coding: utf-8 -*-
"""
호흡 주기·깊이 실험 진행 프로그램 (PC 측)

  python experiment.py                 # 피코 자동 탐색, 전체화면
  python experiment.py --port COM5     # 포트 지정
  python experiment.py --no-sensor     # 센서 없이 화면·로그만 (리허설용)
  python experiment.py --speed 20 --windowed --no-sensor --autopilot   # 빠른 드라이런

피실험자 화면: pygame 전체화면.  실험자 콘솔: 이 터미널.
ESC 를 누르면 즉시 중단(로그에 abort 기록).

출력 (data/<참가자ID>_<일시>/):
  meta.json      참가자, 시드, 세트 순서, 시각 기준, 피코 동기화 정보
  events.csv     모든 단계 시작/종료 시각
  keypress.csv   스페이스바 누른 시각 (어느 단계에서 눌렀는지 포함)
  ecg.csv        AD8232 원시 파형 (피코 틱 + PC 시각)

시각 기준: t_s = PC 단조 시계(perf_counter) 기준 초, 프로그램 시작 = 0.
          meta.json 의 wall_start 가 t_s=0 에 해당하는 실제 시각.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import random
import sys
import threading
import time
from collections import deque
from datetime import datetime

# ---------------------------------------------------------------- 설정 ----
PERIODS_S = [2, 6, 10]                 # 호흡 주기 (들숨+날숨)
DEPTHS = ["shallow", "deep"]           # 호흡 깊이
DEPTH_KO = {"shallow": "얕게", "deep": "깊게"}

REST_INIT_S = 120                      # 초기 안정
BREATH_S = 120                         # 호흡
REPRO_S = 60                           # 시간 재현
REST_S = 120                           # 세트 간 안정
COUNTDOWN_S = 3                        # 호흡 시작 전 카운트다운

PRACTICE_COND = (6, "deep")            # 연습 조건
PRACTICE_BREATH_S = 15
PRACTICE_REPRO_S = 10

# 원 크기 (화면 높이 대비 비율). 깊이에 따라 최대 크기가 다르다.
R_MIN_FRAC = 0.07
R_MAX_FRAC = {"shallow": 0.20, "deep": 0.40}

FPS = 60
BG = (12, 12, 16)
FG = (235, 235, 235)
DIM = (120, 120, 130)
CIRCLE_IN = (70, 150, 255)
CIRCLE_OUT = (90, 200, 160)
RING = (60, 60, 75)

SERIAL_BAUD = 115200
PICO_VID = 0x2E8A
MAINS_HZ = 60                          # 전원 주파수 (한국 60)
# 노치 사양: (중심 Hz, Q, 적용 횟수). 보드 발진은 33.8~34.6 Hz 를 떠돌아(2026-10-19 녹음 6개) 34.15 Hz, Q=4 를 두 번 건다.
# 녹음에서 30-38 Hz 잔류가 34.2/Q6 한 번(0.21)보다 34.15/Q4 두 번(0.065)이 3배 낫고 QRS 대역(5-20 Hz)은 98.5 % 보존.
DEFAULT_NOTCHES = ((MAINS_HZ, 6, 1), (34.15, 4, 2))   # 실시간 HR 검출기의 기본 노치. 저장 데이터는 원시 그대로


def parse_notches(text):
    """'60,34.15:4:2' → ((60,6,1),(34.15,4,2)). 항목은 f 또는 f:Q 또는 f:Q:횟수."""
    out = []
    for item in str(text).split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        f0 = float(parts[0])
        q = float(parts[1]) if len(parts) > 1 else 6.0
        n = int(parts[2]) if len(parts) > 2 else 1
        out.append((f0, q, n))
    return tuple(out)


def notches_to_text(specs):
    return ",".join(f"{f0:g}:{q:g}:{n}" for f0, q, n in specs)
TICKS_PERIOD = 1 << 30                 # MicroPython ticks_ms wrap


class Abort(Exception):
    """ESC, 창 닫기, Ctrl-C 등으로 실험을 중단할 때."""


# ------------------------------------------------------------- 시계/로그 ----
T0 = time.perf_counter()
WALL_START = datetime.now()


def now():
    """프로그램 시작 기준 초 (단조 시계)."""
    return time.perf_counter() - T0


class Logger:
    def __init__(self, outdir):
        self.outdir = outdir
        os.makedirs(outdir, exist_ok=True)
        self.ev_f = open(os.path.join(outdir, "events.csv"), "w", newline="", encoding="utf-8")
        self.ev = csv.writer(self.ev_f)
        self.ev.writerow(["t_s", "wall_iso", "event", "set_idx", "period_s", "depth", "detail"])
        self.kp_f = open(os.path.join(outdir, "keypress.csv"), "w", newline="", encoding="utf-8")
        self.kp = csv.writer(self.kp_f)
        self.kp.writerow(["t_s", "phase", "set_idx", "period_s", "depth"])
        self.lock = threading.Lock()

    def event(self, name, set_idx="", period="", depth="", detail="", t=None):
        t = now() if t is None else t
        with self.lock:
            self.ev.writerow([f"{t:.4f}", datetime.now().isoformat(timespec="milliseconds"),
                              name, set_idx, period, depth, detail])
            self.ev_f.flush()
        print(f"[{t:8.2f}s] {name:<16} set={set_idx} {period} {depth} {detail}")

    def keypress(self, t, phase, set_idx, period, depth):
        with self.lock:
            self.kp.writerow([f"{t:.4f}", phase, set_idx, period, depth])
            self.kp_f.flush()

    def close(self):
        self.ev_f.close()
        self.kp_f.close()


# --------------------------------------------------------- 실시간 R-peak ----
class RPeakDetector:
    """Pan-Tompkins 식 간이 검출기. 실험자 모니터용 추정치이며, 분석은 사후 스크립트로."""

    def __init__(self, fs, notches=DEFAULT_NOTCHES):
        self.fs = fs
        self.notches = tuple(notches)
        # 2차 IIR 노치 (Q=6). 전원 60 Hz 외에 보드 자체 발진(예: 34.2 Hz)도 같이 지울 수 있다.
        self.nf = []
        for f0, q, n in self.notches:
            if 0 < f0 < fs / 2:
                w0 = 2 * math.pi * f0 / fs
                alpha = math.sin(w0) / (2 * q)
                c = math.cos(w0)
                a0 = 1 + alpha
                for _ in range(n):
                    self.nf.append([1 / a0, -2 * c / a0, 1 / a0, -2 * c / a0, (1 - alpha) / a0, 0.0, 0.0, 0.0, 0.0])
        self.dc = None
        self.dc_a = 1.0 - math.exp(-2 * math.pi * 0.7 / fs)     # ~0.7 Hz 고역
        self.lp = 0.0
        self.lp_a = 1.0 - math.exp(-2 * math.pi * 20.0 / fs)    # ~20 Hz 저역
        self.deriv = deque([0.0] * 5, maxlen=5)
        self.win = max(1, int(0.15 * fs))
        self.ibuf = deque([0.0] * self.win, maxlen=self.win)
        self.isum = 0.0
        self.y2 = self.y1 = 0.0
        self.spk = 0.0
        self.npk = 0.0
        self.thr = None
        self.learn = deque(maxlen=int(2.5 * fs))
        self.last_peak = -10.0
        self.rr = deque(maxlen=8)
        self.n = 0

    def reset(self):
        self.__init__(self.fs, self.notches)

    def push(self, t, v):
        self.n += 1
        # 노치 필터들 (직렬)
        for f in self.nf:
            b0, b1, b2, a1, a2, x1, x2, y1, y2 = f
            y = b0 * v + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            f[5], f[6], f[7], f[8] = v, x1, y, y1
            v = y
        # 고역(기준선 제거) + 저역
        if self.dc is None:
            self.dc = v
        self.dc += self.dc_a * (v - self.dc)
        hp = v - self.dc
        self.lp += self.lp_a * (hp - self.lp)
        x = self.lp
        # 미분, 제곱, 적분
        self.deriv.append(x)
        d = (2 * self.deriv[4] + self.deriv[3] - self.deriv[1] - 2 * self.deriv[0]) / 8.0
        sq = d * d
        self.isum += sq - self.ibuf[0]
        self.ibuf.append(sq)
        y = self.isum / self.win
        # 학습 구간
        if self.thr is None:
            self.learn.append(y)
            if len(self.learn) == self.learn.maxlen:
                mx = max(self.learn)
                self.spk = 0.4 * mx
                self.npk = sum(self.learn) / len(self.learn)
                self.thr = self.npk + 0.25 * (self.spk - self.npk)
            self.y2, self.y1 = self.y1, y
            return
        # 국소 최대 검출
        if self.y1 > self.y2 and self.y1 >= y and (t - self.last_peak) > 0.25:
            val = self.y1
            if val > self.thr:
                self.spk = 0.125 * val + 0.875 * self.spk
                if self.last_peak > 0:
                    self.rr.append(t - self.last_peak)      # 걸러내지 않는다. 이상치는 사후 분석에서 처리
                self.last_peak = t
            else:
                self.npk = 0.125 * val + 0.875 * self.npk
            self.thr = self.npk + 0.25 * (self.spk - self.npk)
        self.y2, self.y1 = self.y1, y

    def hr(self):
        """마지막 R-peak 간격 하나로 계산한 순간 심박수 (평균·중앙값 없음)."""
        if not self.rr:
            return None
        return 60.0 / self.rr[-1]


# ----------------------------------------------------------- 센서 스레드 ----
class SensorReader(threading.Thread):
    def __init__(self, port, outdir, logger, notches=DEFAULT_NOTCHES):
        super().__init__(daemon=True)
        import serial
        self.serial = serial
        self.port = port
        self.logger = logger
        self.notches = tuple(notches)
        self.ser = serial.Serial(port, SERIAL_BAUD, timeout=0.2)
        self.f = open(os.path.join(outdir, "ecg.csv"), "w", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        self.w.writerow(["seq", "pico_ms", "t_s", "adc", "lo"])
        self.stop_flag = threading.Event()
        self.fs = 250
        self.offset_s = None          # t_s = pico_ms/1000 + offset_s
        self.sync_rtt_ms = None
        self.n_samples = 0
        self.n_gaps = 0
        self.last_seq = -1
        self.lead_off = False
        self.lead_off_since = None
        self.last_t = None
        self.tick_base = 0
        self.last_ticks = None
        self.det = None
        self.error = None
        self.recent = deque(maxlen=600)   # 최근 2초 ADC 값 (진폭 모니터)

    def _readline(self, timeout=1.0):
        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            line = self.ser.readline()
            if line:
                return line.decode(errors="ignore").strip()
        return None

    def _probe(self, wait=1.5):
        """INFO 를 보내고 응답을 기다린다. 반환: (INFO 줄 또는 None, 그동안 받은 모든 줄)"""
        self.ser.reset_input_buffer()
        self.ser.write(b"INFO\n")
        lines = []
        end = time.perf_counter() + wait
        while time.perf_counter() < end:
            line = self._readline(0.3)
            if line is None:
                continue
            lines.append(line)
            if line.startswith("INFO,"):
                return line, lines
        return None, lines

    def handshake(self):
        time.sleep(0.3)
        self.ser.reset_input_buffer()
        self.ser.write(b"STOP\n")
        time.sleep(0.2)
        info, lines = self._probe()
        if info is None:
            # main.py 가 돌고 있지 않은 상태(REPL). Thonny 에서 Stop 을 눌렀거나 저장 후 재부팅을 안 한 경우.
            # Ctrl-C x2 (중단) -> Ctrl-B (일반 REPL) -> Ctrl-D (소프트 리셋) 로 main.py 를 다시 띄운다.
            print("  피코가 응답하지 않습니다. 소프트 리셋으로 main.py 를 다시 시작합니다...")
            if lines:
                print("  (받은 내용: " + " | ".join(lines[:5]) + ")")
            self.ser.write(b"\x03\x03\x02\x04")
            time.sleep(2.5)
            self.ser.reset_input_buffer()
            self.ser.write(b"STOP\n")
            time.sleep(0.2)
            info, lines = self._probe(wait=3.0)
        if info is None:
            got = " | ".join(lines[:5]) if lines else "없음"
            raise RuntimeError(
                "피코가 INFO 에 응답하지 않음 (받은 내용: %s)\n"
                "  확인: 1) main.py 가 피코 안에 'main.py' 이름으로 저장되어 있는지 (Thonny 저장 위치: Raspberry Pi Pico)\n"
                "        2) Thonny 를 닫았는지\n"
                "        3) 피코 USB 를 뺐다 다시 꽂은 뒤 재시도. Thonny 쉘에서 Stop/Restart 후 'READY,fs=300' 이 보여야 정상" % got)
        for kv in info.split(",")[1:]:
            k, _, v = kv.partition("=")
            if k == "fs":
                self.fs = int(v)
        # 시각 동기화: 왕복 지연이 가장 짧은 시도를 채택
        best = None
        for _ in range(10):
            self.ser.reset_input_buffer()
            t_send = now()
            self.ser.write(b"SYNC\n")
            line = self._readline()
            t_recv = now()
            if not line or not line.startswith("SYNC,"):
                continue
            ticks = int(line.split(",")[1])
            rtt = t_recv - t_send
            if best is None or rtt < best[0]:
                best = (rtt, (t_send + t_recv) / 2 - ticks / 1000.0)
            time.sleep(0.02)
        if best is None:
            raise RuntimeError("피코가 INFO 에는 응답했지만 SYNC 에 응답하지 않음 (main.py 의 handle() 확인)")
        self.sync_rtt_ms, self.offset_s = best[0] * 1000.0, best[1]
        self.det = RPeakDetector(self.fs, self.notches)
        self.recent = deque(maxlen=2 * self.fs)
        self.ser.write(b"START\n")

    def _unwrap(self, ticks):
        if self.last_ticks is not None and ticks < self.last_ticks - (TICKS_PERIOD // 2):
            self.tick_base += TICKS_PERIOD
        self.last_ticks = ticks
        return ticks + self.tick_base

    def run(self):
        try:
            last_flush = time.perf_counter()
            while not self.stop_flag.is_set():
                line = self.ser.readline()
                if not line:
                    continue
                s = line.decode(errors="ignore").strip()
                if not s.startswith("D,"):
                    continue
                try:
                    _, seq, ticks, adc, lo = s.split(",")
                    seq, ticks, adc, lo = int(seq), int(ticks), int(adc), int(lo)
                except ValueError:
                    continue
                if self.last_seq >= 0 and seq != self.last_seq + 1:
                    self.n_gaps += 1
                self.last_seq = seq
                ms = self._unwrap(ticks)
                t = ms / 1000.0 + self.offset_s
                self.w.writerow([seq, ms, f"{t:.4f}", adc, lo])
                self.n_samples += 1
                self.last_t = t
                self.recent.append(adc)
                if lo:
                    if not self.lead_off:
                        self.lead_off = True
                        self.lead_off_since = t
                        self.logger.event("lead_off", detail="on", t=t)
                        self.det.reset()
                else:
                    if self.lead_off:
                        self.lead_off = False
                        self.logger.event("lead_off", detail="off", t=t)
                    self.det.push(t, adc)
                if time.perf_counter() - last_flush > 1.0:
                    self.f.flush()
                    last_flush = time.perf_counter()
        except Exception as e:      # noqa
            self.error = repr(e)
            self.logger.event("sensor_error", detail=self.error)

    def close(self):
        self.stop_flag.set()
        try:
            self.ser.write(b"STOP\n")
            time.sleep(0.1)
            self.ser.close()
        except Exception:
            pass
        self.f.flush()
        self.f.close()

    def status(self):
        hr = self.det.hr() if self.det else None
        s = f"ECG {self.n_samples} samples"
        if self.n_gaps:
            s += f" gaps={self.n_gaps}"
        s += "  HR=" + (f"{hr:5.1f}" if hr else "  ---")
        if self.recent:
            lo_v, hi_v = min(self.recent), max(self.recent)
            s += f"  adc {lo_v}..{hi_v} (pp {hi_v - lo_v})"
            if lo_v < 500 or hi_v > 65000:
                s += " SATURATED?"
        if self.lead_off:
            s += "  ** LEAD OFF **"
        if self.last_t is not None and now() - self.last_t > 2.0:
            s += "  ** NO DATA **"
        if self.error:
            s += "  ERROR " + self.error
        return s


def find_pico_port():
    from serial.tools import list_ports
    ports = list(list_ports.comports())
    for p in ports:
        if p.vid == PICO_VID:
            return p.device
    if len(ports) == 1:
        return ports[0].device
    names = ", ".join(f"{p.device}({p.description})" for p in ports) or "없음"
    raise RuntimeError(f"피코 포트를 찾지 못함. 사용 가능 포트: {names}. --port 로 지정하세요.")


# ------------------------------------------------------------------ UI ----
class UI:
    def __init__(self, windowed):
        import pygame
        self.pg = pygame
        pygame.init()
        pygame.display.set_caption("Breathing experiment")
        if windowed:
            self.screen = pygame.display.set_mode((1280, 800))
        else:
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
            pygame.mouse.set_visible(False)
        self.W, self.H = self.screen.get_size()
        self.clock = pygame.time.Clock()
        name = None
        for cand in ["malgungothic", "malgun gothic", "nanumgothic", "applegothic", "gulim", "notosanscjkkr"]:
            if pygame.font.match_font(cand):
                name = cand
                break
        if name is None:
            print("경고: 한글 폰트를 찾지 못했습니다. 글자가 깨질 수 있습니다.")
        self.font_big = pygame.font.SysFont(name, int(self.H * 0.11))
        self.font_mid = pygame.font.SysFont(name, int(self.H * 0.055))
        self.font_small = pygame.font.SysFont(name, int(self.H * 0.035))
        self.font_timer = pygame.font.SysFont(name, int(self.H * 0.06))

    def text(self, s, font, color, cx, cy):
        lines = s.split("\n")
        h = font.get_linesize()
        y = cy - h * (len(lines) - 1) / 2
        for ln in lines:
            surf = font.render(ln, True, color)
            self.screen.blit(surf, surf.get_rect(center=(cx, y)))
            y += h

    def clear(self):
        self.screen.fill(BG)

    def timer_topright(self, secs, prefix=""):
        m, s = divmod(int(secs), 60)
        surf = self.font_timer.render(f"{prefix}{m:02d}:{s:02d}", True, DIM)
        self.screen.blit(surf, surf.get_rect(topright=(self.W - 40, 30)))

    def flip(self):
        self.pg.display.flip()
        self.clock.tick(FPS)

    def quit(self):
        self.pg.quit()


# --------------------------------------------------------------- 실험 ----
class Experiment:
    def __init__(self, args):
        self.args = args
        self.speed = args.speed
        self.pid = args.participant or input("참가자 ID: ").strip() or "anon"
        seed = args.seed if args.seed is not None else int(hashlib.sha256(self.pid.encode()).hexdigest()[:8], 16)
        self.seed = seed
        rng = random.Random(seed)
        self.order = [(p, d) for p in PERIODS_S for d in DEPTHS]
        rng.shuffle(self.order)
        if args.sets:
            self.order = self.order[:args.sets]
        # 구간 길이. --quick 은 데이터 형식 확인용 짧은 프로토콜 (호흡 주기는 그대로).
        self.D = {"rest_init": REST_INIT_S, "breath": BREATH_S, "repro": REPRO_S, "rest": REST_S,
                  "countdown": COUNTDOWN_S, "practice_breath": PRACTICE_BREATH_S, "practice_repro": PRACTICE_REPRO_S}
        if args.quick:
            self.D.update({"rest_init": 10, "breath": 20, "repro": 10, "rest": 10,
                           "practice_breath": 10, "practice_repro": 5})

        stamp = WALL_START.strftime("%Y%m%d_%H%M%S")
        self.outdir = os.path.join(args.outdir, f"{self.pid}_{stamp}")
        self.log = Logger(self.outdir)

        self.sensor = None
        if not args.no_sensor:
            port = args.port or find_pico_port()
            print(f"피코 연결: {port}")
            self.sensor = SensorReader(port, self.outdir, self.log, parse_notches(args.notch))
            self.sensor.handshake()
            self.sensor.start()
            print(f"  fs={self.sensor.fs} Hz  sync rtt={self.sensor.sync_rtt_ms:.1f} ms  offset={self.sensor.offset_s:.4f} s")

        self.ui = UI(args.windowed)
        self.phase = "idle"
        self.set_idx = ""
        self.cond = ("", "")
        self.aborted = False
        self.last_status = 0.0
        self.write_meta("running")

    # -------- 메타
    def write_meta(self, status):
        meta = {
            "participant": self.pid,
            "seed": self.seed,
            "order": [{"set": i + 1, "period_s": p, "depth": d} for i, (p, d) in enumerate(self.order)],
            "wall_start": WALL_START.isoformat(timespec="milliseconds"),
            "speed_factor": self.speed,
            "durations_s": dict(self.D),
            "quick": bool(self.args.quick),
            "sets_run": len(self.order),
            "practice_cond": {"period_s": PRACTICE_COND[0], "depth": PRACTICE_COND[1]},
            "r_max_frac": R_MAX_FRAC, "r_min_frac": R_MIN_FRAC,
            "sensor": None if self.sensor is None else {
                "port": self.sensor.port, "fs": self.sensor.fs,
                "offset_s": self.sensor.offset_s, "sync_rtt_ms": self.sensor.sync_rtt_ms},
            "status": status,
        }
        with open(os.path.join(self.outdir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    # -------- 공통
    def dur(self, s):
        return s / self.speed

    def poll_events(self):
        """키 입력 처리. 스페이스는 항상 기록(단계 표시 포함). ESC 는 중단."""
        pg = self.ui.pg
        space = False
        for ev in pg.event.get():
            if ev.type == pg.QUIT:
                raise Abort("window_close")
            if ev.type == pg.KEYDOWN:
                if ev.key == pg.K_ESCAPE:
                    raise Abort("esc")
                if ev.key == pg.K_SPACE:
                    t = now()
                    self.log.keypress(t, self.phase, self.set_idx, self.cond[0], self.cond[1])
                    space = True
        return space

    def console_status(self, remaining):
        if now() - self.last_status < 5.0:
            return
        self.last_status = now()
        s = f"  [{self.phase}] set={self.set_idx} 남은 {remaining:6.1f}s"
        if self.sensor:
            s += "   " + self.sensor.status()
        print(s)

    def begin(self, phase, set_idx="", cond=("", ""), detail=""):
        self.phase, self.set_idx, self.cond = phase, set_idx, cond
        self.log.event(phase + "_start", set_idx, cond[0], cond[1], detail)

    def end(self, phase):
        self.log.event(phase + "_end", self.set_idx, self.cond[0], self.cond[1])
        self.phase = "idle"

    # -------- 화면들
    def wait_space(self, phase, text, sub=""):
        self.begin(phase)
        t0 = now()
        while True:
            if self.poll_events():
                break
            if self.args.autopilot and now() - t0 > 1.0 / self.speed:
                break
            self.ui.clear()
            self.ui.text(text, self.ui.font_mid, FG, self.ui.W / 2, self.ui.H * 0.42)
            if sub:
                self.ui.text(sub, self.ui.font_small, DIM, self.ui.W / 2, self.ui.H * 0.62)
            self.ui.text("준비되면 스페이스바를 누르세요", self.ui.font_small, DIM, self.ui.W / 2, self.ui.H * 0.85)
            self.ui.flip()
        self.end(phase)

    def rest(self, secs, set_idx, label="편안히 쉬세요"):
        self.begin("rest", set_idx)
        t_end = now() + self.dur(secs)
        while True:
            rem = t_end - now()
            if rem <= 0:
                break
            self.poll_events()
            self.ui.clear()
            self.ui.text(label, self.ui.font_big, FG, self.ui.W / 2, self.ui.H * 0.45)
            self.ui.timer_topright(math.ceil(rem * self.speed), "남은 시간 ")
            self.ui.flip()
            self.console_status(rem)
        self.end("rest")

    def countdown(self, set_idx, cond):
        period, depth = cond
        self.begin("countdown", set_idx, cond)
        t_end = now() + self.dur(COUNTDOWN_S)
        while True:
            rem = t_end - now()
            if rem <= 0:
                break
            self.poll_events()
            self.ui.clear()
            self.ui.text(f"다음: 주기 {period}초, {DEPTH_KO[depth]} 호흡", self.ui.font_mid, FG,
                         self.ui.W / 2, self.ui.H * 0.25)
            self.ui.text(str(math.ceil(rem * self.speed)), self.ui.font_big, FG, self.ui.W / 2, self.ui.H * 0.5)
            self.ui.text("원이 커지면 들숨, 작아지면 날숨", self.ui.font_small, DIM, self.ui.W / 2, self.ui.H * 0.75)
            self.ui.flip()
        self.end("countdown")

    def breathe(self, secs, set_idx, cond):
        period, depth = cond
        self.begin("breath", set_idx, cond)
        ui = self.ui
        cx, cy = ui.W / 2, ui.H * 0.52
        r_min = R_MIN_FRAC * ui.H
        r_max = R_MAX_FRAC[depth] * ui.H
        t_start = now()
        t_end = t_start + self.dur(secs)
        half = self.dur(period) / 2.0
        while True:
            t = now()
            rem = t_end - t
            if rem <= 0:
                break
            self.poll_events()
            elapsed = t - t_start
            pos = elapsed % self.dur(period)
            if pos < half:
                f = pos / half
                label, col = "들숨", CIRCLE_IN
            else:
                f = 1.0 - (pos - half) / half
                label, col = "날숨", CIRCLE_OUT
            f = (1.0 - math.cos(math.pi * f)) / 2.0          # 부드러운 가감속
            r = r_min + (r_max - r_min) * f
            ui.clear()
            ui.pg.draw.circle(ui.screen, RING, (cx, cy), int(r_max), 2)      # 목표(최대) 크기
            ui.pg.draw.circle(ui.screen, col, (cx, cy), int(r))
            ui.text(label, ui.font_big, FG, cx, ui.H * 0.12)
            ui.text(f"주기 {period}초  ·  {DEPTH_KO[depth]} 호흡하세요", ui.font_small, DIM, cx, ui.H * 0.93)
            ui.timer_topright(elapsed * self.speed)                              # 카운트업
            rem_real = rem * self.speed
            if rem_real <= 3.0:                                                  # 마지막 3초: 3, 2, 1
                ui.text(str(math.ceil(rem_real)), ui.font_big, FG, cx, cy)
                ui.text("곧 기억 호흡으로", ui.font_small, DIM, cx, ui.H * 0.80)
            ui.flip()
            self.console_status(rem)
        self.end("breath")

    def reproduce(self, secs, set_idx, cond):
        self.begin("repro", set_idx, cond)
        t_end = now() + self.dur(secs)
        msg = ("방금의 호흡 리듬과 깊이를 기억에 의존해 이어가면서\n"
               "1초 간격으로 스페이스바를 누르세요")
        next_fake = now() + self.dur(1.0)
        while True:
            rem = t_end - now()
            if rem <= 0:
                break
            if self.args.autopilot and now() >= next_fake:     # 리허설: 1초마다 가짜 스페이스
                self.ui.pg.event.post(self.ui.pg.event.Event(self.ui.pg.KEYDOWN, key=self.ui.pg.K_SPACE))
                next_fake += self.dur(1.0)
            self.poll_events()                      # 피드백 없음
            self.ui.clear()
            self.ui.text(msg, self.ui.font_mid, FG, self.ui.W / 2, self.ui.H * 0.40)
            rem_real = rem * self.speed
            if rem_real <= 3.0:                                  # 마지막 3초: 3, 2, 1
                self.ui.text(str(math.ceil(rem_real)), self.ui.font_big, FG, self.ui.W / 2, self.ui.H * 0.66)
            self.ui.flip()
            self.console_status(rem)
        self.end("repro")

    # -------- 전체 흐름
    def run(self):
        try:
            self.log.event("program_start", detail=f"pid={self.pid} seed={self.seed} order={self.order}")
            self.wait_space("intro", "호흡과 심박수 실험",
                            "전극이 잘 붙어 있는지 확인한 뒤 시작합니다")
            # 연습
            self.wait_space("practice_intro",
                            "연습을 먼저 합니다",
                            "원이 커지면 들숨, 작아지면 날숨\n"
                            "원이 사라지면 리듬을 기억해 이어가면서 1초마다 스페이스바를 누릅니다")
            D = self.D
            self.countdown("P", PRACTICE_COND)
            self.breathe(D["practice_breath"], "P", PRACTICE_COND)
            self.reproduce(D["practice_repro"], "P", PRACTICE_COND)
            n = len(self.order)
            total_min = (D["rest_init"] + (D["countdown"] + D["breath"] + D["repro"] + D["rest"]) * n - D["rest"]) / 60
            self.wait_space("main_intro", "연습이 끝났습니다",
                            f"이제 본 실험을 시작합니다 (약 {total_min:.0f}분)\n질문이 있으면 지금 말씀해 주세요")
            # 본 실험
            self.log.event("main_start")
            self.rest(D["rest_init"], 0)
            for i, cond in enumerate(self.order, start=1):
                self.countdown(i, cond)
                self.breathe(D["breath"], i, cond)
                self.reproduce(D["repro"], i, cond)
                if i < n:
                    self.rest(D["rest"], i)
            self.log.event("main_end")
            self.phase = "done"
            self.wait_space("done", "실험이 끝났습니다", "수고하셨습니다")
        except Abort as e:
            self.aborted = True
            self.log.event("abort", self.set_idx, self.cond[0], self.cond[1], f"phase={self.phase} reason={e}")
        except KeyboardInterrupt:
            self.aborted = True
            self.log.event("abort", self.set_idx, self.cond[0], self.cond[1], f"phase={self.phase} reason=ctrl_c")
        finally:
            self.log.event("program_end")
            if self.sensor:
                self.sensor.close()
            self.write_meta("aborted" if self.aborted else "completed")
            self.log.close()
            self.ui.quit()
            print(f"\n저장 위치: {self.outdir}")


def main():
    ap = argparse.ArgumentParser(description="호흡 주기·깊이 실험 진행")
    ap.add_argument("--participant", "-p", help="참가자 ID (생략 시 입력 요청)")
    ap.add_argument("--port", help="피코 시리얼 포트 (예: COM5). 생략 시 자동 탐색")
    ap.add_argument("--no-sensor", action="store_true", help="센서 없이 진행")
    ap.add_argument("--windowed", action="store_true", help="전체화면 대신 창 모드")
    ap.add_argument("--speed", type=float, default=1.0, help="리허설용 시간 배속 (예: 20)")
    ap.add_argument("--seed", type=int, help="세트 순서 시드 강제 지정")
    ap.add_argument("--outdir", default="data", help="저장 폴더 (기본 data/)")
    ap.add_argument("--autopilot", action="store_true", help="리허설: 대기 화면 자동 진행, 재현 구간에 가짜 키 입력")
    ap.add_argument("--quick", action="store_true",
                    help="짧은 프로토콜 (안정 10s, 호흡 20s, 재현 10s). 호흡 주기와 데이터 형식은 본 실험과 동일")
    ap.add_argument("--sets", type=int, help="세트 수 제한 (예: --sets 2). 순서는 본 실험과 같은 시드로 뽑은 앞부분")
    ap.add_argument("--notch", default=notches_to_text(DEFAULT_NOTCHES),
                    help="실시간 HR 추정용 노치 목록 f[:Q[:횟수]] (기본 %(default)s). 저장 데이터는 원시 그대로")
    args = ap.parse_args()
    if args.quick or args.sets:
        print(f"주의: 짧은 실행 모드 (quick={args.quick}, sets={args.sets})")
    if args.speed != 1.0:
        print(f"주의: {args.speed}배속 리허설 모드")
    Experiment(args).run()


if __name__ == "__main__":
    main()
