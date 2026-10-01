# -*- coding: utf-8 -*-
"""
카메라 ROI + 색상 범위 캘리브레이션 도구.
카메라 번호 / ROI / HSV 범위 / 판정 기준은 전부 board_config.py에서 가져옴 -
값을 바꾸려면 board_config.py를 수정하고 이 스크립트를 다시 켤 것
(그러면 메인 코드에도 같은 값이 그대로 적용됨).

사용법:
  1. 로봇암을 CAMERA_CHECK_XYZ 위치에 두고 python camera.py
  2. 초록 사각형(ROI)이 트레이 위 보드 자리에 오는지 확인 -> 안 맞으면
     board_config.py의 CAMERA_ROI 수정 후 재실행
  3. 's': ROI 안 HSV 출력 (중앙 픽셀 + ROI 전체 중앙값)
  4. 'c': 색상 판정 테스트 - 메인 코드와 같은 기준으로 "보드 있음/없음"까지 표시
     보드 없는 빈 트레이에서도 꼭 눌러서 "없음"으로 나오는지 확인할 것
  5. 'q': 종료

주의: 조명이 바뀌면(전시장 vs 테스트 환경) 범위가 달라질 수 있음 - 현장에서 재확인.
"""

import cv2
import numpy as np

from board_config import (
    CAMERA_INDEX_PREFERENCE, CAMERA_ROI, COLOR_RANGES_HSV, CAMERA_PRESENCE_MIN_RATIO,
)

CAMERA_INDEX = CAMERA_INDEX_PREFERENCE[0]
ROI_X, ROI_Y, ROI_W, ROI_H = CAMERA_ROI


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
        print(f"카메라 {CAMERA_INDEX}를 열 수 없음 - board_config.py의 CAMERA_INDEX_PREFERENCE 확인")
        return

    print(f"[카메라 {CAMERA_INDEX}번] ROI={CAMERA_ROI} 판정기준={CAMERA_PRESENCE_MIN_RATIO}")
    print("[조작] s: HSV 출력 / c: 색상 판정 / q: 종료")

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
            hsv = cv2.cvtColor(get_roi(frame), cv2.COLOR_BGR2HSV)
            cy, cx = hsv.shape[0] // 2, hsv.shape[1] // 2
            h, s, v = hsv[cy, cx]
            mh, ms, mv = np.median(hsv.reshape(-1, 3), axis=0).astype(int)
            print(f"[HSV] 중앙 픽셀 H:{h} S:{s} V:{v} / ROI 중앙값 H:{mh} S:{ms} V:{mv}")

        elif key == ord("c"):
            color, ratio = classify_color(get_roi(frame))
            present = ratio >= CAMERA_PRESENCE_MIN_RATIO
            print(f"[판정] 감지된 색: {color}, 비율: {ratio:.2f} -> "
                  f"{'보드 있음' if present else '보드 없음'} (기준 {CAMERA_PRESENCE_MIN_RATIO})")

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()