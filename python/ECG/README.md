# 호흡 주기·깊이 × 심박수 실험 프로그램

피코 2W(마이크로파이썬)가 AD8232 ECG를 300 Hz로 샘플링해 USB 시리얼로 보내고,
PC(Python + pygame)가 피실험자 화면, 스페이스바 기록, 모든 이벤트 로깅을 맡는다.

```
main.py               피코 펌웨어 (Thonny 로 피코에 main.py 로 저장)
experiment.py         실험 진행 프로그램 (PC)
monitor.py            실시간 ECG 파형 뷰어 (실험 전 전극 상태 확인)
analyze.py            사후 분석: R-peak, 단계별 HR, 스페이스바 간격
data/                 세션별 결과 (참가자ID_일시)
```

## 0. 전체 워크플로우

### A. 한 번만 (장비 준비)
1. 배선(1절)대로 AD8232 를 피코 2W 에 연결. 3V3 에 연결했는지, SDN 은 비워 뒀는지 확인.
2. Thonny 로 `main.py` 를 피코에 `main.py` 이름으로 저장 → Stop/Restart 했을 때 쉘에 `READY,fs=300` 이 보이면 됨. Thonny 종료.
3. PC: `pip install pygame pyserial numpy` (그래프까지 보려면 `matplotlib`).
4. `python experiment.py -p rehearsal --no-sensor --windowed --quick --sets 1 --autopilot` 로 화면 흐름을 한 번 봐 둔다.
5. **34 Hz 발진 해결 (6-1절)**: `python monitor.py` 로 클리핑 0 %, 주 성분이 심박(1~2 Hz) 또는 낮은 비중이 될 때까지 하드웨어를 손본다. 이게 끝나기 전의 실험 데이터는 쓸 수 없다.

### B. 참가자마다 (약 45분)
| 단계 | 할 일 | 시간 |
|---|---|---|
| 1 | 참가자 설명, 동의, 휴대폰 무음. 피부를 알코올로 닦고 새 젤 전극 3개 부착(RA, LA, RL) | 5분 |
| 2 | `python monitor.py` 로 신호 확인: 클리핑 0 %, R-peak 눈금이 박동마다, 순간 HR 이 상식적 범위. 아니면 전극 다시 | 2~3분 |
| 3 | monitor 를 ESC 로 끄고 `python experiment.py` 실행 → 참가자 ID 입력 → 피코 연결·동기화 메시지 확인 → 전체화면 | 1분 |
| 4 | 안내 → 연습(카운트다운 3초 → 호흡 15초 → 기억 호흡+스페이스 10초). 참가자가 규칙을 이해했는지 확인 후 스페이스로 본 실험 시작 | 2분 |
| 5 | 본 실험 30분: 안정 2분 → [카운트다운 3초 → 호흡 2분(끝 3초 카운트다운) → 기억 호흡+스페이스 1분(끝 3초 카운트다운) → 안정 2분] × 6. 실험자는 콘솔의 5초 간격 상태줄(단계, 남은 시간, 순간 HR, 진폭, LEAD OFF)만 지켜본다 | 30분 |
| 6 | 종료 화면에서 스페이스 → 프로그램이 닫히며 `저장 위치: data/<ID>_<일시>` 출력. 콘솔 상태줄에 `gaps=` 가 떴거나 LEAD OFF 가 있었으면 메모 | 1분 |
| 7 | 바로 `python analyze.py data/<ID>_<일시>/` 로 표를 띄워 박동 수·HR 이 모든 단계에 들어 있는지 확인. 비어 있으면 그 자리에서 원인 파악(전극, 포화) | 2분 |

중단이 필요하면 ESC. `events.csv` 에 `abort` 와 당시 단계가 남고 그때까지의 데이터는 모두 저장된다. 다시 하려면 새 ID(예: `P03b`)로 처음부터.

### C. 전체 참가자 분석
1. `python analyze.py data/*/ --plot` → 세션마다 `summary_phases.csv`, `summary_keypress.csv`, `rpeaks.csv`, `hr_timeline.png`.
2. `summary_phases.csv` 를 모두 합쳐(참가자 열 추가) 세트 조건(period_s, depth)별 호흡 구간 HR, 안정 대비 변화량을 비교한다. 같은 조건이 참가자마다 다른 순서로 나오므로 `set_idx` 가 아니라 `period_s`+`depth` 로 묶는다.
3. 시간 재현은 `summary_keypress.csv` 의 `mean_s`(1초에 얼마나 가까운가)와 `cv`(얼마나 고른가)를 조건별로 비교한다.
4. RR 이상치는 `rpeaks.csv` 의 `rr_bad` 로 표시되어 있고 단계별 HR 계산에서는 이미 제외돼 있다. 기준을 바꾸려면 `analyze.py` 의 `flag_rr` 을 고친다.

### 파일 흐름
```
main.py(피코) --USB 시리얼--> experiment.py / monitor.py --> data/<ID>_<일시>/{meta.json, events.csv, keypress.csv, ecg.csv}
                                                               --> analyze.py --> rpeaks.csv, summary_phases.csv, summary_keypress.csv, hr_timeline.png
```

## 1. 배선 (AD8232 → 피코 2W)

| AD8232 | 피코 2W |
|---|---|
| 3.3V | 3V3 (pin 36) |
| GND | GND |
| OUTPUT | GP26 (ADC0) |
| LO+ | GP16 |
| LO- | GP17 |

전극은 AD8232 보드의 RA / LA / RL 패드 순서대로.

## 2. 설치

피코: Thonny 에서 `main.py` 를 열어 "Raspberry Pi Pico" 에 `main.py` 로 저장. 전원을 다시 꽂으면 자동 실행된다.
피코는 PC 가 `START` 를 보낼 때까지 조용히 대기하므로 Thonny 의 쉘이 메시지로 가득 차지 않는다.
**실험 전에 Thonny 를 닫아야 한다** (시리얼 포트를 Thonny 가 잡고 있으면 PC 프로그램이 열 수 없다).

PC:

```bash
pip install pygame pyserial numpy
```

그래프까지 보려면 `pip install matplotlib`.

## 3. 실험 진행

```bash
python experiment.py
```

1. 콘솔에 참가자 ID 입력 → 피코 자동 탐색, 시각 동기화 → 전체화면.
2. 화면 흐름: 안내 → 연습(카운트다운 3초 → 호흡 15초 → 재현 10초) → 본 실험 시작 안내
   → 초기 안정 2분 → [카운트다운 3초 → 호흡 2분 → 시간 재현 1분 → 안정 2분] × 6 (마지막 세트는 안정 없음) → 종료.
3. 안내 화면은 스페이스바로 넘어간다. 실험 중 ESC 를 누르면 즉시 중단되며 `abort` 이벤트가 기록된다.
4. 실험자 콘솔에는 단계 전환, 5초마다 현재 단계·남은 시간·추정 심박수·ADC 진폭·lead-off 가 찍힌다.

옵션:

| 옵션 | 뜻 |
|---|---|
| `-p ID` | 참가자 ID (콘솔 입력 생략) |
| `--port COM5` | 피코 포트 지정 (자동 탐색 실패 시) |
| `--no-sensor` | 센서 없이 화면·로그만 |
| `--windowed` | 창 모드 |
| `--speed 20` | 리허설용 배속 (모든 구간이 1/20 로 짧아짐) |
| `--autopilot` | 리허설: 안내 화면 자동 통과, 재현 구간에 가짜 키 입력 |
| `--seed N` | 세트 순서 시드 강제 (기본은 참가자 ID 로부터 결정) |

리허설 예:

```bash
python experiment.py -p rehearsal --no-sensor --windowed --speed 20 --autopilot
```

## 3-1. 실험 전 신호 확인 (monitor.py)

```bash
python monitor.py
```

최근 6초 파형, 추정 심박수, 최근 2초 ADC 진폭(pp), 5분 진폭 추세, lead-off 를 한 창에 보여준다.
전극을 붙인 뒤 이 창에서 R-peak 눈금이 또박또박 찍히는지, 진폭이 시간에 따라 줄어드는지 확인하고 실험을 시작한다.
키: `a` 자동 스케일/전체 범위, `f` 전원 잡음 노치 켜기/끄기, `ESC` 종료. 원시 데이터는 `data/monitor_<일시>/ecg.csv` 에 저장된다.

## 3-2. 30분을 다 돌리지 않고 데이터 형식 확인하기

```bash
python experiment.py -p test --quick --sets 2
```

`--quick` 은 안정 10초, 호흡 20초, 재현 10초로 줄이되 호흡 주기와 파일 형식은 본 실험과 같다. `--sets 2` 는 앞 2세트만 돈다 (약 2분).
센서를 붙인 채 돌리면 ecg.csv 까지 실제 데이터로 생기고, 바로 `python analyze.py data/test_*/` 로 결과 표를 볼 수 있다.

## 4. 결과 파일 (`data/<ID>_<일시>/`)

| 파일 | 내용 |
|---|---|
| `meta.json` | 참가자, 시드, 세트 순서, 시작 실제 시각(`wall_start`), 피코 동기화 오프셋 |
| `events.csv` | 모든 단계 `_start`/`_end` 시각, lead-off 전환, abort |
| `keypress.csv` | 스페이스바 누른 시각. 어느 단계·세트에서 눌렀는지 포함 (재현 구간 밖의 입력도 남긴다) |
| `ecg.csv` | 원시 파형: `seq, pico_ms, t_s, adc(16bit), lo` |

모든 시각 `t_s` 는 PC 단조 시계 기준 초(프로그램 시작 = 0). ECG 의 `t_s` 는 피코 틱을 시작 시 동기화한 오프셋으로 PC 시각에 맞춘 값이다
(동기화 왕복 지연은 `meta.json` 의 `sync_rtt_ms`, 보통 수 ms). `seq` 가 건너뛰면 샘플 유실이며 콘솔에 `gaps=` 로 표시된다.

## 5. 사후 분석

```bash
python analyze.py data/<ID>_<일시>/ --plot
```

세션 폴더에 `rpeaks.csv`, `summary_phases.csv`(세트×단계별 평균 HR, SDNN, RMSSD, 박동 수),
`summary_keypress.csv`(재현 구간별 간격 평균·SD·CV), `keypress_intervals.csv`, `hr_timeline.png` 가 생긴다.
여러 세션은 `data/*/` 로 한 번에.

처리: 전원 잡음 노치(fs/60 샘플 이동평균) → 5–20 Hz 대역통과 → Pan-Tompkins 식 R-peak → RR 이상치 플래그(0.3–2 s 밖, 국소 중앙값 ±30%).
콘솔의 "잡음 점검" 줄은 원시 신호 20–150 Hz 대역에서 가장 큰 성분의 주파수를 알려준다. 맥놀이가 보이면 이 값으로 간섭원 주파수를 특정할 수 있다.

## 6. 신호 관련 메모

### 6-1. 34 Hz 발진 (2026-10-19 확인)

monitor 로 받은 실제 데이터(`data/monitor_20261019_104307`)는 ADC 전 범위(48..64767)를 오가는 **약 34.2 Hz 정현파**가 지배하고, 샘플의 14 % 가 아래쪽 끝값에 붙어 있었다(클리핑).
예전 코드의 137 Hz + 4샘플 평균은 137/4 = 34.25 Hz 에 이동평균 노치가 생겨 이 발진을 우연히 지운 것이다. 발진이 커져 증폭기가 포화되면 노치로도 복구되지 않고 파형이 평평해진다.
즉 **파형이 점점 일직선이 되는 것과 30 ms 주기 파동은 같은 원인**이다.

어떤 디지털 필터도 끝값에 붙은(클리핑된) 샘플은 되살리지 못하므로 아날로그 쪽에서 원인을 없애야 한다. monitor 의 "주 성분"과 "클리핑" 표시로 아래 테스트를 하나씩 한다. 각 20초면 충분하다.

| # | 조건 | 발진이 그대로면 | 발진이 사라지면 |
|---|---|---|---|
| 1 | 센서 케이블을 보드에서 뽑음 (LEAD OFF 표시는 무시) | 보드·전원·배선 문제 → 2a | 전극·몸 쪽 문제 → 3 |
| 2a | main.py 의 `WL_GPIO1` 줄을 지우고(무선칩 전원 안 켬) 재부팅 | 2b | 무선칩 전원 간섭 |
| 2b | AD8232 의 3.3V-GND 사이에 10 µF + 0.1 µF, GND 를 피코 AGND(33번 핀)에, OUTPUT 선을 짧게 | 보드 불량 의심 | 전원·접지 잡음 |
| 3 | 전극 3개를 젖은 휴지 한 장에 같이 붙임 (전극 임피던스 0 에 가깝게) | 케이블·RLD 루프 문제 → 4 | 피부 접촉 문제: 새 젤 전극, 피부를 알코올로 닦고 문지름, RL 전극을 단단히 |
| 4 | 몸에 붙인 채 노트북 충전기 분리(배터리로) | 그대로면 보드 쪽 | 접지 루프 |

AD8232 가 5 V(VBUS)가 아니라 3V3 에 연결되어 있는지, SDN 핀에 아무것도 연결하지 않았는지도 확인한다.

**발진 주파수 적합 (녹음 6개, 총 5분)**: 클리핑된 샘플을 빼고 정현파를 최소제곱으로 맞추면 5초 창마다 33.8~34.6 Hz 사이를 떠돈다
(오전 녹음 평균 34.25, 오후 33.95). 고정된 한 값이 아니므로 노치는 이 범위를 통째로 덮어야 한다.
후보를 녹음에 적용해 30-38 Hz 잔류를 비교한 결과 **34.15 Hz, Q=4 를 두 번** 거는 것이 34.2/Q6 한 번보다 3배 좋았다(잔류 0.065 vs 0.21, QRS 대역 98.5 % 보존).

실시간 HR 추정(experiment.py, monitor.py)과 사후 분석(analyze.py)은 기본으로 60 Hz(Q6) 와 34.15 Hz(Q4, 2회) IIR 노치를 건다
(`experiment.py` 의 `DEFAULT_NOTCHES`, `analyze.py` 의 `NOTCH_HZ`; `--notch 60,34.15:4:2` 형식). 발진이 하드웨어에서 없어지면 34.15 항목을 빼면 된다.
저장 데이터는 원시 그대로다. 노치로 심박이 검출되더라도 포화(클리핑)가 있는 동안의 값은 믿을 수 없다.

### 6-2. 샘플링
- **300 Hz, 평균 없음.** 노치는 PC 에서 IIR 로. 50 Hz 환경이면 `MAINS_HZ` 와 `NOTCH_HZ` 의 60 을 50 으로.
- 피실험자가 30분 동안 움직이지 않도록 하고, 전극은 실험 직전에 새로 붙인다. 실험 전 monitor 로 클리핑 0 %, 주 성분이 1~2 Hz(심박) 또는 낮은 비중인지 확인한다.

## 7. 설정값 바꾸기

`experiment.py` 상단: 구간 길이(`REST_INIT_S`, `BREATH_S`, `REPRO_S`, `REST_S`, `COUNTDOWN_S`), 연습 길이, 원 크기(`R_MIN_FRAC`, `R_MAX_FRAC`), 색.
`main.py` 상단: `FS`.
