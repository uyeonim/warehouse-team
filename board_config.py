# -*- coding: utf-8 -*-
"""
=====================================================================
 보드 로봇암 튜닝값 모음 (board_config.py)
=====================================================================
main_board_robot_arm.py / board_location_tunning.py / board_place_tunning.py /
camera.py 가 전부 이 파일을 import 해서 씀 - 여기 값 하나 바꾸면 4개 스크립트에
동시에 반영됨. 비밀값(MQTT/Flask 주소 등)은 여기 넣지 말고 .env에 둘 것.

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
# 좌표 - 픽업 (x, y, base_z, r)
# 색당 5개×4줄(대각선 배치) 적재. row0=제일 가까운 줄 ~ row3=제일 먼 줄.
# 픽업은 가까운 줄부터 5개 다 빈 다음 다음 줄로.
# base_z = 그 줄에 1개 남았을 때 높이. 개수가 늘면 BOARD_THICKNESS_MM씩 위로.
# r = 픽업 시 R (이 값이 손목 각도를 정함 - 나머지 위치 R은 자동 계산)
# =================================================================
ROW_CAPACITY = 5      # 줄 하나당 최대 적재 개수
NUM_ROWS = 4          # 줄 개수

PICK_POSE_ROWS = {
    "red":    [(229.4263, 190, -38, 41), (255.1454, 190, -38, 38),
               (290.2512, 190, -38, 35), (320.1271, 165.9958, -37.1079, 27.3997)],
    "green":  [(240.0187, 85, -38, 20), (267.8990, 85, -38, 20),
               (296.3269, 85, -38, 20), (326.5881, 66.0017, -38, 20)],
    "blue":   [(245.9397, -0.5, -38, 0.5), (271.3919, -0.5, -38, 0.5),
               (298.0619, -0.5, -38, 0.5), (325.5233, -28.8626, -38, 0.5)],
    "yellow": [(253.0182, -100, -38, -20), (279.8343, -100, -38, -18),
               (303.9048, -100, -38, -16), (332.5840, -121.5756, -37.2979, -20.0798)],
}
BOARD_THICKNESS_MM = 20.0

# =================================================================
# 좌표 - 나머지 위치 (x, y, z) - R은 자동 계산이라 안 적음
# =================================================================
PLACE_XYZ = (-116.6558, -294.7615, 28.6249)          # 트레이에 보드 놓는 위치 (흡착 상태로 트레이에 닿는 높이)
CAMERA_CHECK_XYZ = (-97.5224, -229.2938, 58.3063)    # 카메라로 배치 확인하는 위치
HOME_XYZ = (239.9906, -5.5209, 84.0930)              # 대기 위치
HOME_WRIST_DEG = 0.0   # 픽업 전(손목 각도가 아직 없을 때) HOME에서 쓸 손목 각도

SAFE_LIFT_MM = 40.0    # 이동할 때 출발/도착 높이보다 이만큼 더 올려서 이동

# =================================================================
# 이동
# =================================================================
# 출발/도착 지점의 베이스 각도 차이가 이 값 이상이면 극좌표 중간점을 경유함
# (반대편으로 크게 돌 때 이동 명령이 씹히는 문제 대응)
MIDPOINT_MIN_ANGLE_DEG = 60.0

MOVE_WAIT_SEC = 2.0             # 일반 이동 대기
PRECISION_MOVE_WAIT_SEC = 3.0   # 픽업/놓기 정밀 접촉 이동 대기 - 짧으면 흡착 실패
CAMERA_MOVE_WAIT_SEC = 4.0      # 카메라 확인 위치로 이동 대기
HOME_MOVE_WAIT_SEC = 5.0        # HOME 복귀 대기
SUCTION_SETTLE_DELAY = 1.0      # 흡착 ON 후 흡착력 형성 대기
RELEASE_SETTLE_DELAY = 0.5      # 흡착 OFF 후 대기

# 픽업 위치 이동 / 트레이 하강 두 구간만 GetPose로 도착 확인 - 허용오차
POSITION_TOLERANCE_MM = 2.0
POSITION_TOLERANCE_DEG = 2.0

# =================================================================
# 컨베이어 (Magic Box) - 벨트 2개를 이어붙여 하나처럼 동시 구동
# =================================================================
TRAY_CONVEYOR_STP_1 = 0
TRAY_CONVEYOR_STP_2 = 1
TRAY_CONVEYOR_SPEED_MM_S = 50
TRAY_CONVEYOR_RUN_TIME_SECTION1 = 12.0   # keycap_packed -> 보드 로봇암 앞까지 (초)
TRAY_CONVEYOR_RUN_TIME_SECTION2 = 6.5    # board_packed  -> 픽업대까지 (초)

# =================================================================
# 카메라 (로봇암 장착, CAMERA_CHECK_XYZ에서 촬영)
# =================================================================
CAMERA_INDEX_PREFERENCE = [1]   # 앞에서부터 시도해서 처음 열리는 번호 사용. [1]이면 1번 고정
CAMERA_ROI = (200, 150, 150, 100)   # (x, y, w, h) 픽셀 - camera.py로 맞춤
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
# =================================================================
COLOR_RANGES_HSV = {
    "yellow": [((35, 15, 150), (56, 255, 255))],
    "green":  [((57, 15, 150), (85, 255, 255))],
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
    """손목 각도를 wrist_deg로 고정했을 때 그 XY에서의 R."""
    return _wrap_deg(base_angle(x, y) + wrist_deg)


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


def needs_midpoint(xy_from, xy_to):
    """베이스 각도 차이가 MIDPOINT_MIN_ANGLE_DEG 이상이면 중간점 경유 필요."""
    diff = abs(_wrap_deg(base_angle(*xy_to) - base_angle(*xy_from)))
    return diff >= MIDPOINT_MIN_ANGLE_DEG


def compute_pick_pose(color, count):
    """
    count = 해당 색 전체 재고(1~TOTAL_CAPACITY). "가까운 줄부터 빈다"는 전제로
    현재 줄과 그 줄 남은 개수를 역산해서 (x, y, z, r) 반환.
    예: TOTAL_CAPACITY=20, count=3 -> row3(4번째 줄), 그 줄 3개.
    """
    if count <= 0:
        raise ValueError(f"[{color}] 재고 0 이하로 픽업 위치 계산 불가 (count={count})")
    emptied = TOTAL_CAPACITY - count
    row_index = min(emptied // ROW_CAPACITY, NUM_ROWS - 1)
    count_in_row = ROW_CAPACITY - (emptied % ROW_CAPACITY)
    return pick_pose_for(color, row_index, count_in_row)


def pick_pose_for(color, row_index, count_in_row):
    """줄 번호/그 줄 개수를 직접 지정해서 (x, y, z, r) 반환 (튜닝 스크립트용)."""
    x, y, base_z, r = PICK_POSE_ROWS[color][row_index]
    z = base_z + (count_in_row - 1) * BOARD_THICKNESS_MM
    return (x, y, z, r)