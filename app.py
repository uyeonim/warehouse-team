import os
import json
import ssl
import uuid
import requests
import paho.mqtt.client as mqtt
from flask import Flask, request, abort, jsonify
from dotenv import load_dotenv

load_dotenv()

MQTT_HOST = os.getenv("MQTT_HOST")
MQTT_PORT = int(os.getenv("MQTT_PORT", 8883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASS")

# 컨테이너 종류 -> MQTT 토픽 (Mobius --웹훅--> Flask --publish--> MQTT, 주문/재고 push 전용)
# cnt_process 하나가 orders/key_restock/board_restock을 다 담고 있어서
# 기존 order/replace/load 구독은 폐기하고 process 하나로 통합.
#
# 주의: 라우트가 /notify/<kind> 형태라 실제 웹훅 URL은 "kind" 값 그대로 사용됨.
#   cnt_process 알림을 받을 Mobius 구독의 nu는 반드시
#   ".../notify/process" 로 등록할 것 (".../notify/cnt_process" 아님)
TOPICS = {
    "process": "sjuiot/warehouse/process",
}

# ---- Mobius (COSS API) 연결 정보 ----
# 변수명은 ORIGIN / AE_NAME으로 통일 (팀 표준).
# register_process_subscription.py 등 다른 스크립트도 이 이름 기준으로 맞출 것.
COSS_BASE_URL = os.getenv("COSS_BASE_URL")
COSS_API_KEY = os.getenv("COSS_API_KEY")
COSS_LECTURE = os.getenv("COSS_LECTURE")
COSS_CREATOR = os.getenv("COSS_CREATOR")
ORIGIN = os.getenv("ORIGIN")
AE_NAME = os.getenv("AE_NAME")

app = Flask(__name__)


def make_mqtt_client() -> mqtt.Client:
    """
    paho-mqtt 1.x / 2.x 버전 상관없이 동작하도록 처리.
    2.x는 CallbackAPIVersion을 요구하고(안 주면 DeprecationWarning 또는 향후 에러),
    1.x는 그 속성 자체가 없어서 그냥 mqtt.Client()로 호출해야 함.
    """
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    return mqtt.Client()


def publish(topic: str, payload: str) -> bool:
    c = make_mqtt_client()
    c.username_pw_set(MQTT_USER, MQTT_PASS)
    c.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
    try:
        c.connect(MQTT_HOST, MQTT_PORT, keepalive=10)
        c.loop_start()
        info = c.publish(topic, payload, qos=1)
        info.wait_for_publish(timeout=5)
        return info.is_published()
    except Exception as e:
        print("MQTT 오류:", e)
        return False
    finally:
        c.loop_stop()
        c.disconnect()


def post_cin_to_mobius(cnt_path: str, content: dict) -> bool:
    """
    {COSS_BASE_URL}/{AE_NAME}/{cnt_path} 밑에 CIN(콘텐츠 인스턴스)을 하나 생성.
    cnt_path 는 AE 기준 상대경로 (여러 단계 가능).
    예: "cnt_warehouse/cnt_keycap_status/cnt_E_r", "cnt_warehouse/cnt_keycap_dispense"
    실제 트리: {AE}/cnt_warehouse/cnt_keycap_status/cnt_E_r · cnt_S_y · ... (32개)
             {AE}/cnt_warehouse/cnt_keycap_dispense
    """
    url = f"{COSS_BASE_URL.rstrip('/')}/{AE_NAME}/{cnt_path.strip('/')}"
    headers = {
        "X-M2M-Origin": ORIGIN,
        "X-M2M-RI": str(uuid.uuid4()),
        "X-M2M-RVI": "3",
        "X-API-KEY": COSS_API_KEY,
        "X-AUTH-CUSTOM-CREATOR": COSS_CREATOR,
        "X-AUTH-CUSTOM-LECTURE": COSS_LECTURE,
        # Postman에서 실제로 CIN 생성 성공(200/201)이 검증된 형식으로 통일
        "Content-Type": "application/vnd.onem2m-res+json;ty=4",
        "Accept": "application/json",
    }
    body = {
        "m2m:cin": {
            "con": json.dumps(content, ensure_ascii=False)
        }
    }

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=5)
        ok = 200 <= resp.status_code < 300
        if not ok:
            print(f"[Mobius] CIN 생성 실패 ({resp.status_code}):", resp.text)
        return ok
    except Exception as e:
        print("[Mobius] 요청 오류:", e)
        return False


@app.route("/")
def hello():
    return "Flask 서버 정상 동작 중"


# ============================================================
# 인바운드: Mobius --웹훅--> Flask --publish--> MQTT (주문/재고 push)
# ============================================================
# 실제 경로: /notify/process  (TOPICS 딕셔너리의 키를 그대로 씀)
@app.route("/notify/<kind>", methods=["POST"])
def notify(kind):
    if kind not in TOPICS:
        abort(404)

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
        ok = publish(TOPICS[kind], payload)
        print(f"[{kind}] -> MQTT {'전송 완료' if ok else '전송 실패'}:", payload)
    return "", 200


# ============================================================
# 아웃바운드: 아두이노 --HTTPS POST--> Flask --> Mobius CIN
# ============================================================
@app.route("/report/keycap-status", methods=["POST"])
def report_keycap_status():
    data = request.get_json(force=True, silent=True) or {}

    letter_color = data.get("letter_color")  # 예: "E_r"
    order_id = data.get("order_id")
    count = data.get("count")
    status = data.get("status")

    if not letter_color or status is None:
        return jsonify({"error": "letter_color, status는 필수"}), 400

    content = {"order_id": order_id, "count": count, "status": status}
    cnt_path = f"cnt_warehouse/cnt_keycap_status/cnt_{letter_color}"

    ok = post_cin_to_mobius(cnt_path, content)
    print(f"[report/keycap-status] {cnt_path} {'성공' if ok else '실패'}:", content)

    if not ok:
        return jsonify({"error": "mobius CIN 생성 실패"}), 502
    return jsonify({"result": "ok"}), 200


@app.route("/report/keycap-dispense", methods=["POST"])
def report_keycap_dispense():
    data = request.get_json(force=True, silent=True) or {}

    order_id = data.get("order_id")
    keycap = data.get("keycap")  # 예: "E_r"

    if not order_id or not keycap:
        return jsonify({"error": "order_id, keycap은 필수"}), 400

    content = {"order_id": order_id, "keycap": keycap}

    ok = post_cin_to_mobius("cnt_warehouse/cnt_keycap_dispense", content)
    print(f"[report/keycap-dispense] {'성공' if ok else '실패'}:", content)

    if not ok:
        return jsonify({"error": "mobius CIN 생성 실패"}), 502
    return jsonify({"result": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)