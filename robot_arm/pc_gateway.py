# -*- coding: utf-8 -*-
"""
PC 게이트웨이 — Dobot 이 USB 로 연결된 PC 에서 실행

cnt_process 최신 사진을 1초마다 읽어서(폴링), warehouse_server.py 와 똑같은 판단·보고 로직으로 처리합니다.
  - PC 는 공개 주소가 없어서 Mobius 구독 알림을 받을 수 없음 → 직접 읽어오는 폴링 방식
  - 스레드 없이 한 줄로 순서대로 돌기 때문에 DobotRPC 규칙(한 이벤트루프·스레드 금지)과도 맞음
  - 시작할 때 이미 올라와 있던 사진은 "기준"으로만 기록하고 실행하지 않음 (옛 주문에 로봇이 움직이지 않게)

.env
  HARDWARE=dobot   실제 Dobot 2세트 제어 (fake 면 가짜 장비로 통신만 확인)
  PROCESS_PATH     읽을 cnt_process 경로. 비우면 TEST_ROOT/cnt_process (연습용), 합동 테스트 때 cnt_process
  REPORT_ROOT      보고 위치 (warehouse_server.py 와 같음)

실행:  py -3.9 pc_gateway.py        (Ctrl+C 로 종료)
※ gcloud 서버와 같은 cnt_process 를 동시에 처리하면 보고가 중복됩니다. 둘 중 하나만 켜기.
"""

import os
import time

import mobius
import warehouse_server as ws

PROCESS_PATH = (os.environ.get("PROCESS_PATH", "").strip("/")
                or f"{os.environ.get('TEST_ROOT', 'cnt_test_warehouse_arm')}/cnt_process")
POLL_SEC = 1.0


def main():
    print(f"PC 게이트웨이 시작 — 읽는 곳: {PROCESS_PATH}, 보고 위치: {ws.REPORT_ROOT or '진짜 컨테이너'}, 장비: {ws.HARDWARE}")
    print(ws.settings_summary())
    print(ws.settings_fault_summary())
    if ws.hw:
        ws.hw.connect()

    try:
        first = mobius.get_latest(PROCESS_PATH)
        if isinstance(first, dict):
            ws.mark_as_handled(first)
            print(f"[시작] 기존 사진(seq={first.get('seq')})은 기준으로만 기록 — 새 status 부터 반응합니다")

        while True:
            try:
                snapshot = mobius.get_latest(PROCESS_PATH)
            except Exception as e:
                print(f"[읽기 실패] {e}")
                time.sleep(POLL_SEC * 3)
                continue
            if isinstance(snapshot, dict) and ws.process_snapshot(snapshot) == "retry":
                time.sleep(POLL_SEC * 2)  # 보고 전송 실패 → 잠깐 쉬고 재시도
            if ws.hw:
                ws.hw.keepalive()  # 기다리는 동안 DobotLink 연결이 끊기지 않게
            time.sleep(POLL_SEC)
    except KeyboardInterrupt:
        print("\n종료합니다")
    finally:
        if ws.hw:
            ws.hw.disconnect()


if __name__ == "__main__":
    main()
