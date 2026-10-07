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

# ============================================================
# 이 Flask는 "로봇암/컨베이어 파트" 전용 릴레이.
# 키캡 카트리지 파트(팀원 Cloud Run)와는 완전히 분리된 인프라:
#   - 별도 Cloud Run 서비스
#   - 별도 HiveMQ 계정/크리덴셜
#   - 별도 Mobius 구독 (sub 이름이 팀원 것과 겹치면 안 됨!)
# Mobius AE/인증 정보(COSS_*)는 팀 공용 값을 그대로 씀 (원래 팀 공용
# 크리덴셜이라 여기서 새로 만들 필요 없음)
#
# 보드는 카트리지·아두이노가 없어서, 이 릴레이가 board 재고(Redis)도
# 같이 관리함 - 로봇암 PC가 busy/idle+count를 보고하면 여기서 캐싱.
#
# ⚠️ Redis 키 네이밍: 실제 초기 재고 시드 스크립트(seed_board_stock.py 등)가
# stock:board_{코드}, pool:board_{코드}, status:board_{코드} (코드=r/y/b/g
# 한 글자)로 이미 실제 데이터를 심어놨음 - 이 Flask도 반드시 같은 코드
# 체계를 써야 진짜 재고 데이터를 읽고 씀. (전에 stock:board_{전체이름}으로
# 잘못 짜여있던 버그를 여기서 수정함 - 키캡과 동일하게 코드 기준으로 통일)
# ============================================================

BOARD_COLORS = ["red", "yellow", "blue", "green"]
BOARD_STATUS_VALUES = ("idle", "busy", "disable", "empty")  # idle/busy/disable(오배출)/empty(재고부족)
BOARD_COLOR_CODE = {"red": "r", "yellow": "y", "blue": "b", "green": "g"}  # Redis 키 + Mobius 컨테이너명 공용
BOARD_CARTRIDGE_CAPACITY = 3  # 한 번에 진열하는 단위 개수 (restock 시 pool에서 이만큼씩 채움)

MQTT_HOST = os.getenv("MQTT_HOST")
MQTT_PORT = int(os.getenv("MQTT_PORT", 8883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASS")

# 컨테이너 종류 -> MQTT 토픽 (Mobius --웹훅--> Flask --publish--> MQTT)
# /notify/process 는 con 내용을 파싱하지 않고 그대로 relay만 함.
# 로봇암 PC(board_robot_arm.py)가 이 토픽을 구독해서 자기 status에 반응함.
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

# 필수 환경변수 누락을 배포 직후 부팅 로그에서 바로 드러나게 함
# (없으면 None이 조용히 들어갔다가, 나중에 요청 처리 중 .rstrip() 등에서
#  AttributeError로 터져서 502만 뜨고 원인 파악이 어려워짐)
_REQUIRED_ENV = {
    "COSS_BASE_URL": COSS_BASE_URL, "AE_NAME": AE_NAME, "ORIGIN": ORIGIN,
    "COSS_API_KEY": COSS_API_KEY, "COSS_CREATOR": COSS_CREATOR, "COSS_LECTURE": COSS_LECTURE,
    "MQTT_HOST": os.getenv("MQTT_HOST"), "MQTT_USER": os.getenv("MQTT_USER"), "MQTT_PASS": os.getenv("MQTT_PASS"),
}
_missing = [k for k, v in _REQUIRED_ENV.items() if not v]
if _missing:
    print(f"[BOOT] ⚠️ 환경변수 누락: {', '.join(_missing)} - 관련 기능이 500/502로 실패함")

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
    if not COSS_BASE_URL or not AE_NAME:
        print("[Mobius] COSS_BASE_URL/AE_NAME이 비어있음 - env.yaml/.env 확인 필요")
        return False

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
# (status별 로봇암 동작 분기는 board_robot_arm.py 쪽에서 처리)
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
    보드 로봇암 -> 트레이 직접 투입 결과 보고.
    body: {"order_id": "...", "board": "done" | "failed"}
    스펙 필드명이 "board" (status 아님) - COSS 명세 cnt_board_robot_arm 그대로.
    실패 시 플랫폼이 fault:board_pack_failed 로 판정하고, 사람이 원인 해결 후
    fault:null 로 넘어오면 board_robot_arm.py가 재시도하고 이 라우트를 다시 호출함
    (재시도도 이 라우트를 그대로 재사용 - Flask 쪽 변경 불필요).
    색상 실측(cnt_board_dispense)은 /report/board-dispense 에서 별도 보고.
    """
    data = request.get_json(force=True, silent=True) or {}
    order_id = data.get("order_id")
    board = data.get("board")

    if not order_id or board not in ("done", "failed"):
        return jsonify({"error": "order_id, board(done/failed)는 필수"}), 400

    content = {"order_id": order_id, "board": board}
    ok = post_cin_to_mobius("cnt_robot_arm/cnt_board_robot_arm", content)
    print(f"[report/board-robot-arm] {'성공' if ok else '실패'}:", content)

    if not ok:
        return jsonify({"error": "mobius CIN 생성 실패"}), 502
    return jsonify({"result": "ok"}), 200


@app.route("/report/board-dispense", methods=["POST"])
def report_board_dispense():
    """
    실제로 배출(픽업)한 보드 색상 보고 - 플랫폼이 주문 색과 대조하는 용도.
    body: {"order_id": "...", "board": "blue"}   (색 이름 그대로, r/y/b/g 코드 아님)
    스펙: cnt_warehouse/cnt_board_dispense
    """
    data = request.get_json(force=True, silent=True) or {}
    order_id = data.get("order_id")
    color = data.get("board")

    if not order_id or color not in BOARD_COLORS:
        return jsonify({"error": f"order_id, board({'/'.join(BOARD_COLORS)})는 필수"}), 400

    content = {"order_id": order_id, "board": color}
    ok = post_cin_to_mobius("cnt_warehouse/cnt_board_dispense", content)
    print(f"[report/board-dispense] {'성공' if ok else '실패'}:", content)

    if not ok:
        return jsonify({"error": "mobius CIN 생성 실패"}), 502
    return jsonify({"result": "ok"}), 200


@app.route("/report/board-status", methods=["POST"])
def report_board_status():
    """
    보드 색상별 카트리지(레일) 상태 보고 (아두이노가 없어서 로봇암 PC가 대신 보고).
    body: {"order_id": "...", "board": "blue", "status": "idle"|"busy"|"disable"|"empty", "count": 5}
    - order_id: 이 레일을 마지막으로 사용한 주문 (restock 등 주문 무관 이벤트는 null)
    - count: 현재 재고 수량
    - status: idle(대기) / busy(배출 중) / disable(오배출) / empty(재고 부족)
    스펙: cnt_warehouse/cnt_board_status/cnt_{r|y|b|g}  예) cnt_warehouse/cnt_board_status/cnt_r

    Redis: stock:board_{코드}(현재 수량), status:board_{코드}(재부팅 시 마지막 상태
    복원용 - board_robot_arm.py는 직접 안 읽고, 사람이 현황 확인용으로 조회 가능하게 남김)
    """
    data = request.get_json(force=True, silent=True) or {}
    order_id = data.get("order_id")  # None 허용 (restock 등 주문 무관 이벤트)
    color = data.get("board")
    status = data.get("status")
    count = data.get("count")

    if color not in BOARD_COLORS or status not in BOARD_STATUS_VALUES:
        return jsonify({
            "error": f"board({'/'.join(BOARD_COLORS)}), status({'/'.join(BOARD_STATUS_VALUES)})는 필수"
        }), 400
    if count is None:
        return jsonify({"error": "count는 필수"}), 400

    content = {"order_id": order_id, "count": count, "status": status}
    code = BOARD_COLOR_CODE[color]
    cnt_path = f"cnt_warehouse/cnt_board_status/cnt_{code}"

    ok = post_cin_to_mobius(cnt_path, content)
    print(f"[report/board-status] {cnt_path} {'성공' if ok else '실패'}:", content)

    if not ok:
        return jsonify({"error": "mobius CIN 생성 실패"}), 502

    # Mobius 성공 후에만 Redis 갱신 - 둘이 어긋난 상태로 안 남게
    redis.set(f"stock:board_{code}", count)
    redis.set(f"status:board_{code}", status)
    return jsonify({"result": "ok"}), 200


@app.route("/stock/board", methods=["GET"])
def get_stock_board():
    """
    로봇암 PC가 시작할 때 현재 재고를 읽어오는 용도.
    응답: {"red": 3, "yellow": 3, "blue": 3, "green": 3}
    (HTTP 계약은 전체 색상명 유지 - Redis 키 내부 표현(코드)와 분리)
    """
    keys = [f"stock:board_{BOARD_COLOR_CODE[c]}" for c in BOARD_COLORS]
    values = redis.mget(*keys)
    result = {c: int(v or 0) for c, v in zip(BOARD_COLORS, values)}
    return jsonify(result), 200


@app.route("/restock/board/<color>", methods=["POST"])
def restock_board(color):
    """
    board_restock 감지 시 로봇암 PC가 호출 - 키캡의 /restock/<letter>/<color>와
    동일한 패턴(pool에서 실제로 차감하며 채움, 고정값으로 덮어쓰지 않음).
    color: red/yellow/blue/green (전체 이름 - URL 경로 가독성 위해 코드 아님)
    응답: {"filled": N}  (로봇암 PC가 로컬 재고 갱신에 사용)
    """
    if color not in BOARD_COLORS:
        return jsonify({"error": f"color는 {'/'.join(BOARD_COLORS)} 중 하나"}), 400

    code = BOARD_COLOR_CODE[color]
    pool_key = f"pool:board_{code}"
    stock_key = f"stock:board_{code}"

    pool_qty = int(redis.get(pool_key) or 0)
    fill_amount = min(BOARD_CARTRIDGE_CAPACITY, pool_qty)

    redis.set(stock_key, fill_amount)
    if fill_amount > 0:
        redis.decrby(pool_key, fill_amount)
    redis.set(f"status:board_{code}", "idle")

    content = {"order_id": None, "count": fill_amount, "status": "idle"}
    ok = post_cin_to_mobius(f"cnt_warehouse/cnt_board_status/cnt_{code}", content)
    print(f"[restock/board/{color}] {fill_amount}개로 채움 (pool 남음 {pool_qty - fill_amount}), "
          f"모비우스 {'성공' if ok else '실패'}")

    if not ok:
        return jsonify({"error": "mobius CIN 생성 실패"}), 502
    return jsonify({"filled": fill_amount}), 200


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

    tray 컨베이어: section0 / section1 / section2 각각 별도 보고. 실패 시 플랫폼이
      fault:tray_00_belt_jammed(section0, 이름 임시) / tray_01_belt_jammed(section1) /
      tray_02_belt_jammed(section2)로 판정.
      fault 해제 후 재시도도 이 라우트를 그대로 재사용.
      body: {"order_id": "...", "section": "0" | "1" | "2", "status": "done" | "failed"}
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
        if section not in ("0", "1", "2") or status not in ("done", "failed"):
            return jsonify({"error": "section(0/1/2), status(done/failed)는 필수"}), 400
        content = {"order_id": order_id, f"section{section}": status}
        cnt_path = "cnt_conveyor/cnt_tray_conveyor"

    else:
        abort(404)

    ok = post_cin_to_mobius(cnt_path, content)
    print(f"[report/conveyor/{which}] {'성공' if ok else '실패'}:", content)

    if not ok:
        return jsonify({"error": "mobius CIN 생성 실패"}), 502
    return jsonify({"result": "ok"}), 200


# ============================================================
# 보드 픽업 대기열 (재부팅 시 미완료 주문 복원용) - 키캡의 /queue/{board}/* 패턴과 동일.
# 보드는 로봇암이 한 대뿐이라 {board} 자리는 고정값 "board" 하나만 씀.
# ============================================================
def _board_queue_item(order_id: str, color: str) -> str:
    return json.dumps({"order_id": order_id, "color": color}, ensure_ascii=False)


@app.route("/queue/board/next", methods=["GET"])
def board_queue_next():
    items = redis.lrange("queue:board", 0, -1)
    result = [json.loads(item) for item in items]
    return jsonify(result), 200


@app.route("/queue/board/enqueue", methods=["POST"])
def board_queue_enqueue():
    data = request.get_json(force=True, silent=True) or {}
    order_id = data.get("order_id")
    color = data.get("color")
    if not order_id or color not in BOARD_COLORS:
        return jsonify({"error": f"order_id, color({'/'.join(BOARD_COLORS)})는 필수"}), 400

    redis.rpush("queue:board", _board_queue_item(order_id, color))
    return jsonify({"result": "ok"}), 200


@app.route("/queue/board/complete", methods=["POST"])
def board_queue_complete():
    data = request.get_json(force=True, silent=True) or {}
    order_id = data.get("order_id")
    color = data.get("color")
    if not order_id or color not in BOARD_COLORS:
        return jsonify({"error": f"order_id, color({'/'.join(BOARD_COLORS)})는 필수"}), 400

    redis.lrem("queue:board", 1, _board_queue_item(order_id, color))
    return jsonify({"result": "ok"}), 200


# ============================================================
# 픽업 게이트 (안전망용) - 정상 흐름에서는 창고 벨트가 주문 1건만 태우므로
# tray_reached 시점에 이전 주문 흔적이 있을 수 없어 사실상 항상 즉시 통과함.
# 그래도 수동 테스트 스냅샷처럼 명세를 벗어난 입력이 들어올 경우를 대비한
# 안전장치로 유지 - 키캡의 /gate/{board}/* 패턴과 동일, {board} 자리 고정값 "board".
# Redis 키: gate:board (값 = 대기 중인 order_id, 키 자체가 없으면 "열림" 상태)
#
# ⚠️ fault→null 재시도(board_pack_failed 등)는 이 게이트를 거치지 않고
# board_robot_arm.py가 직접 재실행함 - 이미 게이트를 걸고 있는 주문 본인이
# 재시도하는 것이라 줄 설 필요가 없고, 큐를 거치면 "게이트가 열려야 큐를
# 처리하는데 게이트는 이 주문이 성공해야 열린다"는 데드락이 생기기 때문.
# ============================================================
def _board_gate_key() -> str:
    return "gate:board"


@app.route("/gate/board", methods=["GET"])
def get_board_gate_status():
    """
    로봇암 PC 부팅 시 게이트 상태 복원용 조회.
    응답: {"locked": true, "order_id": "order_test_8017"} 또는 {"locked": false}
    """
    order_id = redis.get(_board_gate_key())
    if not order_id:
        return jsonify({"locked": False}), 200
    return jsonify({"locked": True, "order_id": order_id}), 200


@app.route("/gate/board", methods=["POST"])
def lock_board_gate():
    """로봇암 PC가 픽업+컨베이어 직후(성공/실패 무관) 게이트를 잠글 때 호출."""
    data = request.get_json(force=True, silent=True) or {}
    order_id = data.get("order_id")
    if not order_id:
        return jsonify({"error": "order_id는 필수"}), 400

    redis.set(_board_gate_key(), order_id)
    print(f"[gate/board] 잠금: {order_id}")
    return jsonify({"result": "ok"}), 200


@app.route("/gate/board/clear", methods=["POST"])
def clear_board_gate():
    """로봇암 PC가 pickup_reached(이후) 확인해서 게이트를 열 때 호출."""
    redis.delete(_board_gate_key())
    print("[gate/board] 해제")
    return jsonify({"result": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)