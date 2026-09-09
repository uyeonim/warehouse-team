import os
import json
import ssl
import uuid
import requests
import paho.mqtt.client as mqtt
from flask import Flask, request, abort, jsonify
from dotenv import load_dotenv

load_dotenv()

from upstash_redis import Redis

redis = Redis(
    url=os.environ["UPSTASH_REDIS_REST_URL"],
    token=os.environ["UPSTASH_REDIS_REST_TOKEN"]
)

MQTT_HOST = os.getenv("MQTT_HOST")
MQTT_PORT = int(os.getenv("MQTT_PORT", 8883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASS")

# 컨테이너 종류 → MQTT 토픽 (Mobius --웹훅--> Flask --publish--> MQTT, 주문/재고 push 전용)
# /notify/process 는 con 내용을 파싱하지 않고 그대로 relay만 함.
# orders/restock 파싱과 그에 따른 후속 처리(재고 체크, 큐 적재, restock 호출)는
# 전부 아두이노가 MQTT로 직접 받아서 처리함.
TOPICS = {
    "process": "sjuiot/warehouse/process",
}

# ---- Mobius (COSS API) 연결 정보: 기존 .env 변수 그대로 사용, 새로 추가하는 값 없음 ----
COSS_BASE_URL = os.getenv("COSS_BASE_URL")
COSS_API_KEY = os.getenv("COSS_API_KEY")
COSS_LECTURE = os.getenv("COSS_LECTURE")
COSS_CREATOR = os.getenv("COSS_CREATOR")
ORIGIN = os.getenv("ORIGIN")
AE_NAME = os.getenv("AE_NAME")

# ---- 재고 관리 상수 ----
BOARD_LETTERS = {
    "EI": ["E", "I"],
    "NS": ["N", "S"],
    "TF": ["T", "F"],
    "PJ": ["P", "J"],
}
COLORS = ["r", "y", "b", "g"]  # r=핑크, y=옐로우, b=블루, g=그린
RAIL_CAPACITY = 30  # 카트리지(stock) 최대 장전 개수, 아두이노 MAX_CAPACITY와 동일

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
    cnt_path 는 AE 기준 상대경로 (여러 단계 가능).
    예: "cnt_warehouse/cnt_keycap_status/cnt_E_r", "cnt_warehouse/cnt_keycap_dispense"
    실제 트리: {AE}/cnt_warehouse/cnt_keycap_status/cnt_E_r · cnt_S_y · … (32개)
             {AE}/cnt_warehouse/cnt_keycap_dispense
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
    return "Flask 서버 정상 동작 중"


# ============================================================
# 인바운드: Mobius --웹훅--> Flask --publish--> MQTT
# con 내용은 파싱하지 않고 그대로 relay만 함 (orders/restock 처리는 아두이노 담당)
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

    # 아두이노가 보고한 값 그대로 덮어씀 (재고 원본은 Redis, 아두이노는 캐시)
    if count is not None:
        redis.set(f"stock:{letter_color}", count)

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

    # Redis는 건드리지 않음 - 재고 차감은 /report/keycap-status(SET)에서 이미 처리됨.
    # 여기는 Mobius에 배출 이력을 CIN으로 남기는 용도만.
    content = {"order_id": order_id, "keycap": keycap}

    ok = post_cin_to_mobius("cnt_warehouse/cnt_keycap_dispense", content)
    print(f"[report/keycap-dispense] {'성공' if ok else '실패'}:", content)

    if not ok:
        return jsonify({"error": "mobius CIN 생성 실패"}), 502
    return jsonify({"result": "ok"}), 200


# ============================================================
# 아두이노 부팅 시 재고 초기 조회
# 응답 형식: {"E_r":8,"E_y":8,"E_b":8,"E_g":8,"I_r":8,"I_y":8,"I_b":8,"I_g":8}
# ============================================================
@app.route("/stock/keycap", methods=["GET"])
def get_stock_keycap():
    board = request.args.get("board")
    if board not in BOARD_LETTERS:
        return jsonify({"error": "board는 EI/NS/TF/PJ 중 하나여야 함"}), 400

    keys = [f"stock:{letter}_{color}" for letter in BOARD_LETTERS[board] for color in COLORS]
    values = redis.mget(*keys)

    result = {}
    for key, value in zip(keys, values):
        letter_color = key.split(":", 1)[1]
        result[letter_color] = int(value or 0)
    return jsonify(result), 200


# ============================================================
# 보드별 대기 큐 (재부팅 시 미완료 주문 복원용)
# ============================================================
def _queue_item(order_id: str, letter: str, color: str) -> str:
    return json.dumps({"order_id": order_id, "letter": letter, "color": color}, ensure_ascii=False)


@app.route("/queue/<board>/next", methods=["GET"])
def queue_next(board):
    if board not in BOARD_LETTERS:
        abort(404)
    items = redis.lrange(f"queue:{board}", 0, -1)
    result = [json.loads(item) for item in items]
    return jsonify(result), 200


@app.route("/queue/<board>/enqueue", methods=["POST"])
def queue_enqueue(board):
    if board not in BOARD_LETTERS:
        abort(404)

    data = request.get_json(force=True, silent=True) or {}
    order_id = data.get("order_id")
    letter = data.get("letter")
    color = data.get("color")
    if not order_id or not letter or not color:
        return jsonify({"error": "order_id, letter, color는 필수"}), 400

    redis.rpush(f"queue:{board}", _queue_item(order_id, letter, color))
    return jsonify({"result": "ok"}), 200


@app.route("/queue/<board>/complete", methods=["POST"])
def queue_complete(board):
    if board not in BOARD_LETTERS:
        abort(404)

    data = request.get_json(force=True, silent=True) or {}
    order_id = data.get("order_id")
    letter = data.get("letter")
    color = data.get("color")
    if not order_id or not letter or not color:
        return jsonify({"error": "order_id, letter, color는 필수"}), 400

    redis.lrem(f"queue:{board}", 1, _queue_item(order_id, letter, color))
    return jsonify({"result": "ok"}), 200


# ============================================================
# restock: 아두이노가 감지해서 호출, pool에서 stock으로 채워 넣고
# 채운 양을 응답으로 반환 (아두이노가 로컬 count 동기화에 씀)
# 응답 형식: {"filled": 25}
# ============================================================
@app.route("/restock/<letter>/<color>", methods=["POST"])
def restock_letter_color(letter, color):
    letter_color = f"{letter}_{color}"
    pool_key = f"pool:{letter_color}"  # 색상별로 분리 (예: pool:E_r), 글자 단위 pool:E 아님

    pool_qty = int(redis.get(pool_key) or 0)
    fill_amount = min(RAIL_CAPACITY, pool_qty)

    redis.set(f"stock:{letter_color}", fill_amount)
    if fill_amount > 0:
        redis.decrby(pool_key, fill_amount)

    print(f"[restock] {letter_color}: {fill_amount}개로 채움 (pool 남음 {pool_qty - fill_amount})")
    return jsonify({"filled": fill_amount}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)