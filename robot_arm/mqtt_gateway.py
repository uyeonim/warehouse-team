# -*- coding: utf-8 -*-
"""
MQTT 게이트웨이 — Dobot 이 USB 로 연결된 PC 에서 실행 (pc_gateway.py 의 MQTT 판)

차이는 "cnt_process 사진을 어떻게 받느냐" 하나뿐입니다.
  pc_gateway.py   : 1초마다 Mobius 에서 직접 읽음 (폴링, HTTP)
  mqtt_gateway.py : VM 중계(mqtt_relay.py)가 뿌려 주는 걸 받음 (MQTT)
판단·장비 동작·보고는 똑같은 warehouse_server.py 를 그대로 씁니다. 보고는 지금처럼 Mobius 에 HTTP.

.env
  MQTT_HOST/PORT/USER/PASS/MQTT_TOPIC, HARDWARE, REPORT_ROOT …(pc_gateway.py 와 동일)

실행:  py -3.9 mqtt_gateway.py        (Ctrl+C 로 종료)
※ pc_gateway.py 와 동시에 켜지 마세요. 같은 주문에 보고가 두 번 올라갑니다.
"""

import json
import os
import queue
import time

import mqtt_relay  # build_client 재사용
import warehouse_server as ws

TOPIC = os.environ.get("MQTT_TOPIC", "sjuiot/warehouse/conveyor")
inbox = queue.Queue()  # 받은 사진을 쌓아 두는 곳. 로봇이 움직이는 동안 온 것도 안 사라짐


def on_message(client, userdata, msg):
    try:
        inbox.put(json.loads(msg.payload.decode("utf-8")))
    except ValueError as e:
        print(f"[무시] 읽을 수 없는 메시지: {e}")


def main():
    print(f"MQTT 게이트웨이 시작 — 토픽: {TOPIC}, 보고 위치: {ws.REPORT_ROOT or '진짜 컨테이너'}, 장비: {ws.HARDWARE}")
    print(ws.settings_summary())
    print(ws.settings_fault_summary())
    if ws.hw:
        ws.hw.connect()

    # clean_session=False: 와이파이가 잠깐 끊겨도 그 사이 온 사진을 브로커가 보관했다가 전달해 줌
    client = mqtt_relay.build_client(os.environ.get("MQTT_CLIENT_ID", "pc_conveyor_arm"), clean_session=False)
    client.on_connect = lambda c, u, f, rc: (print(f"[브로커] 접속 rc={rc}" + (" (성공)" if rc == 0 else " !! 실패")),
                                             c.subscribe(TOPIC, qos=1))
    client.on_disconnect = lambda c, u, rc: print(f"[브로커] 끊김 rc={rc} — 자동 재접속 중")
    client.on_message = on_message
    client.connect(os.environ["MQTT_HOST"], int(os.environ["MQTT_PORT"]), keepalive=30)
    client.loop_start()

    first = True
    try:
        while True:
            try:
                snapshot = inbox.get(timeout=1.0)
            except queue.Empty:
                if ws.hw:
                    ws.hw.keepalive()  # 기다리는 동안 DobotLink 연결 유지
                continue
            if first:  # 켜기 전부터 올라와 있던 주문에는 반응하지 않음 (옛 주문에 로봇이 움직이지 않게)
                ws.mark_as_handled(snapshot)
                print(f"[시작] 기존 사진(seq={snapshot.get('seq')})은 기준으로만 기록 — 새 status 부터 반응합니다")
                first = False
                continue
            if ws.process_snapshot(snapshot) == "retry":
                time.sleep(2)  # 보고 전송 실패 → 잠깐 쉬었다가, 다음 사진에서 다시 보고
    except KeyboardInterrupt:
        print("\n종료합니다")
    finally:
        client.loop_stop()
        client.disconnect()
        if ws.hw:
            ws.hw.disconnect()


if __name__ == "__main__":
    main()
