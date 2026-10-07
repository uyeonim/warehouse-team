# -*- coding: utf-8 -*-
"""
=====================================================================
 보드 로봇암 튜닝값 모음 (board_config.py)
=====================================================================
main_board_robot_arm.py와 tools/ 아래 테스트/튜닝 스크립트(camera.py, board_pick_check.py 등)가
전부 이 파일을 import 해서 씀 - 여기 값 하나 바꾸면 전부 동시에 반영됨. 비밀값(MQTT/Flask 주소 등)은 여기 넣지 말고 .env에 둘 것.

맨 아래 "계산 헬퍼"는 튜닝값이 아니라 공용 계산식이라 건드리지 말 것.

[R값 규칙]
  픽업 좌표에만 R을 적고, 나머지 위치(놓기/카메라/HOME/중간점)는 R을 안 적음.
  픽업할 때의 손목 각도(= 픽업 R - 픽업 지점 베이스 각도)를 고정한 채 베이스만
  돌아가므로, 나머지 위치의 R은 r_for()가 자동 계산함.
=====================================================================
"""

import math

# =================================================================
# 연결
# =================================================================
DOBOT_PORT = "COM3"   # 팔 + 컨베이어(Magic Box) 공유 포트 - 장치관리자에서 확인

# =================================================================
# 좌표 - 픽업 (x, y, floor_z, r)
# 색당 5개×4줄(대각선 배치) 적재. row0=제일 가까운 줄 ~ row3=제일 먼 줄.
# 픽업은 층별로 - 맨 위층을 가까운 줄부터 하나씩(0/5 1/5 2/5 3/5) 걷고 다음 층(0/4 1/4 ...)으로.
#   그래서 재고 N개일 때 실물도 이 순서대로 남아 있어야 함 (예: 재고 3 = row1~3에 1개씩, row0 비어있음)
# floor_z = 그 자리 바닥 높이(보드 0개). 수납함/받침대가 있으면 그 높이 포함.
#           픽업 높이 = floor_z + 개수 × BOARD_THICKNESS_MM (맨 위 보드 윗면)
#           실측할 때: floor_z = 실측 z - 그때 쌓인 개수 × BOARD_THICKNESS_MM
# r = 픽업 시 R (이 값이 손목 각도를 정함 - 나머지 위치 R은 자동 계산)
# 실측 2026-10-06 (Joint4=0, 손목 0도). green만 수납함, 나머지는 맨바닥.
# 2026-10-07 보드 두께 20 -> 22mm 반영 (floor_z = 실측 z - 개수 × 22로 재계산)
# "추정" 자리 = 그 색 row3 실측 + green의 같은 줄-row3 xy 차이, 바닥은 row3와 같게, R=베이스 각도
#   -> tools/board_pick_check.py로 위치 확인 후 실측값으로 교체할 것
# =================================================================
ROW_CAPACITY = 5      # 줄 하나당 최대 적재 개수
NUM_ROWS = 4          # 줄 개수

PICK_POSE_ROWS = {
    "red": [
        (254.6581, 154.6197, -64.5424, 31.2647),  # row0 추정 - 실측 필요
        (279.2333, 156.9358, -64.5424, 29.3370),  # row1 추정 - 실측 필요
        (302.6684, 158.3068, -64.5424, 27.6112),  # row2 추정 - 실측 필요
        (323.5456, 158.6226, -64.5424, 26.1172),  # row3 실측 (맨바닥, 3개)
    ],
    "green": [
        (266.8598, 54.8011, -54.2228, 11.6047),   # row0 실측 (수납함, 5개, 2026-10-07 재측정)
        (290.5193, 52.1270, -52.1839, 10.1722),   # row1 실측 (수납함, 5개, 2026-10-07 재측정)
        (313.9551, 56.2684, -53.8346, 10.1609),   # row2 실측 (수납함, 5개, 2026-10-07 재측정)
        (333.0137, 55.2356, -55.6250, 9.4177),    # row3 실측 (수납함, 5개, 2026-10-07 재측정)
    ],
    "blue": [
        (265.9353, -45.4868, -59.9105, -9.7062),  # row0 추정 - 실측 필요
        (290.5105, -43.1707, -59.9105, -8.4525),  # row1 추정 - 실측 필요
        (313.9456, -41.7997, -59.9105, -7.5839),  # row2 추정 - 실측 필요
        (334.8228, -41.4839, -59.9105, -7.0629),  # row3 실측 (맨바닥, 3개)
    ],
    "yellow": [
        (268.1114, -126.2881, -55.6555, -25.2218),  # row0 추정 - 실측 필요
        (292.6866, -123.9720, -55.6555, -22.9558),  # row1 추정 - 실측 필요
        (316.1217, -122.6010, -55.6555, -21.1977),  # row2 추정 - 실측 필요
        (336.9989, -122.2852, -55.6555, -19.9441),  # row3 실측 (맨바닥, 3개)
    ],
}
BOARD_THICKNESS_MM = 22.0   # 실측 (2026-10-07)

# =================================================================
# 좌표 - 나머지 위치 (x, y, z) - R은 자동 계산이라 안 적음
# =================================================================
PLACE_XYZ = (-124.8803, -265.2661, 34.3968)          # 트레이에 보드 놓는 위치 (흡착 상태로 트레이에 닿는 높이) - 2026-10-07 재조정
# 트레이에 놓을 때 R - 픽업 R과 무관하게 "기준 R(손목 0도, 놓는 위치 베이스 각도)"에서
# 색별로 이만큼 돌린 값으로 놓음. 트레이 위에서 제자리로 R만 돌린 뒤 하강.
PLACE_R_OFFSET_DEG_BY_COLOR = {
    "red":    -50.0,
    "green":  -60.0,
    "blue":   -80.0,
    "yellow": -100.0,
}
HOME_XYZ = (239.9906, -5.5209, 84.0930)              # 대기 위치
HOME_WRIST_DEG = 0.0   # 픽업 전(손목 각도가 아직 없을 때) HOME에서 쓸 손목 각도

SAFE_LIFT_MM = 40.0    # 이동할 때 출발/도착 높이보다 이만큼 더 올려서 이동

# =================================================================
# 이동
# =================================================================
# 출발/도착 지점의 베이스 각도 차이가 이 값 이상이면 극좌표 중간점을 경유함
# (반대편으로 크게 돌 때 이동 명령이 씹히는 문제 대응)
MIDPOINT_MIN_ANGLE_DEG = 60.0

# 이동은 "명령 보낸 뒤 목표에 도착할 때까지 GetPose로 확인하며 대기" 방식.
# 고정 대기시간이 아니라서 빨리 도착하면 바로 다음 단계로 넘어가고, 큰 회전처럼
# 오래 걸리는 동작은 도착할 때까지 기다려줌.
# 도착 = 허용오차 이내 + "멈춤"(직전 조회 대비 MOVE_STABLE 이내). 아직 움직이는 중에
# 다음 명령을 보내면 그 명령이 씹혀서 팔이 안 움직이는 게 실물로 확인됨(2026-10-02).
# 목표 못 간 채 MOVE_RESEND_AFTER_SEC 동안 멈춰 있으면 명령 재전송, MOVE_MAX_RESEND번 다 해도
# 안 되면 타임아웃 안 기다리고 바로 도착 실패.
MOVE_TIMEOUT_SEC = 6.0          # 이 시간 안에 목표 도착 못 하면 "도착 실패"로 봄
MOVE_POLL_INTERVAL_SEC = 0.2    # 도착 확인 간격
MOVE_STABLE_MM = 0.5            # 직전 조회 대비 이 이내로만 움직였으면 "멈춤"
MOVE_STABLE_DEG = 0.5
MOVE_RESEND_AFTER_SEC = 1.0     # 목표 못 간 채 이만큼 멈춰 있으면 명령 씹힘으로 보고 재전송
MOVE_MAX_RESEND = 2             # 재전송 최대 횟수
PRECISION_SETTLE_SEC = 0.5      # 픽업/트레이 하강처럼 정밀 접촉 이동은 도착 후 이만큼 더 안정화 대기
SUCTION_SETTLE_DELAY = 1.0      # 흡착 ON 후 흡착력 형성 대기
RELEASE_SETTLE_DELAY = 0.5      # 흡착 OFF 후 대기

# 도착 판정 허용오차 (모든 이동에 사용. 픽업 위치 / 트레이 하강은 도착 실패 시 failed 처리)
POSITION_TOLERANCE_MM = 2.0
POSITION_TOLERANCE_DEG = 2.0

# =================================================================
# 컨베이어 (Magic Box) - 벨트 2개를 이어붙여 하나처럼 동시 구동
# =================================================================
TRAY_CONVEYOR_STP_1 = 0
TRAY_CONVEYOR_STP_2 = 1
TRAY_CONVEYOR_SPEED_MM_S = 50
TRAY_CONVEYOR_RUN_TIME_SECTION0 = 4.0    # keycap_dispensed -> 키캡 로봇암 앞까지 (초)
TRAY_CONVEYOR_RUN_TIME_SECTION1 = 11.5   # keycap_packed -> 보드 로봇암 앞까지 (초)
TRAY_CONVEYOR_RUN_TIME_SECTION2 = 6.5    # board_packed  -> 픽업대까지 (초)

# =================================================================
# 카메라 (트레이 위 고정 카메라 - 2026-10-07 로봇암에서 분리. 팔이 HOME으로 돌아간 뒤 촬영)
# =================================================================
CAMERA_INDEX_PREFERENCE = [0]   # 앞에서부터 시도해서 처음 열리는 번호 사용. [1]이면 1번 고정
CAMERA_ROI = (242, 242, 150, 100)   # (x, y, w, h) 픽셀 - tools/camera.py로 맞춤 (2026-10-07 고정 카메라 기준)
CAMERA_PRESENCE_MIN_RATIO = 0.5     # ROI 중 해당 색 비율이 이 이상이어야 "보드 있음"
                                     # 실측: 보드 최소 0.72 / 빈 배경 최대 0.13 (2026-09-30, 임시 트레이)
CAMERA_SETTLE_DELAY = 0.5      # 촬영 전 대기
CAMERA_WARMUP_FRAMES = 5       # 열자마자 읽은 앞 N장은 버림 (자동노출 적응 전이라 어두움)

SAVE_CAMERA_IMAGE = True       # 확인할 때마다 이미지 저장
SHOW_CAMERA_WINDOW = False     # 확인할 때마다 창 띄우기 (모니터 붙은 환경에서만)
CAMERA_WINDOW_MS = 800         # 창 띄우는 시간(ms) - 이 동안 메인 루프 멈춤
CAMERA_IMAGE_DIR = r"C:\Users\dusdn\warehouse-flask\board_camera_capture_log"

# =================================================================
# 색상 판정 HSV 범위 ((H,S,V 하한), (H,S,V 상한))
# 실측(2026-09-30, DSHOW): yellow H42 S25 / green H67 S41 / blue H94 S149 / red(핑크) H149 S63
# 흰 종이 H88 S20, 박스 트레이 H90 S2 - 배경은 판정 비율 0.13 이하로 확인됨
# 실측(2026-10-07, 고정 카메라 0번): yellow H31 S80 / green H43 S70 / 흰 트레이 H80 S12
#   -> yellow/green만 새 카메라 기준으로 갱신 (경계 H37, 흰 트레이 거르려고 S 하한 40)
#   blue/red는 아직 예전 카메라 기준 - 새 카메라로 측정 후 갱신 필요
# =================================================================
COLOR_RANGES_HSV = {
    "yellow": [((20, 40, 150), (37, 255, 255))],
    "green":  [((38, 40, 150), (85, 255, 255))],
    "blue":   [((86, 30, 150), (130, 255, 255))],
    "red":    [((135, 30, 150), (179, 255, 255))],
}


# =================================================================
# =================================================================
# 계산 헬퍼 (튜닝값 아님 - 수정 금지)
# =================================================================
# =================================================================
BOARD_COLORS = list(PICK_POSE_ROWS.keys())
TOTAL_CAPACITY = ROW_CAPACITY * NUM_ROWS

# 모든 자리가 꽉 찼을 때 제일 높은 맨 위 보드 윗면 (수납함 벽/옆 스택 높이의 기준)
FULL_STACK_TOP_Z = max(floor_z + ROW_CAPACITY * BOARD_THICKNESS_MM
                       for rows in PICK_POSE_ROWS.values() for (_x, _y, floor_z, _r) in rows)


def _wrap_deg(a):
    """각도를 (-180, 180] 범위로."""
    a = (a + 180.0) % 360.0 - 180.0
    return 180.0 if a == -180.0 else a


def base_angle(x, y):
    """그 XY를 향할 때의 베이스 각도(도)."""
    return math.degrees(math.atan2(y, x))


def wrist_of(x, y, r):
    """주어진 자세의 손목 각도 = R - 베이스 각도."""
    return _wrap_deg(r - base_angle(x, y))


def r_for(x, y, wrist_deg):
    """손목 각도를 wrist_deg로 고정했을 때 그 XY에서의 R.
    ⚠️ -180~180으로 접지 않음 - Dobot은 R = 베이스 + 손목이라, 예를 들어 -191을
    169로 접어서 보내면 손목을 반대로 280도 돌리라는 명령이 돼서 한계에 걸림."""
    return base_angle(x, y) + wrist_deg


def place_r_for(color):
    """트레이에 놓을 때 R = 놓는 위치 기준 R(손목 0도) + 색별 회전값. 접지 않음."""
    return base_angle(PLACE_XYZ[0], PLACE_XYZ[1]) + PLACE_R_OFFSET_DEG_BY_COLOR[color]


def arc_midpoint(xy_from, xy_to):
    """두 점 사이의 극좌표 중간점 (베이스 각도 평균 + 반경 평균).
    직선 중간점은 베이스 근처로 떨어져 팔이 못 가는 경우가 많아서 호(arc) 위로 잡음."""
    a1 = base_angle(*xy_from)
    a2 = base_angle(*xy_to)
    mid_a = math.radians(a1 + _wrap_deg(a2 - a1) / 2.0)
    r1 = math.hypot(*xy_from)
    r2 = math.hypot(*xy_to)
    mid_r = (r1 + r2) / 2.0
    return (mid_r * math.cos(mid_a), mid_r * math.sin(mid_a))


def travel_z_for(pick_z):
    """보드 들고 이동하는 높이. 스택이 줄어도 낮아지지 않게 "꽉 찼을 때 제일 높은 자리" 기준으로 고정
    (스택 높이 따라 낮추면 4층부터 들고 나올 때 수납함 벽에 걸리는 게 실물로 확인됨, 2026-10-06)."""
    return max(pick_z, PLACE_XYZ[2], FULL_STACK_TOP_Z) + SAFE_LIFT_MM


def needs_midpoint(xy_from, xy_to):
    """베이스 각도 차이가 MIDPOINT_MIN_ANGLE_DEG 이상이면 중간점 경유 필요."""
    diff = abs(_wrap_deg(base_angle(*xy_to) - base_angle(*xy_from)))
    return diff >= MIDPOINT_MIN_ANGLE_DEG


def compute_pick_pose(color, count):
    """
    count = 해당 색 전체 재고(1~TOTAL_CAPACITY). "층별로 빈다"(맨 위층을 row0->row3 순으로
    하나씩, 다 걷으면 다음 층)는 전제로 집을 줄과 그 줄 남은 개수를 역산해서 (x, y, z, r) 반환.
    예: TOTAL_CAPACITY=20 -> count 20: 0/5, 19: 1/5, 17: 3/5, 16: 0/4, 3: 1/1, 1: 3/1
    """
    if count <= 0:
        raise ValueError(f"[{color}] 재고 0 이하로 픽업 위치 계산 불가 (count={count})")
    if count > TOTAL_CAPACITY:
        raise ValueError(f"[{color}] 재고가 최대 적재량 초과 (count={count} > {TOTAL_CAPACITY})")
    emptied = TOTAL_CAPACITY - count
    row_index = emptied % NUM_ROWS
    count_in_row = ROW_CAPACITY - emptied // NUM_ROWS
    return pick_pose_for(color, row_index, count_in_row)


def pick_pose_for(color, row_index, count_in_row):
    """줄 번호/그 줄 개수를 직접 지정해서 (x, y, z, r) 반환 (튜닝 스크립트용)."""
    x, y, floor_z, r = PICK_POSE_ROWS[color][row_index]
    z = floor_z + count_in_row * BOARD_THICKNESS_MM
    return (x, y, z, r)
