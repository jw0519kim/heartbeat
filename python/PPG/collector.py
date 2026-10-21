# collector.py

import serial
import time
import csv
import sys
import os
from datetime import datetime

# --- 설정 ---
# ⚠️ 중요: 윈도우에서는 이 포트 이름을 'COMX' (예: 'COM3')로 수정해야 합니다.
SERIAL_PORT = 'COM3' 
BAUD_RATE = 115200
EXPECTED_FEILDS = 1

# **[수정사항 4]** CSV 파일 저장 경로 설정
# 경로의 백슬래시(\)는 Python 문자열에서 이스케이프해야 하므로 두 번( \\ ) 사용합니다.
SAVE_PATH = "C:\\jaewon_kim\\02_pico_projects\\9_heartbeat\\data"

def get_user_info():
    """사용자로부터 이름과 성별을 입력받습니다."""
    print("\n=========================================================")
    print("Pico 데이터 수집 프로그램 (PC)")
    print("=========================================================")
    name = input("측정할 사람의 이름을 입력하세요: ").replace(" ", "_").strip()
    gender = input("측정할 사람의 성별을 입력하세요: ").replace(" ", "_").strip()
    return name, gender

def wait_for_pico_response(ser, expected_response):
    """**[수정사항 3]** Pico로부터 특정 응답이 올 때까지 대기합니다."""
    print(f"Pico 응답 대기 중: '{expected_response}'...")
    start_time = time.time()
    while time.time() - start_time < 5: # 5초 타임아웃
        try:
            line_bytes = ser.readline()
            if not line_bytes: continue
            line = line_bytes.decode('utf-8').strip()
            
            if line == expected_response:
                print(f"✅ 응답 수신: {line}")
                return True
            elif line.startswith(("->", "Core 0:", "Core 1:", "Warning:")):
                print(f"Pico Status: {line}")
        except Exception as e:
            print(f"응답 대기 중 오류 발생: {e}")
            break
    print(f"❌ '{expected_response}' 응답 타임아웃.")
    return False

def collect_data(ser):
    """시리얼 포트에서 데이터를 읽고 CSV 파일에 저장합니다."""

    is_recording = False
    current_filename = ""
    csv_file = None
    csv_writer = None
    row_count = 0

    while True:
        # 1. Pico 준비 대기
        if not is_recording:
            try:
                # 사용자 정보 입력
                ser.flushOutput() # 출력 버퍼 비우기
                name, gender = get_user_info()

                # A. NAME 전송 및 응답 대기
                ser.write(f"NAME:{name}\r\n".encode('utf-8'))
                time.sleep(0.1)
                if not wait_for_pico_response(ser, "OK_NAME"):
                    print("NAME 전송 실패. 재시도.")
                    continue
                
                # B. GENDER 전송 및 응답 대기
                ser.write(f"GENDER:{gender}\r\n".encode('utf-8'))
                time.sleep(0.1)
                if not wait_for_pico_response(ser, "OK_GENDER"):
                    print("GENDER 전송 실패. 재시도.")
                    continue

                print("Pico 준비 상태로 전환 완료. GP15 버튼 트리거를 기다리는 중...")
                ser.flushInput() # Pico가 보낸 이전 에러 메시지를 지우고 깨끗하게 시작

            except Exception as e:
                print(f"사용자 정보 처리 중 오류 발생: {e}")
                time.sleep(1)
                continue

            # 2. 시리얼 데이터 수신 및 처리 (TRIGGER_START 대기)
            while not is_recording:
                try:
                    line_bytes = ser.readline()
                    if not line_bytes: continue
                    line = line_bytes.decode('utf-8').strip()

                    if line.startswith("TRIGGER_START:"):
                        # 포맷: TRIGGER_START:파일명.csv:헤더
                        parts = line.split(":", 2)
                        current_filename_only = parts[1]
                        header_line = parts[2]
                        
                        # **[수정사항 4 반영]** 절대 경로와 파일명 결합
                        full_filepath = os.path.join(SAVE_PATH, current_filename_only)

                        # CSV 파일 열기 및 헤더 작성
                        # **주의: 파일이 해당 경로에 성공적으로 저장되는지 확인하세요.**
                        csv_file = open(full_filepath, mode='w', newline='')
                        csv_writer = csv.writer(csv_file)

                        csv_writer.writerow(["Time"] + header_line.split(','))
                        csv_file.flush()

                        is_recording = True
                        row_count = 0
                        print(f"✅ 레코딩 시작! -> {full_filepath}에 저장 중...")
                        break # 레코딩 루프로 진입
                    elif line:
                        # 기타 Pico 상태 메시지 출력
                        print(f"Pico Status: {line}")
                except KeyboardInterrupt:
                    print("\n프로그램을 종료합니다.")
                    return
                except Exception as e:
                    print(f"대기 중 오류 발생: {e}")
                    time.sleep(1)


        # 3. 레코딩 중
        while is_recording:
            try:
                line_bytes = ser.readline()
                if not line_bytes: continue
                line = line_bytes.decode('utf-8').strip()

                if line == "TRIGGER_STOP":
                    print(f"🛑 레코딩 중지. ({current_filename_only}에 총 {row_count}행 저장)")
                    is_recording = False
                    if csv_file:
                        csv_file.close()
                        csv_file = None
                    break # PC 입력 대기 루프로 복귀

                elif line and not line.startswith(("TRIGGER_START:", ">> CORE0:", "Core 1:", "Warning:", "OK_")):
                    # CSV 데이터 라인 (GSR, TEMP, BPM 3개 항목)
                    data_fields = line.split(',')
                    if len(data_fields) == EXPECTED_FEILDS:
                        # 현재 시간 추가 및 저장
                        current_datetime = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                        csv_writer.writerow([current_datetime] + data_fields)
                        csv_file.flush()
                        row_count += 1
                    else:
                         print(f"경고: 예상치 못한 데이터 포맷 수신: {line}") # 데이터 포맷 오류 확인용

                elif line.startswith((">> CORE0:", "Core 1:", "Warning:", "MAX30102")):
                    print(f"Pico Status: {line}") # 기타 상태 메시지 출력

            except KeyboardInterrupt:
                print("\n프로그램을 종료합니다.")
                if csv_file: csv_file.close()
                return
            except Exception as e:
                print(f"데이터 처리 중 오류 발생: {e}")
                time.sleep(1)

def main():
    try:
        import serial
    except ImportError:
        print("\nPySerial 라이브러리가 설치되어 있지 않습니다.")
        print("pip install pyserial 명령어로 설치해주세요.")
        return

    # **[수정사항 4]** 저장 경로 존재 여부 확인
    if not os.path.isdir(SAVE_PATH):
        print(f"❌ 저장 경로를 찾을 수 없습니다: {SAVE_PATH}")
        print("    경로를 확인하거나 직접 생성해주세요.")
        return
        
    print(f"시리얼 포트 열기: {SERIAL_PORT} @ {BAUD_RATE}...")
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
        ser.flushInput()
        ser.flushOutput()
    except serial.SerialException as e:
        print(f"❌ 시리얼 포트 연결 실패: {e}")
        print("    올바른 포트 이름인지 확인하고, Pico가 연결되어 있는지 확인하세요.")
        return

    try:
        collect_data(ser)
    finally:
        ser.close()

if __name__ == "__main__":
    main()
