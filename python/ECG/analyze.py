# -*- coding: utf-8 -*-
"""
사후 분석: R-peak 검출, 단계별 심박수, 스페이스바 간격 통계

  python analyze.py data/<참가자>_<일시>/            # 한 세션
  python analyze.py data/*/                          # 여러 세션
  python analyze.py <폴더> --plot                    # HR 그래프 PNG 저장 (matplotlib 필요)

출력 (각 세션 폴더 안):
  rpeaks.csv             R-peak 시각(t_s)과 RR 간격, 이상치 플래그
  summary_phases.csv     단계(세트×호흡/재현/안정)별 평균 HR, SDNN, RMSSD, 박동 수, lead-off 비율
  summary_keypress.csv   시간 재현 구간별 스페이스바 간격 평균/표준편차/CV
  keypress_intervals.csv 모든 스페이스바 간격
  hr_timeline.png        (--plot) 심박수 추이와 단계 구분

필요: numpy. matplotlib 는 --plot 일 때만.
"""

import argparse
import csv
import glob
import json
import math
import os
import sys

import numpy as np

NOTCH_HZ = (60.0, 34.2)          # 전원 + AD8232 보드 발진. experiment.py 의 DEFAULT_NOTCHES 와 맞춘다
BP_LO, BP_HI = 5.0, 20.0          # QRS 검출용 대역통과 (Hz)


# ------------------------------------------------------------- 필터 ----
def biquad(b, a, x):
    """direct-form II transposed, 계수 정규화됨."""
    y = np.zeros_like(x, dtype=float)
    z1 = z2 = 0.0
    b0, b1, b2 = b
    a1, a2 = a[1], a[2]
    for i in range(len(x)):
        xi = x[i]
        yi = b0 * xi + z1
        z1 = b1 * xi - a1 * yi + z2
        z2 = b2 * xi - a2 * yi
        y[i] = yi
    return y


def butter2(fc, fs, kind):
    """2차 버터워스 (Audio EQ cookbook, Q=1/sqrt2)."""
    w0 = 2 * math.pi * fc / fs
    alpha = math.sin(w0) / (2 * math.sqrt(0.5))
    c = math.cos(w0)
    if kind == "lp":
        b = [(1 - c) / 2, 1 - c, (1 - c) / 2]
    else:
        b = [(1 + c) / 2, -(1 + c), (1 + c) / 2]
    a = [1 + alpha, -2 * c, 1 - alpha]
    return [bi / a[0] for bi in b], [ai / a[0] for ai in a]


def filtfilt2(fc, fs, kind, x):
    b, a = butter2(fc, fs, kind)
    y = biquad(b, a, x)
    y = biquad(b, a, y[::-1])[::-1]
    return y


def notch_biquad(f0, fs, q=6.0):
    w0 = 2 * math.pi * f0 / fs
    alpha = math.sin(w0) / (2 * q)
    c = math.cos(w0)
    a = [1 + alpha, -2 * c, 1 - alpha]
    b = [1.0, -2 * c, 1.0]
    return [bi / a[0] for bi in b], [ai / a[0] for ai in a]


def mains_notch(x, fs, freqs=NOTCH_HZ):
    """IIR 노치(영위상, 앞뒤 두 번)를 주파수마다 직렬로."""
    for f0 in freqs:
        if 0 < f0 < fs / 2:
            b, a = notch_biquad(f0, fs)
            x = biquad(b, a, biquad(b, a, x)[::-1])[::-1]
    return x


# --------------------------------------------------------- R-peak ----
def detect_rpeaks(t, x, fs):
    """Pan-Tompkins 계열. 반환: R-peak 시각 배열."""
    x = x.astype(float)
    x = mains_notch(x, fs)
    x = x - np.mean(x)
    bp = filtfilt2(BP_HI, fs, "lp", filtfilt2(BP_LO, fs, "hp", x))
    d = np.gradient(bp) * fs
    sq = d * d
    win = max(1, int(0.15 * fs))
    integ = np.convolve(sq, np.ones(win) / win, mode="same")

    refr = int(0.25 * fs)
    # 국소 최대
    cand = np.where((integ[1:-1] > integ[:-2]) & (integ[1:-1] >= integ[2:]))[0] + 1
    peaks = []
    learn = integ[: int(3 * fs)]
    spk = 0.4 * learn.max() if len(learn) else 0.0
    npk = float(np.mean(learn)) if len(learn) else 0.0
    thr = npk + 0.25 * (spk - npk)
    last = -10 ** 9
    for i in cand:
        if i - last < refr:
            continue
        v = integ[i]
        if v > thr:
            spk = 0.125 * v + 0.875 * spk
            peaks.append(i)
            last = i
        else:
            npk = 0.125 * v + 0.875 * npk
        thr = npk + 0.25 * (spk - npk)
    # R 위치 정밀화: 대역통과 신호의 ±60 ms 내 최대 절댓값
    half = int(0.06 * fs)
    r_idx = []
    for p in peaks:
        lo, hi = max(0, p - half), min(len(bp), p + half + 1)
        seg = np.abs(bp[lo:hi])
        r_idx.append(lo + int(np.argmax(seg)))
    r_idx = np.unique(np.array(r_idx, dtype=int))
    return t[r_idx]


def flag_rr(rr):
    """생리적으로 불가능하거나 국소 중앙값에서 30% 이상 벗어난 RR 을 이상치로."""
    bad = (rr < 0.3) | (rr > 2.0)
    n = len(rr)
    for i in range(n):
        lo, hi = max(0, i - 5), min(n, i + 6)
        med = np.median(rr[lo:hi])
        if med > 0 and abs(rr[i] - med) / med > 0.3:
            bad[i] = True
    return bad


# ---------------------------------------------------------- 로드 ----
def load_session(folder):
    with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    events = []
    with open(os.path.join(folder, "events.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            r["t_s"] = float(r["t_s"])
            events.append(r)
    keys = []
    with open(os.path.join(folder, "keypress.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            r["t_s"] = float(r["t_s"])
            keys.append(r)
    ecg = None
    p = os.path.join(folder, "ecg.csv")
    if os.path.exists(p) and os.path.getsize(p) > 100:
        arr = np.genfromtxt(p, delimiter=",", skip_header=1, usecols=(2, 3, 4))
        if arr.ndim == 2 and len(arr) > 10:
            ecg = {"t": arr[:, 0], "adc": arr[:, 1], "lo": arr[:, 2].astype(bool)}
    return meta, events, keys, ecg


def phases_from_events(events):
    """(phase, set_idx, period, depth, t_start, t_end) 목록. 연습(P)과 intro 류는 제외."""
    out = []
    open_ = {}
    for e in events:
        name = e["event"]
        if name.endswith("_start"):
            open_[name[:-6]] = e
        elif name.endswith("_end"):
            ph = name[:-4]
            s = open_.pop(ph, None)
            if s is None:
                continue
            if ph in ("breath", "repro", "rest", "countdown") and s["set_idx"] != "P":
                out.append({"phase": ph, "set_idx": s["set_idx"], "period_s": s["period_s"],
                            "depth": s["depth"], "t_start": s["t_s"], "t_end": e["t_s"]})
    return out


# -------------------------------------------------------- 분석 ----
def analyze(folder, plot=False):
    meta, events, keys, ecg = load_session(folder)
    print(f"\n=== {folder}  participant={meta['participant']}  status={meta['status']}")
    phases = phases_from_events(events)
    fs = (meta.get("sensor") or {}).get("fs", 300)

    # ---- R-peak
    r_t = np.array([])
    rr = np.array([])
    bad = np.array([], dtype=bool)
    if ecg is not None:
        # 실제 fs 추정 (틱 기준)
        span = ecg["t"][-1] - ecg["t"][0]          # 피코 틱이 1 ms 단위라 중앙값 간격은 쓰지 않는다
        fs_est = (len(ecg["t"]) - 1) / span if span > 0 else fs
        if abs(fs_est - fs) / fs > 0.05:
            print(f"  주의: 메타 fs={fs} 인데 실측 샘플 간격은 {fs_est:.1f} Hz")
        fs = fs_est
        r_t = detect_rpeaks(ecg["t"], ecg["adc"], fs)
        rr = np.diff(r_t)
        bad = flag_rr(rr) if len(rr) else np.array([], dtype=bool)
        with open(os.path.join(folder, "rpeaks.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t_s", "rr_s", "rr_bad"])
            for i, t in enumerate(r_t):
                w.writerow([f"{t:.4f}", f"{rr[i-1]:.4f}" if i > 0 else "", int(bad[i-1]) if i > 0 else ""])
        lo_frac = float(np.mean(ecg["lo"]))
        print(f"  ECG {len(ecg['t'])} samples, {len(ecg['t'])/fs/60:.1f} min, fs~{fs:.1f}, "
              f"R-peaks {len(r_t)}, RR 이상치 {int(bad.sum())}/{len(rr)}, lead-off {lo_frac*100:.1f}%")
        noise_report(ecg, fs)
    else:
        print("  ecg.csv 없음 (센서 없이 진행한 세션)")

    # ---- 단계별 HR
    rows = []
    for ph in phases:
        if ph["phase"] == "countdown":
            continue
        row = dict(ph)
        row["dur_s"] = ph["t_end"] - ph["t_start"]
        if len(rr):
            m = (r_t[1:] >= ph["t_start"]) & (r_t[1:] < ph["t_end"]) & ~bad
            sel = rr[m]
            row["n_beats"] = int(m.sum())
            if len(sel) >= 3:
                row["hr_mean"] = 60.0 / sel.mean()
                row["sdnn_ms"] = sel.std(ddof=1) * 1000
                row["rmssd_ms"] = np.sqrt(np.mean(np.diff(sel) ** 2)) * 1000 if len(sel) > 3 else ""
            else:
                row["hr_mean"] = row["sdnn_ms"] = row["rmssd_ms"] = ""
            lm = (ecg["t"] >= ph["t_start"]) & (ecg["t"] < ph["t_end"])
            row["lead_off_frac"] = float(ecg["lo"][lm].mean()) if lm.any() else ""
        rows.append(row)
    cols = ["set_idx", "phase", "period_s", "depth", "t_start", "t_end", "dur_s",
            "n_beats", "hr_mean", "sdnn_ms", "rmssd_ms", "lead_off_frac"]
    with open(os.path.join(folder, "summary_phases.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (f"{v:.3f}" if isinstance(v, float) else v) for k, v in r.items()})
    def fmt(v, w=6):
        return f"{v:{w}.1f}" if isinstance(v, float) else f"{str(v):>{w}}"
    print(f"  {'set':>3} {'phase':<7} {'per':>3} {'depth':<8} {'dur':>6} {'beats':>5} {'HR':>6} {'SDNN':>6} {'RMSSD':>6}")
    for r in rows:
        print(f"  {str(r['set_idx']):>3} {r['phase']:<7} {str(r['period_s']):>3} {r['depth']:<8} {r['dur_s']:6.1f} "
              f"{str(r.get('n_beats', '')):>5} {fmt(r.get('hr_mean', ''))} {fmt(r.get('sdnn_ms', ''))} {fmt(r.get('rmssd_ms', ''))}")

    # ---- 스페이스바 간격
    krows, ivals = [], []
    for ph in phases:
        if ph["phase"] != "repro":
            continue
        ts = np.array(sorted(k["t_s"] for k in keys
                             if k["phase"] == "repro" and k["set_idx"] == ph["set_idx"]))
        iv = np.diff(ts)
        for i, v in enumerate(iv):
            ivals.append([ph["set_idx"], ph["period_s"], ph["depth"], f"{ts[i+1]:.4f}", f"{v:.4f}"])
        krows.append({"set_idx": ph["set_idx"], "period_s": ph["period_s"], "depth": ph["depth"],
                      "n_press": len(ts), "n_intervals": len(iv),
                      "mean_s": iv.mean() if len(iv) else "", "sd_s": iv.std(ddof=1) if len(iv) > 1 else "",
                      "cv": (iv.std(ddof=1) / iv.mean()) if len(iv) > 1 and iv.mean() > 0 else "",
                      "median_s": float(np.median(iv)) if len(iv) else ""})
    with open(os.path.join(folder, "summary_keypress.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["set_idx", "period_s", "depth", "n_press", "n_intervals",
                                          "mean_s", "sd_s", "cv", "median_s"])
        w.writeheader()
        for r in krows:
            w.writerow({k: (f"{v:.4f}" if isinstance(v, float) else v) for k, v in r.items()})
    with open(os.path.join(folder, "keypress_intervals.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["set_idx", "period_s", "depth", "t_s", "interval_s"])
        w.writerows(ivals)
    print("  스페이스바:")
    for r in krows:
        m, cv = r["mean_s"], r["cv"]
        print(f"    set {r['set_idx']} ({r['period_s']}s {r['depth']}): {r['n_press']} presses, "
              f"mean {m if m == '' else f'{m:.3f}'} s, cv {cv if cv == '' else f'{cv:.3f}'}")

    if plot and len(rr):
        make_plot(folder, r_t, rr, bad, phases)


def noise_report(ecg, fs):
    """원시 신호 10초 구간 스펙트럼에서 20-150 Hz 대역의 가장 큰 잡음 성분을 보고."""
    n = int(10 * fs)
    if len(ecg["adc"]) < n:
        return
    seg = ecg["adc"][len(ecg["adc"]) // 2: len(ecg["adc"]) // 2 + n]
    seg = seg - seg.mean()
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    freqs = np.fft.rfftfreq(len(seg), 1 / fs)
    band = (freqs >= 20) & (freqs <= min(150, fs / 2))
    if not band.any():
        return
    i = np.argmax(spec[band])
    f_peak = freqs[band][i]
    ratio = spec[band][i] / (np.median(spec[(freqs >= 1) & (freqs <= 20)]) + 1e-9)
    print(f"  잡음 점검: 20-150 Hz 대역 최대 성분 {f_peak:.1f} Hz (QRS 대역 중앙값 대비 x{ratio:.1f})")


def make_plot(folder, r_t, rr, bad, phases):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib 없음: pip install matplotlib")
        return
    fig, ax = plt.subplots(figsize=(14, 4))
    hr = 60.0 / rr
    ax.plot(r_t[1:][~bad], hr[~bad], ".", ms=3, color="k", label="HR")
    ax.plot(r_t[1:][bad], hr[bad], "x", ms=4, color="r", label="flagged")
    colors = {"breath": "#cfe8ff", "repro": "#ffe9c6", "rest": "#e6e6e6", "countdown": "#ffd0d0"}
    for ph in phases:
        ax.axvspan(ph["t_start"], ph["t_end"], color=colors.get(ph["phase"], "#eee"), alpha=0.6, lw=0)
        if ph["phase"] == "breath":
            ytop = hr[~bad].max() if (~bad).any() else 100
            ax.text((ph["t_start"] + ph["t_end"]) / 2, ytop, f"{ph['period_s']}s {ph['depth']}",
                    ha="center", va="top", fontsize=8)
    ax.set_xlabel("t (s)")
    ax.set_ylabel("HR (bpm)")
    ax.legend(loc="lower right")
    fig.tight_layout()
    out = os.path.join(folder, "hr_timeline.png")
    fig.savefig(out, dpi=120)
    print(f"  그래프 저장: {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folders", nargs="+")
    ap.add_argument("--plot", action="store_true")
    args = ap.parse_args()
    folders = []
    for pat in args.folders:
        folders += [p for p in glob.glob(pat) if os.path.isdir(p)]
    if not folders:
        sys.exit("세션 폴더를 찾지 못함")
    for f in sorted(folders):
        analyze(f, plot=args.plot)


if __name__ == "__main__":
    main()
