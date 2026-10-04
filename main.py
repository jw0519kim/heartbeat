# main.py

import time
import machine
import _thread
import sys

# --- 전역 변수 설정 (두 코어가 공유) ---
# [gsr_raw_value, temperature_celsius, red_data, filtered_data, HEART_RATE_BPM]
sensor_data = [0, 0.0, 0, 0, 0]
data_lock = _thread.allocate_lock()

# 코어 작업 제어 변수 (Core 0에서 제어)
SENSOR_TASK_ENABLED = False
SERIAL_OUTPUT_ENABLED = False
SHOULD_RESET_CORE1 = False

# --- 타이머 상수 (ms) ---
GSR_TEMP_INTERVAL_MS = 1000 # 1초 (GSR, 온도, 시리얼 출력 주기)
HEARTRATE_INTERVAL_MS = 20 # 20ms (심박 연산 주기)
RECORDING_DURATION_MS = 700000 #측정 시간

# --- 핀 설정 ---
GSR_PIN = 28
gsr_adc = machine.ADC(GSR_PIN)
SKINTEMP_I2C_BUS = 0
MAX30205_ADDRESS = 0x48
TEMPERATURE_REGISTER = 0x00
skintemp_i2c = machine.I2C(SKINTEMP_I2C_BUS, sda=machine.Pin(12), scl=machine.Pin(13), freq=100000)
HEARTRATE_I2C_BUS = 1
heartrate_i2c = machine.I2C(HEARTRATE_I2C_BUS, scl=machine.Pin(27), sda=machine.Pin(26), freq=100000)
BUTTON_PIN = 15
button = machine.Pin(BUTTON_PIN, machine.Pin.IN, machine.Pin.PULL_UP)

# --- 심박 센서 드라이버 import ---
try:
    from max30102 import MAX30102
except ImportError:
    print("Warning: max30102.py driver not found.")
    MAX30102 = None

# --- 심박 센서: 기저선 보정 클래스 ---
class BaselineRemover:
    def __init__(self, alpha=0.95):
        self.alpha = alpha
        self.estimated_baseline = 0
    def filter(self, value):
        if self.estimated_baseline == 0:
            self.estimated_baseline = value
        self.estimated_baseline = self.alpha * self.estimated_baseline + (1 - self.alpha) * value
        filtered_value = value - self.estimated_baseline
        return filtered_value
    def reset(self):
        self.estimated_baseline = 0


# ====================================================================
# Core 1: 센서 데이터 수집 및 연산 (백그라운드 작업)
# ====================================================================

def core1_task():
    global sensor_data, data_lock, SENSOR_TASK_ENABLED, SHOULD_RESET_CORE1
    
    # Core 1 전용 로컬 변수
    baseline_remover = BaselineRemover(alpha=0.95)
    last_gsr_temp_time = 0
    last_heartrate_time = 0
    HEART_RATE_BPM = 0
    
    # BPM 연산을 위한 로컬 변수
    MIN_PEAK_THRESHOLD = 30
    PEAK_DEBOUNCE_MS = 500
    last_peak_time = 0
    peak_times = [] # 피크 간 시간 차이를 저장
    MAX_PEAK_COUNT = 5
    previous_filtered_data = 0 # 이전 필터링된 값
    
    # MAX30205 센서 확인
    devices = skintemp_i2c.scan()
    max30205_ok = MAX30205_ADDRESS in devices
    if not max30205_ok:
        print("Core 1: MAX30205 센서를 찾을 수 없습니다. (0x48)")

    # 심박 센서 변수 및 초기화
    sensor = None
    if MAX30102:
        sensor = MAX30102(heartrate_i2c)

    def setup_max30102():
        nonlocal sensor, baseline_remover, HEART_RATE_BPM, last_peak_time, peak_times, previous_filtered_data
        if not sensor: return False
        
        MAX30105_MODE_RED_ONLY = 0x02
        SAMPLING_RATE_VALUE_SPS = 100
        PULSE_WIDTH_VALUE_US = 411
        FIFO_AVERAGE_VALUE = 4
        ADC_RANGE_VALUE_NA = 2048
        
        try:
            sensor.soft_reset()
            sensor.set_led_mode(MAX30105_MODE_RED_ONLY)
            sensor.set_adc_range(ADC_RANGE_VALUE_NA)
            sensor.set_pulse_width(PULSE_WIDTH_VALUE_US)
            sensor.set_sample_rate(SAMPLING_RATE_VALUE_SPS)
            sensor.set_fifo_average(FIFO_AVERAGE_VALUE)
            sensor.set_pulse_amplitude_red(0x1F)
            sensor.set_pulse_amplitude_ir(0x1F)
            sensor.set_pulse_amplitude_green(0x00)
            
            # BPM 로컬 변수 초기화
            baseline_remover.reset()
            HEART_RATE_BPM = 0
            last_peak_time = 0
            peak_times = []
            previous_filtered_data = 0
            
            # 기저선 초기화
            print("Core 1: MAX30102 기저선 초기화 중...")
            init_data_count = 0
            while init_data_count < SAMPLING_RATE_VALUE_SPS * 1:
                sensor.check()
                red_data = sensor.get_red()
                if red_data is not None and red_data > 100:
                    baseline_remover.filter(red_data)
                    init_data_count += 1
                time.sleep_ms(5)
            print("Core 1: 기저선 초기화 완료.")
            return True
        except Exception as e:
            print(f"MAX30102 setup failed: {e}")
            return False

    # 메인 루프
    while True:
        # === SENSOR_TASK_ENABLED가 False일 때: 대기 루프 및 리셋 처리 ===
        if not SENSOR_TASK_ENABLED:
            if SHOULD_RESET_CORE1 and sensor:
                sensor.shutdown()
                SHOULD_RESET_CORE1 = False
                # 센서 재설정 대기
                if MAX30102 and setup_max30102():
                    print("Core 1: 센서 초기화 완료. 버튼 트리거 대기 중...")
            time.sleep_ms(100)
            continue

        # === SENSOR_TASK_ENABLED가 True일 때: 데이터 수집/연산 루프 ===
        current_time = time.ticks_ms()

        # 1. 20ms 주기 센서 (심박) - 데이터 수집, 필터링, BPM 계산
        if MAX30102 and time.ticks_diff(current_time, last_heartrate_time) >= HEARTRATE_INTERVAL_MS:
            
            red_data = 0
            filtered_data = 0
            
            sensor.check()
            red_data_raw = sensor.get_red()
            
            if red_data_raw is not None and red_data_raw > 100:
                red_data = red_data_raw
                # --- 기저선 보정 ---
                filtered_data = baseline_remover.filter(red_data)
                
                # --- 피크 감지 로직 ---
                is_potential_peak = (filtered_data < previous_filtered_data) and \
                                    (previous_filtered_data > MIN_PEAK_THRESHOLD)
                                    
                is_debounce_passed = (current_time - last_peak_time) > PEAK_DEBOUNCE_MS
                
                if is_potential_peak and is_debounce_passed:
                    # --- 심박수 계산 로직 ---
                    if last_peak_time != 0:
                        time_diff_ms = current_time - last_peak_time
                        
                        peak_times.append(time_diff_ms)
                        if len(peak_times) > MAX_PEAK_COUNT:
                            peak_times.pop(0)

                        if len(peak_times) > 0:
                            avg_time_diff = sum(peak_times) / len(peak_times)
                            if avg_time_diff > 0:
                                HEART_RATE_BPM = round(60000 / avg_time_diff)
                    
                    last_peak_time = current_time # 마지막 피크 시간 업데이트
                
                # --- 다음 루프를 위한 데이터 저장 ---
                previous_filtered_data = filtered_data
            
            # 전역 변수 업데이트 (락 사용)
            with data_lock:
                sensor_data[2] = red_data
                sensor_data[3] = int(filtered_data)
                sensor_data[4] = int(HEART_RATE_BPM) # 20ms마다 BPM 업데이트
            
            last_heartrate_time = current_time
            
        # 2. 1초 주기 센서 (GSR, 피부 온도) - 데이터 수집
        if time.ticks_diff(current_time, last_gsr_temp_time) >= GSR_TEMP_INTERVAL_MS:
            
            # --- GSR 데이터 수집 및 연산 ---
            gsr_raw_value = gsr_adc.read_u16()
            
            # --- 피부 온도 데이터 수집 및 연산 ---
            temperature_celsius = 0.0
            if max30205_ok:
                try:
                    data = skintemp_i2c.readfrom_mem(MAX30205_ADDRESS, TEMPERATURE_REGISTER, 2)
                    raw_temp = (data[0] << 8) | data[1]
                    temperature_celsius = raw_temp * 0.00390625
                except Exception as e:
                    # print(f"MAX30205 read error: {e}") # 1초마다 출력 시 노이즈 발생 가능
                    pass
            
            # 전역 변수 업데이트 (락 사용)
            with data_lock:
                sensor_data[0] = round(gsr_raw_value * 3.3 / 65535, 3)
                sensor_data[1] = round(temperature_celsius, 2)
                # BPM은 20ms 주기에서 이미 업데이트됨
            
            last_gsr_temp_time = current_time
            
        time.sleep_ms(5) # 짧은 대기


# ====================================================================
# Core 0: 메인 루프 (버튼 감지 및 시리얼 통신)
# ====================================================================

# 전역 변수 (Core 0 전용)
global_name = ""
global_gender = ""
recording_start_time = 0

def button_handler(pin):
    """인터럽트 핸들러: 버튼이 눌리면 시리얼 출력 상태를 토글합니다."""
    global SERIAL_OUTPUT_ENABLED, recording_start_time, global_name, global_gender
    
    # SENSOR_TASK_ENABLED가 True (준비 상태)일 때만 버튼 처리
    if SENSOR_TASK_ENABLED:
        time.sleep_ms(50) # 디바운싱
        # **[수정사항 1]** 버튼이 눌려 PULL_UP 저항에 의해 Low(0)가 될 때 트리거
        if pin.value() == 0 and not SERIAL_OUTPUT_ENABLED: 
            SERIAL_OUTPUT_ENABLED = True
            recording_start_time = time.ticks_ms()
            
            # 파일명과 헤더를 포함한 트리거 메시지 전송
            # 파일명은 (성별)_(이름).csv
            filename = f"{global_gender}_{global_name}.csv"
            # 출력 항목: GSR_RAW, TEMP_C, BPM
            HEADER = f"피부 전도도_{global_name},피부 온도_{global_name},심박수_{global_name}"
            print(f"TRIGGER_START:{filename}:{HEADER}")
            print(f">> CORE0: 레코딩 시작! {RECORDING_DURATION_MS/1000}초 동안 데이터를 보냅니다.")
    
    else:
        print(">> CORE0: 센서 작업이 시작되지 않았습니다. PC에서 이름과 성별을 입력해주세요.")


def core0_main():
    global sensor_data, data_lock, SENSOR_TASK_ENABLED, SERIAL_OUTPUT_ENABLED, SHOULD_RESET_CORE1, recording_start_time
    global global_name, global_gender
    
    # 버튼 인터럽트 설정
    button.irq(trigger=machine.Pin.IRQ_FALLING, handler=button_handler)
    
    last_serial_time = 0
    
    while True:
        # === 1. PC 입력 대기 및 SENSOR_TASK_ENABLED 설정 ===
        if not SENSOR_TASK_ENABLED:
            SHOULD_RESET_CORE1 = True # Core 1 리셋 요청
            global_name = ""
            global_gender = ""
            SERIAL_OUTPUT_ENABLED = False
            
            print("\n---------------------------------------------------------")
            print("Core 0: PC 입력 대기 중. (터미널에서 '이름:'과 '성별:' 입력 요청을 기다립니다.)")
            
            # 'NAME:' 입력 대기
            while True:
                line = sys.stdin.readline().strip()
                if line.startswith("NAME:"):
                    global_name = line.split(":", 1)[1].strip()
                    print(f"-> 이름 수신: {global_name}")
                    print("OK_NAME") # **[수정사항 2a]** PC 응답 메시지 전송
                    break
                time.sleep_ms(10)
            
            # 'GENDER:' 입력 대기
            while True:
                line = sys.stdin.readline().strip()
                if line.startswith("GENDER:"):
                    global_gender = line.split(":", 1)[1].strip()
                    print(f"-> 성별 수신: {global_gender}")
                    print("OK_GENDER") # **[수정사항 2b]** PC 응답 메시지 전송
                    break
                time.sleep_ms(10)
            
            print("Core 0: 모든 입력 완료. 센서 작업 시작 및 버튼 대기 중...")
            SENSOR_TASK_ENABLED = True # Core 1 센서 작업 시작
            # Core 1이 센서 초기화를 할 시간을 줌
            time.sleep_ms(1500)
            continue

        # === 2. SENSOR_TASK_ENABLED가 True일 때: 데이터 처리 루프 ===
        current_time = time.ticks_ms()

        # 2-1. 시리얼 통신 종료 확인 (500초 타이머)
        if SERIAL_OUTPUT_ENABLED:
            if time.ticks_diff(current_time, recording_start_time) >= RECORDING_DURATION_MS:
                SERIAL_OUTPUT_ENABLED = False
                SENSOR_TASK_ENABLED = False # 코어 리셋 및 PC 입력 대기 상태로 돌아감
                print("TRIGGER_STOP")
                print(f">> CORE0: {RECORDING_DURATION_MS/1000}초 레코딩 완료. 초기 상태로 돌아갑니다.")
                continue

        # 2-2. 1초 주기 시리얼 출력
        if SERIAL_OUTPUT_ENABLED and time.ticks_diff(current_time, last_serial_time) >= GSR_TEMP_INTERVAL_MS:
            
            # 전역 변수 읽기 (락 사용)
            with data_lock:
                # 필요한 데이터: GSR_RAW (0), TEMP_C (1), BPM (4)
                gsr = sensor_data[0]
                temp = sensor_data[1]
                bpm = sensor_data[4]

            # CSV 포맷으로 시리얼 출력 (3개 항목)
            csv_line = f"{gsr},{temp:.2f},{bpm}"
            print(csv_line)
            
            last_serial_time = current_time
            
        time.sleep_ms(10) # CPU 부하 감소

# --- 프로그램 시작 ---
_thread.start_new_thread(core1_task, ()) # Core 1 시작
core0_main() # Core 0 메인 루프 시작
