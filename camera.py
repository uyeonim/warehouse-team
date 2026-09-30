"""
카메라 ROI(트레이 위 보드가 보이는 영역) + 색상 범위 캘리브레이션 도구.

사용법:
  1. python camera.py
  2. 웹캠 화면에 초록 사각형(ROI)이 보임 - 트레이 위 보드 놓이는 자리에 오도록
     아래 ROI_X/Y/W/H 값을 수정하면서 재실행
  3. ROI 안에 보드를 실제로 놓고, 's' 키를 눌러 그 순간의 HSV 값을 출력
     -> 이 값들을 보고 COLOR_RANGES_HSV 범위를 4색 전부 채울 것
  4. 다 채웠으면 'c' 눌러서 색상 판정이 잘 되는지(터미널에 색상명 출력) 확인
     - 보드 없는 빈 트레이 상태에서도 'c' 눌러서 비율이 0.5(메인 스크립트의
       CAMERA_PRESENCE_MIN_RATIO) 밑으로 나오는지 꼭 확인할 것
  5. 'q'로 종료

주의: 조명이 바뀌면(특히 전시장 조명 vs 지금 테스트 환경) 범위가 달라질 수
있음 - 전시장 가서 반드시 재보정할 것.
여기서 확정한 ROI / COLOR_RANGES_HSV 값은 main_board_robot_arm.py의
CAMERA_ROI / COLOR_RANGES_HSV에 똑같이 복사할 것.
"""

import cv2
import numpy as np

CAMERA_INDEX = 1  # 웹캠이 여러 개면 0,1,2... 바꿔가며 찾을 것

# ⚠️ 트레이 위 보드가 보이는 화면 영역 - 로봇암 장착 카메라 기준으로 재보정 필요
ROI_X, ROI_Y, ROI_W, ROI_H = 200, 150, 150, 100

# H:S:V 순서 실측값 (2026-09-30, 로봇암 장착 카메라, CAMERA_CHECK_POSE 기준)
# yellow(47,30,214) green(66,36,218) blue(99,144,255) red/핑크(147,57,219).
# 노랑/초록 채도(S)가 30대로 매우 낮아서 S 하한을 15로 내림 - 흰 트레이 배경이
# 노랑/초록으로 잡힐 위험이 있으니 빈 트레이 상태로 'c' 판정 꼭 확인할 것.
# 노랑(47)/초록(66) 경계는 중간값 56/57로 나눔.
# main_board_robot_arm.py의 COLOR_RANGES_HSV와 동일값 유지할 것.
COLOR_RANGES_HSV = {
    "yellow": [((35, 15, 150), (56, 255, 255))],
    "green":  [((57, 15, 150), (85, 255, 255))],
    "blue":   [((86, 30, 150), (130, 255, 255))],
    "red":    [((135, 30, 150), (179, 255, 255))],
}


def get_roi(frame):
    return frame[ROI_Y:ROI_Y + ROI_H, ROI_X:ROI_X + ROI_W]


def classify_color(roi_bgr):
    hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
    total_px = roi_bgr.shape[0] * roi_bgr.shape[1]
    best_color, best_ratio = None, 0.0
    for color, ranges in COLOR_RANGES_HSV.items():
        mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
        for lo, hi in ranges:
            mask |= cv2.inRange(hsv, np.array(lo), np.array(hi))
        ratio = cv2.countNonZero(mask) / total_px
        if ratio > best_ratio:
            best_ratio, best_color = ratio, color
    return best_color, best_ratio


def main():
    # 윈도우 기본 백엔드(MSMF)는 여는 데 5~30초씩 걸려서 DirectShow로 지정
    cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print(f"카메라 {CAMERA_INDEX}를 열 수 없음 - CAMERA_INDEX 값 확인")
        return

    print("[조작] s: 현재 ROI 중앙 픽셀 HSV 출력 / c: 색상 판정 테스트 / q: 종료")

    while True:
        ok, frame = cap.read()
        if not ok:
            print("프레임 읽기 실패")
            break

        cv2.rectangle(frame, (ROI_X, ROI_Y), (ROI_X + ROI_W, ROI_Y + ROI_H), (0, 255, 0), 2)
        cv2.imshow("calibrate (s: HSV, c: classify, q: quit)", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            break

        elif key == ord("s"):
            roi = get_roi(frame)
            hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            cy, cx = hsv.shape[0] // 2, hsv.shape[1] // 2
            h, s, v = hsv[cy, cx]
            print(f"[HSV] ROI 중앙 픽셀 = H:{h} S:{s} V:{v}  "
                  f"(이 값 참고해서 COLOR_RANGES_HSV 범위 채울 것)")

        elif key == ord("c"):
            roi = get_roi(frame)
            color, ratio = classify_color(roi)
            print(f"[판정] 감지된 색: {color}, 비율: {ratio:.2f}")

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()