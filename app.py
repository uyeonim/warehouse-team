import os
import json
import ssl
import uuid
import requests
import paho.mqtt.client as mqtt
from flask import Flask, request, abort, jsonify
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# 이 Flask는 "로봇암/컨베이어 파트" 전용 릴레이.
# 키캡 카트리지 파트(팀원 Cloud Run)와는 완전히 분리된 인프라:
#   - 별도 Cloud Run 서비스
#   - 별도 HiveMQ 계정/크리덴셜
#   - 별도 Mobius 구독 (sub 이름이 팀원 것과 겹치면 안 됨!)
# Mobius AE/인증 정보(COSS_*)는 팀 공용 값을 그대로 씀 (원래 팀 공용
# 크리덴셜이라 여기서 새로 만들 필요 없음)
# ============================================================

MQTT_HOST = os.getenv("MQTT_HOST")
MQTT_PORT = int(os.getenv("MQTT_PORT", 8883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASS")

# 컨테이너 종류 -> MQTT 토픽 (Mobius --웹훅--> Flask --publish--> MQTT)
# /notify/process 는 con 내용을 파싱하지 않고 그대로 relay만 함.
# 로봇암 PC(robot_worker.py 등)가 이 토픽을 구독해서 자기 status에 반응함.
TOPICS = {
    "process": os.getenv("MQTT_TOPIC", "sjuiot/warehouse/robotarm"),
}

# ---- Mobius (COSS API) 연결 정보: 팀 공용 .env 값 그대로 사용 ----
COSS_BASE_URL = os.getenv("COSS_BASE_URL")
COSS_API_KEY = os.getenv("COSS_API_KEY")
COSS_LECTURE = os.getenv("COSS_LECTURE")
COSS_CREATOR = os.getenv("COSS_CREATOR")
ORIGIN = os.getenv("ORIGIN")
AE_NAME = os.getenv("AE_NAME")

app = Flask(__name__)


def publish(topic: str, payload: str) -> bool:
    c = mqtt.Client()
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
    cnt_path 예:
      "cnt_robot_arm/cnt_tray_robot_arm"
      "cnt_robot_arm/cnt_board_robot_arm"
      "cnt_robot_arm/cnt_keycap_robot_arm"
      "cnt_conveyor/cnt_keycap_conveyor"
      "cnt_conveyor/cnt_tray_conveyor"
    """
    url = f"{COSS_BASE_URL.rstrip('/')}/{AE_NAME}/{cnt_path.strip('/')}"
    headers = {
        "X-API-KEY": COSS_API_KEY,
        "X-AUTH-CUSTOM-LECTURE": COSS_LECTURE,
        "X-AUTH-CUSTOM-CREATOR": COSS_CREATOR,
        "Accept": "application/json",
        "X-M2M-RI": str(uuid.uuid4()),
        "X-M2M-RVI": "3",
        "X-M2M-Origin": ORIGIN,
        "Content-Type": "application/json;ty=4",
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
    return "로봇암 Flask 서버 정상 동작 중"


# ============================================================
# 인바운드: Mobius --웹훅--> Flask --publish--> MQTT
# con 내용은 파싱하지 않고 그대로 relay만 함
# (status별 로봇암 동작 분기는 robot_worker.py 쪽에서 처리)
# ============================================================
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
        print(f"[{kind}] → MQTT {'전송 완료' if ok else '전송 실패'}:", payload)
    return "", 200


# ============================================================
# 아웃바운드: 로봇암 PC --HTTPS POST--> Flask --> Mobius CIN
# ============================================================

@app.route("/report/tray-robot-arm", methods=["POST"])
def report_tray_robot_arm():
    """
    빈 트레이 벨트 투입 결과 보고.
    body: {"order_id": "...", "status": "done" | "failed"}
    스펙: cnt_tray_robot_arm - status:done만 확인용(스테이지 전이 없음),
          failed면 플랫폼이 fault:tray_load_failed 로 판정
    """
    data = request.get_json(force=True, silent=True) or {}
    order_id = data.get("order_id")
    status = data.get("status")

    if not order_id or status not in ("done", "failed"):
        return jsonify({"error": "order_id, status(done/failed)는 필수"}), 400

    content = {"order_id": order_id, "status": status}
    ok = post_cin_to_mobius("cnt_robot_arm/cnt_tray_robot_arm", content)
    print(f"[report/tray-robot-arm] {'성공' if ok else '실패'}:", content)

    if not ok:
        return jsonify({"error": "mobius CIN 생성 실패"}), 502
    return jsonify({"result": "ok"}), 200


@app.route("/report/board-robot-arm", methods=["POST"])
def report_board_robot_arm():
    """
    보드 카트리지 -> 트레이 직접 투입 결과 보고.
    body: {"order_id": "...", "status": "done" | "failed"}
    색상 대조(cnt_board_dispense)는 카트리지 쪽 별도 보고이며 여기선 다루지 않음.
    """
    data = request.get_json(force=True, silent=True) or {}
    order_id = data.get("order_id")
    status = data.get("status")

    if not order_id or status not in ("done", "failed"):
        return jsonify({"error": "order_id, status(done/failed)는 필수"}), 400

    content = {"order_id": order_id, "status": status}
    ok = post_cin_to_mobius("cnt_robot_arm/cnt_board_robot_arm", content)
    print(f"[report/board-robot-arm] {'성공' if ok else '실패'}:", content)

    if not ok:
        return jsonify({"error": "mobius CIN 생성 실패"}), 502
    return jsonify({"result": "ok"}), 200


@app.route("/report/keycap-robot-arm", methods=["POST"])
def report_keycap_robot_arm():
    """
    키캡 로봇암 - 트레이에 1개 넣을 때마다 1건 보고 (주문당 4건).
    body: {"order_id": "...", "keycap_index": 1~4, "status": "done" | "failed"}
    스펙: {order_id, keycap1:"done"} 형태로 필드명이 keycapN
    """
    data = request.get_json(force=True, silent=True) or {}
    order_id = data.get("order_id")
    keycap_index = data.get("keycap_index")
    status = data.get("status")

    if not order_id or keycap_index not in (1, 2, 3, 4) or status not in ("done", "failed"):
        return jsonify({"error": "order_id, keycap_index(1~4), status(done/failed)는 필수"}), 400

    content = {"order_id": order_id, f"keycap{keycap_index}": status}
    ok = post_cin_to_mobius("cnt_robot_arm/cnt_keycap_robot_arm", content)
    print(f"[report/keycap-robot-arm] {'성공' if ok else '실패'}:", content)

    if not ok:
        return jsonify({"error": "mobius CIN 생성 실패"}), 502
    return jsonify({"result": "ok"}), 200


@app.route("/report/conveyor/<which>", methods=["POST"])
def report_conveyor(which):
    """
    컨베이어 이송 결과 보고. which = "keycap" | "tray"

    keycap 컨베이어: 4개를 한 번에 이송하는 물리 동작 1회라 한 건에 담음
      body: {"order_id": "...", "keycap1": "done", "keycap2": "done",
             "keycap3": "done", "keycap4": "failed"}  (4개 다 명시)

    tray 컨베이어: section1 / section2 각각 별도 보고
      body: {"order_id": "...", "section": "1" | "2", "status": "done" | "failed"}
    """
    data = request.get_json(force=True, silent=True) or {}
    order_id = data.get("order_id")
    if not order_id:
        return jsonify({"error": "order_id는 필수"}), 400

    if which == "keycap":
        keys = ["keycap1", "keycap2", "keycap3", "keycap4"]
        missing = [k for k in keys if data.get(k) not in ("done", "failed")]
        if missing:
            return jsonify({"error": f"{missing} 값(done/failed)이 필요함"}), 400
        content = {"order_id": order_id, **{k: data[k] for k in keys}}
        cnt_path = "cnt_conveyor/cnt_keycap_conveyor"

    elif which == "tray":
        section = data.get("section")
        status = data.get("status")
        if section not in ("1", "2") or status not in ("done", "failed"):
            return jsonify({"error": "section(1/2), status(done/failed)는 필수"}), 400
        content = {"order_id": order_id, f"section{section}": status}
        cnt_path = "cnt_conveyor/cnt_tray_conveyor"

    else:
        abort(404)

    ok = post_cin_to_mobius(cnt_path, content)
    print(f"[report/conveyor/{which}] {'성공' if ok else '실패'}:", content)

    if not ok:
        return jsonify({"error": "mobius CIN 생성 실패"}), 502
    return jsonify({"result": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)