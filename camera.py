"""
카메라 ROI(트레이 위 보드가 보이는 영역) + 색상 범위 캘리브레이션 도구.

사용법:
  1. python calibrate_camera.py
  2. 웹캠 화면에 초록 사각형(ROI)이 보임 - 트레이 위 보드 놓이는 자리에 오도록
     아래 ROI_X/Y/W/H 값을 수정하면서 재실행 (실시간 슬라이더는 아니고, 코드
     값 바꿔서 다시 켜는 방식 - 빠르게 맞추고 싶으면 슬라이더 버전도 요청 가능)
  3. ROI 안에 보드를 실제로 놓고, 's' 키를 눌러 그 순간의 HSV 값을 출력
     -> 이 값들을 보고 COLOR_RANGES_HSV 범위를 4색 전부 채울 것
  4. 다 채웠으면 'c' 눌러서 색상 판정이 잘 되는지(터미널에 색상명 출력) 확인
  5. 'q'로 종료

주의: 조명이 바뀌면(특히 전시장 조명 vs 지금 테스트 환경) 범위가 달라질 수
있음 - 전시장 가서 반드시 재보정할 것.
"""

import cv2
import numpy as np

CAMERA_INDEX = 1 # 웹캠이 여러 개면 0,1,2... 바꿔가며 찾을 것

# ⚠️ 트레이 위 보드가 보이는 화면 영역 - 픽셀 좌표로 실측 후 수정
ROI_X, ROI_Y, ROI_W, ROI_H = 200, 150, 150, 100

# ⚠️ 's' 키로 찍은 HSV 값 보고 채울 것 - 지금은 대략적인 기본값만
# ⚠️ 실측 완료 (2026-09 촬영) - H:S:V 순서로 yellow(33,59,252) blue(94,68,243)
# green(53,46,240) red/핑크(160,70,255). 전부 채도(S) 낮고 명도(V) 높은
# 파스텔톤이라 S/V는 4색 공통으로 넉넉하게 낮춰 잡음. 노랑/초록 hue가
# 33/53으로 가까워서 중간값(43) 기준으로 여유 없이 나눔 - 오판정 나면
# 여기 경계값부터 조정할 것.
COLOR_RANGES_HSV = {
    "yellow": [((20, 30, 150), (42, 255, 255))],
    "green":  [((44, 30, 150), (70, 255, 255))],
    "blue":   [((75, 30, 150), (120, 255, 255))],
    "red":    [((140, 30, 150), (180, 255, 255))],  # 실측은 핑크(H160) - 순수빨강(H0 근처) 필요하면 별도 구간 추가
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
    cap = cv2.VideoCapture(CAMERA_INDEX)
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
        cv2.imshow("calibrate (s: HSV출력, c: 색상판정, q: 종료)", frame)

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