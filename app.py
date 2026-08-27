import os
import json
import ssl
import paho.mqtt.client as mqtt
from flask import Flask, request

# 로컬 테스트 시에만 .env 파일을 읽음 (Cloud Run 배포 시엔 --set-env-vars로 값이 주입되므로
# 이 파일이 없어도, dotenv가 없어도 문제없이 동작해야 함)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

MQTT_HOST = os.getenv("MQTT_HOST")
MQTT_PORT = int(os.getenv("MQTT_PORT", 8883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASS")
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "warehouse/order/new")
MQTT_REPLACE_TOPIC = os.getenv("MQTT_REPLACE_TOPIC", "warehouse/replace/new")
MQTT_LOAD_TOPIC = os.getenv("MQTT_LOAD_TOPIC", "warehouse/load/new")

app = Flask(__name__)


def publish_message(topic: str, payload: str) -> bool:
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)  # paho-mqtt 2.x 문법
    c.username_pw_set(MQTT_USER, MQTT_PASS)
    c.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
    try:
        c.connect(MQTT_HOST, MQTT_PORT, keepalive=10)
        c.loop_start()
        info = c.publish(topic, payload, qos=1)
        info.wait_for_publish(timeout=5)
        return info.is_published()
    finally:
        c.loop_stop()
        c.disconnect()


@app.route("/")
def hello():
    return "Flask 서버 정상 동작 중"


# 라우트 이름 주의: Mobius 구독(register_subscription.py)에 등록된 nu 경로와
# 정확히 일치해야 한다 -> https://.../notify/cnt_order
@app.route("/notify/cnt_order", methods=["POST"])
def notify_cnt_order():
    data = request.get_json(force=True, silent=True) or {}
    sgn = data.get("m2m:sgn", {})

    if sgn.get("vrq"):
        # 구독 생성 직후 Mobius가 보내는 검증(verification) 요청 - 정상 응답만 주면 됨
        return "", 200

    cin = sgn.get("nev", {}).get("rep", {}).get("m2m:cin")
    if cin:
        con = cin.get("con")
        if isinstance(con, str):
            try:
                con = json.loads(con)
            except ValueError:
                pass

        payload = json.dumps(con, ensure_ascii=False)
        ok = publish_message(MQTT_TOPIC, payload)
        print(f"[MQTT] 새 주문 -> {'전송 완료' if ok else '전송 실패'}: {payload}")

    return "", 200


# cnt_replace 전용 라우트 (2026-08-26 추가) - register_replace_subscription.py의 nu 경로와 일치해야 함
# -> https://.../notify/cnt_replace
@app.route("/notify/cnt_replace", methods=["POST"])
def notify_cnt_replace():
    data = request.get_json(force=True, silent=True) or {}
    sgn = data.get("m2m:sgn", {})

    if sgn.get("vrq"):
        return "", 200

    cin = sgn.get("nev", {}).get("rep", {}).get("m2m:cin")
    if cin:
        con = cin.get("con")
        if isinstance(con, str):
            try:
                con = json.loads(con)
            except ValueError:
                pass

        payload = json.dumps(con, ensure_ascii=False)
        ok = publish_message(MQTT_REPLACE_TOPIC, payload)
        print(f"[MQTT] 교체 요청 -> {'전송 완료' if ok else '전송 실패'}: {payload}")

    return "", 200


# cnt_load 전용 라우트 (2026-08-27 추가) - AGV팀이 바구니 탑재 완료 시 cnt_load에 올림
# 창고팀은 이걸 구독해서 "다음 주문 배출 시작" 신호로 사용한다.
# -> https://.../notify/cnt_load
@app.route("/notify/cnt_load", methods=["POST"])
def notify_cnt_load():
    data = request.get_json(force=True, silent=True) or {}
    sgn = data.get("m2m:sgn", {})

    if sgn.get("vrq"):
        return "", 200

    cin = sgn.get("nev", {}).get("rep", {}).get("m2m:cin")
    if cin:
        con = cin.get("con")
        if isinstance(con, str):
            try:
                con = json.loads(con)
            except ValueError:
                pass

        payload = json.dumps(con, ensure_ascii=False)
        ok = publish_message(MQTT_LOAD_TOPIC, payload)
        print(f"[MQTT] 바구니 탑재 완료 -> {'전송 완료' if ok else '전송 실패'}: {payload}")

    return "", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)