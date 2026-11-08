import os
import json
import uuid
import logging
import sys
import threading
import time
import requests
import paho.mqtt.client as mqtt
import redis as redis_lib
from flask import Flask, request, abort, jsonify
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# [2026-11-01 로컬 전환] Cloud Run/Upstash/HiveMQ -> 창고 PC(192.168.0.12) 로컬 구성
#  - 이 Flask는 창고 PC에서 실행 (보드 Flask(5000)와 같은 PC, 포트 5002)
#  - Redis: Upstash(REST) -> 창고 PC의 로컬 Redis (REDIS_URL=redis://127.0.0.1:6379/0)
#    보드 Flask와 같은 Redis/DB를 같이 씀 (키 이름이 달라서 안 섞임: stock:E_r vs stock:board_r)
#    플랫폼 서버(192.168.0.100)의 Redis(DB 0)와는 별개의 서버라 DB 번호가 같아도 무관함.
#    변수명을 redis 그대로 두어서 아래 라우트 코드는 수정 없음.
#    decode_responses=True 필수 (없으면 값이 bytes로 나와서 json/비교가 깨짐)
#  - MQTT publish: TLS 제거. Mosquitto는 플랫폼 서버(192.168.0.100:1883)에 있음.
#    MQTT_USER가 비어있으면 익명 접속
#  - Mobius: 플랫폼 서버 주소(COSS_BASE_URL). 인증 헤더(X-API-KEY 등)는 값이 있을 때만 보냄
#  - 실행(Windows): gunicorn은 Windows에서 안 돌아감 -> waitress 사용
#      pip install waitress
#      waitress-serve --listen=0.0.0.0:5002 app:app
#
# [2026-11-04] cnt_process MQTT 발행을 retain=True로 변경
#  - 브로커가 마지막 스냅샷을 들고 있다가, 아두이노가 (재)접속해서 구독하는 순간 바로 보내줌.
#    cnt_process는 "진행 중인 주문 전체" 스냅샷이라, 끊긴 사이에 놓친 주문도 다 들어 있음.
#  - 11/03 테스트에서 아두이노가 "연결됨 + 수신 불가" 상태로 굳은 문제 대응의 일부.
#    아두이노 쪽은 구독 실패 시 재연결 + 자기 에코(핑) 확인으로 재접속하고, 재접속 직후
#    이 retained 스냅샷으로 최신 주문 목록을 다시 받음.
#  - 같은 토픽을 구독하는 다른 클라이언트(로봇팔 main.py 등)도 접속할 때마다 마지막
#    스냅샷을 받으므로, 같은 스냅샷을 다시 받아도 중복 동작하지 않는지 확인할 것.
#  - 아두이노 수정본(11/04: wasHandled/복원 재시도)을 4대 모두 올린 뒤에 이 Flask를
#    적용할 것. 구버전 아두이노는 재부팅 직후 retained 스냅샷을 받아 이미 배출한 주문을
#    다시 큐에 넣을 수 있음.
# ============================================================
_redis_url = os.getenv("REDIS_URL")
if _redis_url:
    redis = redis_lib.Redis.from_url(_redis_url, decode_responses=True)
else:
    redis = redis_lib.Redis(
        host=os.getenv("REDIS_HOST", "127.0.0.1"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        db=int(os.getenv("REDIS_DB", 0)),
        password=os.getenv("REDIS_PASS") or None,
        decode_responses=True,
    )

# ============================================================
# 로깅 설정: print() 대신 이걸 씀 — gunicorn 환경에서
# 버퍼링 없이 즉시 flush됨 (PYTHONUNBUFFERED 이슈 회피)
# ============================================================
# [11/09] 화면 + logs/flask_날짜.log 에도 같이 기록 (보드·게이트웨이 로그와 같은 방식, 시연 기록용)
#  같은 날 다시 켜면 같은 파일에 이어서 씀. 줄마다 바로 기록돼서 창을 강제로 닫아도 남음
_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(_log_dir, exist_ok=True)
LOG_FILE = os.path.join(_log_dir, f"flask_{time.strftime('%Y-%m-%d')}.log")
with open(LOG_FILE, "a", encoding="utf-8") as _f:
    _f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 시작 =====\n")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE, encoding="utf-8")],
    force=True,  # gunicorn이 이미 만들어둔 핸들러 무시하고 강제 재설정
)
logger = logging.getLogger("warehouse")
logger.info(f"로그 파일: {LOG_FILE}")

MQTT_HOST = os.getenv("MQTT_HOST", "192.168.0.100")  # Mosquitto는 플랫폼 서버에 있음 (창고 PC의 localhost 아님)
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")

# 컨테이너 종류 → MQTT 토픽 (Mobius --웹훅--> Flask --publish--> MQTT, 주문/재고 push 전용)
# /notify/process 는 con 내용을 파싱하지 않고 그대로 relay만 함.
# orders/restock 파싱과 그에 따른 후속 처리(재고 체크, 큐 적재, restock 호출)는
# 전부 아두이노가 MQTT로 직접 받아서 처리함.
TOPICS = {
    "process": "sjuiot/warehouse/process",
}

# ---- Mobius 연결 정보 ----
# 로컬 Mobius는 인증 헤더 없이도 응답함(플랫폼팀 확인) -> API_KEY가 비어있으면 헤더 생략
COSS_BASE_URL = os.getenv("COSS_BASE_URL", "http://192.168.0.100:7579/Mobius")  # Mobius도 플랫폼 서버
COSS_API_KEY = os.getenv("COSS_API_KEY", "")
COSS_LECTURE = os.getenv("COSS_LECTURE", "")
COSS_CREATOR = os.getenv("COSS_CREATOR", "")
ORIGIN = os.getenv("ORIGIN")
AE_NAME = os.getenv("AE_NAME")

# ---- 재고 관리 상수 ----
# "board" 라는 단어가 두 가지 의미로 쓰이는 점 주의:
#   - BOARD_LETTERS 의 board: EI/NS/TF/PJ 축(키캡이 꽂히는 보드 종류)
#   - BOARD_COLORS 의 board: 색상별로만 구분되는 피지컬 보드 "상품" 자체
BOARD_LETTERS = {
    "EI": ["E", "I"],
    "NS": ["N", "S"],
    "TF": ["T", "F"],
    "PJ": ["P", "J"],
}
COLORS = ["r", "y", "b", "g"]  # r=핑크, y=옐로우, b=블루, g=그린
RAIL_CAPACITY = 40  # 카트리지(stock) 최대 장전 개수, 아두이노 MAX_CAPACITY와 동일 (11/09 실물 40개, 예전 30)

# 보드(피지컬 보드 상품) 재고 상수 — 알파벳 축 구분 없이 색상으로만 관리
# 모비우스 cnt_board_status 밑 컨테이너가 cnt_r/cnt_y/cnt_b/cnt_g로 확인되어
# 키캡과 동일한 한 글자 색상 코드를 그대로 재사용함 (cnt_red 같은 전체 단어 아님)
BOARD_COLORS = COLORS  # ["r", "y", "b", "g"]
BOARD_CARTRIDGE_CAPACITY = 20  # 카트리지에 실제로 장전되는 재고
# [11/04] 전시 총 재고표 (카트리지 포함, 색마다 같은 수) - 하드리셋 때 pool = 총량 - 카트리지분
#   보드 66×5 = 330/색 (예전 300), 키캡은 글자별 (문자 키캡 ★·꽃 등은 카트리지 칸이 없어 제외)
BOARD_POOL_TOTAL = 330  # 색상별 전체 재고 총량(카트리지 포함) — 창고 pool = 이 값 - 카트리지분
KEYCAP_TOTAL_PER_COLOR = {"E": 190, "I": 170, "N": 160, "S": 200, "T": 170, "F": 200, "P": 180, "J": 180}
# [11/09] 빈 트레이 - 더미 3개 × 5층. 다 쓰면 사람이 모아서 다시 쌓음 (pool 없음). 게이트웨이 tray_stock.TOTAL과 같게
TRAY_TOTAL = 15
TRAY_STATUS_PATH = "cnt_warehouse/cnt_tray_status"   # 플랫폼에 원래 없음 - 처음 올릴 때 키캡 Flask가 만듦 (_ensure_tray_cnt)

app = Flask(__name__)


def _new_mqtt_client():
    # paho-mqtt 2.x는 callback_api_version 인자가 필수, 1.x는 없음 -> 둘 다 대응
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    except AttributeError:
        return mqtt.Client()


def publish(topic: str, payload: str, retain: bool = True) -> bool:
    c = _new_mqtt_client()
    if MQTT_USER:  # 계정이 설정된 경우에만 인증 (비어있으면 익명 접속)
        c.username_pw_set(MQTT_USER, MQTT_PASS)
    # 로컬 Mosquitto(1883)는 평문이라 tls_set() 호출 없음
    try:
        c.connect(MQTT_HOST, MQTT_PORT, keepalive=10)
        c.loop_start()
        # [11/04] retain=True: 브로커가 마지막 스냅샷을 보관했다가 새로 구독하는
        # 클라이언트(재접속/재부팅한 아두이노)에게 바로 보내줌 (cnt_process 스냅샷).
        # 복구 명령(REPAIR_TOPIC)은 retain=False - retain이면 재접속할 때마다 복구가 또 실행됨 (키캡팀 11/04)
        info = c.publish(topic, payload, qos=1, retain=retain)
        info.wait_for_publish(timeout=5)
        return info.is_published()
    except Exception as e:
        logger.info(f"MQTT 오류: {e}")
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
             {AE}/cnt_warehouse/cnt_board_status/cnt_r · cnt_y · cnt_b · cnt_g (4개)
    """
    url = f"{COSS_BASE_URL.rstrip('/')}/{AE_NAME}/{cnt_path.strip('/')}"
    headers = {
        "Accept": "application/json",
        "X-M2M-RI": str(uuid.uuid4()),
        "X-M2M-RVI": "3",
        "X-M2M-Origin": ORIGIN,
        "Content-Type": "application/json;ty=4",
    }
    if COSS_API_KEY:  # 로컬 Mobius는 인증 헤더가 필요 없음 - 값이 있을 때만 추가
        headers["X-API-KEY"] = COSS_API_KEY
        headers["X-AUTH-CUSTOM-LECTURE"] = COSS_LECTURE
        headers["X-AUTH-CUSTOM-CREATOR"] = COSS_CREATOR
    body = {
        "m2m:cin": {
            "con": json.dumps(content, ensure_ascii=False)
        }
    }

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=5)
        ok = 200 <= resp.status_code < 300
        if not ok:
            logger.info(f"[Mobius] CIN 생성 실패 ({resp.status_code}): {resp.text}")
        return ok
    except Exception as e:
        logger.info(f"[Mobius] 요청 오류: {e}")
        return False


def get_last_status(status_key: str) -> str:
    """
    가장 최근 /report/*-status로 보고된 status를 반환.
    아직 한 번도 보고된 적 없으면(신규 설치 등) "idle"로 폴백.
    """
    value = redis.get(status_key)
    return value if value else "idle"


@app.route("/")
def hello():
    return "Flask 서버 정상 동작 중"


# ============================================================
# 인바운드: Mobius --웹훅--> Flask --publish--> MQTT
# con 내용은 파싱하지 않고 그대로 relay만 함 (orders/restock 처리는 아두이노 담당)
# [11/04] publish()가 retain=True로 보내므로, 마지막 스냅샷이 브로커에 보관됨
# ============================================================
@app.route("/notify/<kind>", methods=["POST"])
def notify(kind):
    logger.debug(f"[notify/{kind}] 요청 수신됨")   # 원본 덤프는 DEBUG로 (11/05 - 한 줄에 스냅샷이 4번씩 찍혀서 못 읽었음)

    if kind not in TOPICS:
        logger.warning(f"[notify/{kind}] 등록 안 된 kind, 404 리턴")
        abort(404)

    raw_body = request.get_data(as_text=True)
    logger.debug(f"[notify/{kind}] raw body: {raw_body}")

    data = request.get_json(force=True, silent=True) or {}
    if not data:
        logger.warning(f"[notify/{kind}] JSON 파싱 실패 또는 빈 body")

    sgn = data.get("m2m:sgn", {})
    if not sgn:
        logger.warning(f"[notify/{kind}] 'm2m:sgn' 키 자체가 없음. data keys: {list(data.keys())}")

    if sgn.get("vrq"):
        logger.info(f"[notify/{kind}] 구독 검증 요청(vrq)임 → 200만 응답하고 종료")
        return "", 200

    cin = sgn.get("nev", {}).get("rep", {}).get("m2m:cin")
    if not cin:
        logger.warning(f"[notify/{kind}] cin을 못 찾음. sgn 내용: {sgn}")
        return "", 200

    con = cin.get("con")
    logger.debug(f"[notify/{kind}] cin 찾음. con(raw): {con}")

    if isinstance(con, str):
        try:
            con = json.loads(con)
        except ValueError as e:
            logger.error(f"[notify/{kind}] con JSON 파싱 실패: {e}, con 원본: {con}")

    _relay(kind, con, "알림")
    return "", 200


def _summary(con):
    """스냅샷을 사람이 읽기 좋게: 주문 하나에 한 줄 + 보충 신호 (로그용)"""
    if not isinstance(con, dict):
        return f"    {con}"
    lines = []
    for oid, o in (con.get("orders") or {}).items():
        o = o if isinstance(o, dict) else {}
        colors = ",".join(o.get("colors") or [])
        fault = f"  fault={o.get('fault')}" if o.get("fault") else ""
        lines.append(f"    {oid:<24} {str(o.get('status')):<16} {o.get('board')} {o.get('keycap')} [{colors}]{fault}")
    if not lines:
        lines.append("    (주문 없음)")
    for key in ("keycap_restock", "board_restock"):
        if con.get(key):
            lines.append(f"    {key}: {', '.join(map(str, con[key]))}")
    if con.get("reset"):
        lines.append("    reset: true")
    return "\n".join(lines)


STALE_SEQ_GAP = 10   # 이만큼 안쪽으로 작은 seq = 늦게 도착한 옛 스냅샷. 더 크게 작아지면 플랫폼 서버 재시작으로 봄


def _is_stale(con):
    """방금 보낸 것보다 옛 스냅샷인지 (_relay_lock 안에서 부름). 알림 두 개가 거의 동시에 오면(플랫폼 restock은
    보고 30ms 뒤) 처리 스레드가 달라서 순서가 뒤집힐 수 있음 -> 아두이노가 옛 스냅샷을 나중에 받으면 새 주문을
    큐에서 지워 버림 (11/05 검토). updated_at이 양쪽에 있으면 그걸로, 없으면 seq 차이로 판단"""
    if not isinstance(con, dict) or _last_key is None:
        return False
    last_seq, last_upd = _last_key
    seq, upd = con.get("seq"), con.get("updated_at")
    if isinstance(upd, str) and isinstance(last_upd, str) and upd and last_upd:
        return upd < last_upd
    if isinstance(seq, int) and isinstance(last_seq, int):
        return last_seq - STALE_SEQ_GAP <= seq < last_seq
    return False


def _relay(kind, con, via, gen=None):
    """스냅샷을 MQTT로 보냄. 보낸 내용을 기억해서 주기 조회(_process_poll_loop)가 같은 걸 또 보내지 않게 함.
    [11/05] 비교·전송·기억을 _relay_lock 하나로 묶음 - 알림 스레드와 주기 조회가 동시에 보내면 순서가 뒤집혔음.
    gen: 주기 조회가 Mobius를 읽기 전의 _relay_gen. 그 사이 다른 스냅샷이 나갔으면 읽은 게 옛것일 수 있어서 버림"""
    global _last_relayed, _last_key, _relay_gen
    reset_con = None
    with _relay_lock:
        if gen is not None and gen != _relay_gen:
            return True   # 읽는 동안 알림이 더 새것을 보냄 - 다음 조회가 다시 확인
        if kind == "process" and _is_stale(con):
            logger.warning(f"[{kind}] seq {con.get('seq')} 건너뜀 - 방금 보낸 seq {_last_key[0]}보다 옛 스냅샷 ({via})")
            return True
        reset_con = _auto_hard_reset(con) if kind == "process" else None
        # 하드리셋 땐 아두이노 수량 맞추기용 keycap_restock을 실은 스냅샷을 seq를 바꿔서 보냄 ("<seq>-reset").
        # 원래 seq 그대로면 주기 조회가 원래 스냅샷을 먼저 보낸 경우 아두이노가 "이미 받은 seq"로 무시했음 (11/05).
        # 다음 주기 조회(5초 안)가 원래 스냅샷을 다시 보내서 브로커 retain도 restock 없는 걸로 바뀜 (주문이 비어 있어 무해)
        out = dict(reset_con, seq=f"{con.get('seq')}-reset") if reset_con is not None else con
        payload = json.dumps(out, ensure_ascii=False)
        ok = publish(TOPICS[kind], payload)
        if ok:
            _last_relayed = payload
            _relay_gen += 1
            if isinstance(out, dict):
                _last_key = (out.get("seq"), out.get("updated_at"))
            seq = out.get("seq") if isinstance(out, dict) else "?"
            logger.info(f"[{kind}] seq {seq} → MQTT 전송 완료 ({via})\n{_summary(out)}")
        else:
            logger.error(f"[{kind}] → MQTT 전송 실패 ({via})\n{_summary(out)}")
        logger.debug(f"[{kind}] payload: {payload}")
    if reset_con is not None:
        if not ok:   # reset:last_id는 이미 적혀서 같은 리셋은 다시 안 옴 -> 아두이노 수량 맞추기만 따로 다시 보냄
            logger.warning("[reset] 수량 맞추기 스냅샷 전송 실패 - 5초 뒤 동기화로 다시 보냄")
            threading.Timer(5, _sync_arduinos, args=(_all_keycap_letter_colors(),)).start()
        # 36칸 Mobius 업로드(칸마다 최대 5초)는 알림 응답을 붙잡지 않게 따로 돌림
        threading.Thread(target=_upload_reset_in_background, daemon=True).start()
    return ok


def _upload_reset_in_background():
    failed = _upload_reset_to_mobius()
    logger.info(f"[reset] 하드리셋 모비우스 업로드 실패: {failed if failed else '없음'}")


# ============================================================
# [11/04] cnt_process 주기 조회 - 알림 누락 보충 (보드 main·게이트웨이와 같은 방식)
#  - 노트북 절전·네트워크 끊김 동안 Mobius 알림을 못 받으면 그 주문이 아두이노에 영영 안 감
#    (retain은 마지막으로 "보낸" 스냅샷만 다시 줌). 2026-11-04 19:26~19:42 절전 사례.
#  - PROCESS_POLL_SEC마다 cnt_process/la를 읽어서, 마지막으로 보낸 것과 내용이 다르면 그대로 보냄.
#    같은 스냅샷이 두 번 가도 아두이노는 seq로 걸러냄. 켜자마자 첫 조회에서 최신 스냅샷을 한 번 보냄.
#  - PROCESS_POLL_SEC=0 이면 끔
# ============================================================
PROCESS_POLL_SEC = float(os.getenv("PROCESS_POLL_SEC", "5"))
_last_relayed = None
_last_key = None      # 마지막으로 보낸 스냅샷의 (seq, updated_at) - 옛 스냅샷 거르기용
_relay_gen = 0        # 보낼 때마다 +1 - 주기 조회가 읽는 사이 다른 게 나갔는지 확인용
_relay_lock = threading.Lock()   # _relay 전체(비교·전송·기억)를 한 번에 하나씩


def _get_latest_process():
    url = f"{COSS_BASE_URL.rstrip('/')}/{AE_NAME}/cnt_process/la"
    headers = {"Accept": "application/json", "X-M2M-RI": str(uuid.uuid4()), "X-M2M-RVI": "3",
               "X-M2M-Origin": ORIGIN}
    if COSS_API_KEY:
        headers["X-API-KEY"] = COSS_API_KEY
        headers["X-AUTH-CUSTOM-LECTURE"] = COSS_LECTURE
        headers["X-AUTH-CUSTOM-CREATOR"] = COSS_CREATOR
    resp = requests.get(url, headers=headers, timeout=5)
    resp.raise_for_status()
    con = resp.json()["m2m:cin"]["con"]
    return json.loads(con) if isinstance(con, str) else con


def _process_poll_loop():
    failing = False
    while True:
        time.sleep(PROCESS_POLL_SEC)
        gen = _relay_gen   # 읽기 전 번호 - 읽는 동안 알림이 새 스냅샷을 보내면 이번 결과는 버림
        try:
            con = _get_latest_process()
            if failing:
                logger.info("[poll] cnt_process 주기 조회 복구됨")
                failing = False
        except Exception as e:
            if not failing:   # 끊겨 있는 동안 5초마다 찍히지 않게 처음 한 번만
                logger.warning(f"[poll] cnt_process 주기 조회 실패 - {PROCESS_POLL_SEC:g}초마다 재시도 ({e})")
                failing = True
            continue
        with _relay_lock:
            same = json.dumps(con, ensure_ascii=False) == _last_relayed
        if not same:
            _relay("process", con, "주기 조회 - 알림 누락 보충" if _last_relayed else "시작 시 조회", gen=gen)


if PROCESS_POLL_SEC > 0:
    threading.Thread(target=_process_poll_loop, daemon=True, name="process-poll").start()


# ============================================================
# 아웃바운드: 아두이노 --HTTP POST--> Flask --> Mobius CIN
# ============================================================
@app.route("/report/keycap-status", methods=["POST"])
def report_keycap_status():
    data = request.get_json(force=True, silent=True) or {}

    letter_color = data.get("letter_color")  # 예: "E_r"
    order_id = data.get("order_id")
    count = data.get("count")
    status = data.get("status")
    reason = data.get("reason", "")  # disable 사유: keycap_cartridge_failed / keycap_driver_failed / 플랫폼 fault 문자열 등

    if not letter_color or status is None:
        return jsonify({"error": "letter_color, status는 필수"}), 400

    # 아두이노가 보고한 값 그대로 덮어씀 (재고 원본은 Redis, 아두이노는 캐시)
    if count is not None:
        redis.set(f"stock:{letter_color}", count)
    # status(정상/고장 등)도 같이 저장해둠 — restock/모비우스 리셋 시 이 값을 그대로 씀
    redis.set(f"status:{letter_color}", status)
    # [11/04] 고장 사유도 보관 - 재보충·리셋 뒤 Mobius에 올릴 때 disable과 사유를 그대로 유지 (키캡팀 요청)
    if status == "disable":
        redis.set(f"reason:{letter_color}", reason)
    else:
        redis.delete(f"reason:{letter_color}")

    content = {"order_id": order_id, "count": count, "status": status}
    if status == "disable" and reason:
        content["reason"] = reason
    cnt_path = f"cnt_warehouse/cnt_keycap_status/cnt_{letter_color}"

    ok = post_cin_to_mobius(cnt_path, content)
    logger.info(f"[report/keycap-status] {cnt_path} {'성공' if ok else '실패'}: {content}")

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
    logger.info(f"[report/keycap-dispense] {'성공' if ok else '실패'}: {content}")

    if not ok:
        return jsonify({"error": "mobius CIN 생성 실패"}), 502
    return jsonify({"result": "ok"}), 200


# ============================================================
# 아두이노 부팅 시 재고 초기 조회
# 원래 count만 주는 형태였는데, 아두이노가 재부팅 시 disable 상태까지
# 복원하려면 status도 같이 필요해서 {count, status} 중첩 구조로 응답함.
# 응답 형식: {"E_r": {"count": 8, "status": "idle"}, "E_y": {...}, ...}
# ============================================================
@app.route("/stock/keycap", methods=["GET"])
def get_stock_keycap():
    board = request.args.get("board")
    if board not in BOARD_LETTERS:
        return jsonify({"error": "board는 EI/NS/TF/PJ 중 하나여야 함"}), 400

    letter_colors = [f"{letter}_{color}" for letter in BOARD_LETTERS[board] for color in COLORS]
    stock_keys = [f"stock:{lc}" for lc in letter_colors]
    status_keys = [f"status:{lc}" for lc in letter_colors]

    stock_values = redis.mget(*stock_keys)
    status_values = redis.mget(*status_keys)

    result = {}
    for lc, count, status in zip(letter_colors, stock_values, status_values):
        result[lc] = {
            "count": int(count or 0),
            "status": status if status else "idle",
        }
    return jsonify(result), 200


# ============================================================
# 보드(피지컬 보드 상품) 재고 관리
# 키캡과 달리 알파벳 축 구분 없이 색상으로만 관리함.
# 카트리지 재고(stock)는 BOARD_CARTRIDGE_CAPACITY개, 전체 재고(pool)는
# 색상당 BOARD_POOL_TOTAL개 — 키캡 카트리지 방식과 동일한 구조.
# ============================================================
@app.route("/report/board-status", methods=["POST"])
def report_board_status():
    data = request.get_json(force=True, silent=True) or {}

    color = data.get("color")  # 예: "r" (키캡과 동일한 한 글자 색상 코드)
    order_id = data.get("order_id")
    count = data.get("count")
    status = data.get("status")
    reason = data.get("reason", "")  # disable 사유

    if color not in BOARD_COLORS or status is None:
        return jsonify({"error": f"color({'/'.join(BOARD_COLORS)} 중 하나), status는 필수"}), 400

    # 아두이노/컨베이어 쪽이 보고한 값 그대로 덮어씀 (재고 원본은 Redis)
    if count is not None:
        redis.set(f"stock:board_{color}", count)
    # status(정상/고장 등)도 같이 저장해둠 — restock/모비우스 리셋 시 이 값을 그대로 씀
    redis.set(f"status:board_{color}", status)

    content = {"order_id": order_id, "count": count, "status": status}
    if status == "disable" and reason:
        content["reason"] = reason
    cnt_path = f"cnt_warehouse/cnt_board_status/cnt_{color}"

    ok = post_cin_to_mobius(cnt_path, content)
    logger.info(f"[report/board-status] {cnt_path} {'성공' if ok else '실패'}: {content}")

    if not ok:
        return jsonify({"error": "mobius CIN 생성 실패"}), 502
    return jsonify({"result": "ok"}), 200


# ============================================================
# 아두이노/컨베이어 부팅 시 보드 재고 초기 조회
# 키캡 쪽과 동일하게 count+status 같이 응답함 (재부팅 시 disable 복원용)
# 응답 형식: {"r": {"count": 3, "status": "idle"}, "y": {...}, ...}
# ============================================================
@app.route("/stock/board", methods=["GET"])
def get_stock_board():
    stock_keys = [f"stock:board_{color}" for color in BOARD_COLORS]
    status_keys = [f"status:board_{color}" for color in BOARD_COLORS]

    stock_values = redis.mget(*stock_keys)
    status_values = redis.mget(*status_keys)

    result = {}
    for color, count, status in zip(BOARD_COLORS, stock_values, status_values):
        result[color] = {
            "count": int(count or 0),
            "status": status if status else "idle",
        }
    return jsonify(result), 200


# ============================================================
# 보드 restock: 키캡의 /restock/<letter>/<color> 와 동일한 로직.
# pool에서 카트리지로 채워 넣고, 채운 양을 응답으로 반환
# 응답 형식: {"filled": 3}
# ============================================================
@app.route("/restock/board/<color>", methods=["POST"])
def restock_board(color):
    if color not in BOARD_COLORS:
        abort(404)

    # 리필 시점의 status를 body로 명시적으로 받을 수도 있음 (예: 고장 상태에서도
    # 재고만 채워야 하는 경우). 안 보내주면 "마지막 저장값"이 아니라 방금 채운
    # 개수 기준으로 새로 계산함 (키캡 쪽과 동일한 이유).
    data = request.get_json(force=True, silent=True) or {}
    status_key = f"status:board_{color}"
    status = data.get("status")

    pool_key = f"pool:board_{color}"
    stock_key = f"stock:board_{color}"

    # [11/04] 모자란 만큼만 pool에서 뺌 (_fill_from_pool 참고). filled = 채운 뒤 개수
    filled, moved, pool_left = _fill_from_pool(stock_key, pool_key, BOARD_CARTRIDGE_CAPACITY)

    if status is None:
        status = "empty" if filled == 0 else "idle"
    redis.set(status_key, status)

    # 채운 결과를 order_id=null로 모비우스에 업로드 (여기서 한 번만 올림 -
    # 아두이노 쪽에서 이 호출 이후 또 보고하면 같은 restock이 중복으로
    # 올라가므로 절대 여기 말고 다른 곳에서 또 올리지 않도록 유지할 것)
    content = {"order_id": None, "count": filled, "status": status}
    ok = post_cin_to_mobius(f"cnt_warehouse/cnt_board_status/cnt_{color}", content)

    logger.info(f"[restock] board_{color}: {filled}개 (pool에서 {moved}개, pool 남음 {pool_left}, status={status}, 모비우스 업로드 {'성공' if ok else '실패'})")
    return jsonify({"filled": filled, "moved": moved, "status": status, "mobius_ok": ok}), 200


def _fill_from_pool(stock_key, pool_key, capacity):
    """카트리지를 capacity까지, 모자란 만큼만 pool에서 옮김. (채운 뒤 개수, 옮긴 개수, pool 남은 개수)
    [11/04] 예전엔 남은 게 있어도 카트리지를 capacity로 덮어쓰고 pool에서 capacity만큼 빼서, 가득 찬 칸에
    restock이 오면(11/04 22:14 E_r 사례: 30개인데 restock -> pool만 30 감소) 전체 재고가 그만큼 사라졌음"""
    current = int(redis.get(stock_key) or 0)
    pool_qty = int(redis.get(pool_key) or 0)
    moved = min(max(0, capacity - current), pool_qty)
    redis.set(stock_key, current + moved)
    if moved > 0:
        redis.decrby(pool_key, moved)
    return current + moved, moved, pool_qty - moved


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

    # 리필 시점의 status를 body로 명시적으로 받을 수도 있음 (예: 고장 상태에서도
    # 재고만 채워야 하는 경우). 안 보내주면 "마지막 저장값"이 아니라 방금 채운
    # 개수 기준으로 새로 계산함 - restock은 보통 재고가 empty였을 때 일어나는데,
    # 마지막 저장값을 그대로 쓰면 다 채워놓고도 status가 옛날 empty로 잘못
    # 올라가는 문제가 있었음.
    # [11/04] 하드리셋 직후: 아두이노 RAM 수량 맞추기용 호출 - pool·status·모비우스는 리셋에서 이미 처리했으니
    # 건드리지 않고 지금 stock만 돌려줌 (_auto_hard_reset 참고)
    # [11/09] 수량 맞추기(_sync_arduinos)로 온 호출도 같음 - 칸별 표시는 한 번 쓰면 지움
    if redis.exists("reset:restock_window") or redis.delete(f"sync:{letter_color}"):
        stock = int(redis.get(f"stock:{letter_color}") or 0)
        logger.info(f"[restock] {letter_color}: 하드리셋 직후 수량 맞추기 -> {stock}개 (pool 그대로)")
        return jsonify({"filled": stock, "status": get_last_status(f"status:{letter_color}"), "mobius_ok": True}), 200

    data = request.get_json(force=True, silent=True) or {}
    status_key = f"status:{letter_color}"
    status = data.get("status")

    pool_key = f"pool:{letter_color}"  # 색상별로 분리 (예: pool:E_r), 글자 단위 pool:E 아님

    # [11/04] 모자란 만큼만 pool에서 뺌 (_fill_from_pool 참고). filled = 채운 뒤 개수 - 아두이노가 이 값을 count로 씀
    filled, moved, pool_left = _fill_from_pool(f"stock:{letter_color}", pool_key, RAIL_CAPACITY)

    if status is None:
        # [11/04] 고장(disable)이면 유지 - 펌웨어도 재보충으로는 고장 칸을 안 풂 (키캡팀 확인). 예전엔 idle로 덮어써서
        # 서버·Mobius는 idle인데 아두이노는 그 칸을 건너뛰는 어긋남이 있었음
        if get_last_status(status_key) == "disable":
            status = "disable"
        else:
            status = "empty" if filled == 0 else "idle"
    redis.set(status_key, status)

    # 채운 결과를 order_id=null로 모비우스에 업로드 (여기서 한 번만 올림 -
    # 아두이노 쪽에서 이 호출 이후 또 보고하면 같은 restock이 중복으로
    # 올라가므로 절대 여기 말고 다른 곳에서 또 올리지 않도록 유지할 것)
    content = {"order_id": None, "count": filled, "status": status}
    if status == "disable":
        content["reason"] = redis.get(f"reason:{letter_color}") or ""
    ok = post_cin_to_mobius(f"cnt_warehouse/cnt_keycap_status/cnt_{letter_color}", content)

    logger.info(f"[restock] {letter_color}: {filled}개 (pool에서 {moved}개, pool 남음 {pool_left}, status={status}, 모비우스 업로드 {'성공' if ok else '실패'})")
    return jsonify({"filled": filled, "moved": moved, "status": status, "mobius_ok": ok}), 200


# ============================================================
# 게이트(픽업 대기) 상태: 배출 직후 다음 주문이 pickup_reached(이후) 될 때까지
# 대기하는 상태를 Redis에 영속화. 아두이노 재부팅 시 로컬 RAM(gateOpen/gateOrderId)이
# 유실되므로, 부팅 시 이걸 조회해서 복원함.
# Redis 키: gate:{board}  (값 = 대기 중인 order_id, 키 자체가 없으면 "열림" 상태)
# [11/04] 아두이노는 이 게이트 order_id를 "이미 배출한 주문"으로도 씀 (재부팅 후 중복 적재 방지)
# ============================================================
def _gate_key(board: str) -> str:
    return f"gate:{board}"


@app.route("/gate/<board>", methods=["GET"])
def get_gate_status(board):
    """
    아두이노 부팅 시 게이트 상태 복원용 조회.
    응답 형식: {"locked": true, "order_id": "order_test_8017"} 또는 {"locked": false}
    """
    if board not in BOARD_LETTERS:
        abort(404)

    order_id = redis.get(_gate_key(board))
    if not order_id:
        return jsonify({"locked": False}), 200

    return jsonify({"locked": True, "order_id": order_id}), 200


@app.route("/gate/<board>", methods=["POST"])
def lock_gate(board):
    """
    아두이노가 배출 직후(pickup 대기 시작) 게이트를 잠글 때 호출.
    요청 형식: {"order_id": "order_test_8017"}
    """
    if board not in BOARD_LETTERS:
        abort(404)

    data = request.get_json(force=True, silent=True) or {}
    order_id = data.get("order_id")
    if not order_id:
        return jsonify({"error": "order_id는 필수"}), 400

    redis.set(_gate_key(board), order_id)
    logger.info(f"[gate] {board} 잠금: {order_id}")
    return jsonify({"result": "ok"}), 200


@app.route("/gate/<board>/clear", methods=["POST"])
def clear_gate(board):
    """
    아두이노가 pickup_reached(이후 상태) 확인해서 게이트를 열 때 호출.
    """
    if board not in BOARD_LETTERS:
        abort(404)

    redis.delete(_gate_key(board))
    logger.info(f"[gate] {board} 해제")
    return jsonify({"result": "ok"}), 200


# ============================================================
# 재고 초기화
#
# - 모비우스 리셋 (/reset/mobius): Redis는 건드리지 않고, 현재 Redis에 있는
#   재고값을 "그대로" order_id=null로, status는 마지막으로 보고된 값(고장 상태면
#   고장 상태 그대로) 그대로 모비우스에 재업로드만 함
#   (우리 쪽 값 확인 후 모비우스에 채워 넣는 용도)
#
# - 하드 리셋 (/reset/hard): Redis 자체를 카트리지 기본값(키캡 RAIL_CAPACITY,
#   보드 BOARD_CARTRIDGE_CAPACITY)과 보드 전체 재고(BOARD_POOL_TOTAL)로 되돌리고,
#   진행 중이던 큐/게이트도 전부 비운 뒤, order_id=null / status=null로 모비우스에
#   업로드함. status 캐시(status:{key})는 그대로 유지함 — 재고 초기화가 고장난
#   하드웨어를 고쳐주는 건 아니므로, 실제 마지막 상태 정보를 잃어버리면 안 됨.
#
# [11/04] 키캡 pool:{letter}_{color}도 하드리셋 때 KEYCAP_TOTAL_PER_COLOR - RAIL_CAPACITY로 되돌림
#   (예전엔 키캡 pool은 안 건드려서 redis-cli로 직접 넣어야 했음)
#
# [11/04] 자동 하드리셋: 플랫폼 하드리셋 스냅샷(_is_platform_reset)이 오면 _relay()가 위 하드리셋을
#   한 번 실행하고, 아두이노 4대가 자기 RAM의 수량을 맞추도록
#   하드리셋 = orders가 비었고 keycap_restock·board_restock도 없음 (11/04 22:57:19 seq 177 {"orders": {}}).
#   restock 스냅샷도 주문이 없으면 orders: {} 로 오므로(22:16:18 seq 152 {"orders": {}, "keycap_restock": ["E_r"]})
#   restock 목록이 있으면 하드리셋이 아님. "reset": true 를 실어 보내도 하드리셋
#   그 스냅샷에 keycap_restock(32칸 전부)을 실어 보냄. 아두이노는 기존 재보충 기능으로 /restock/<글자>/<색>을
#   부르는데, 리셋 직후(RESET_RESTOCK_WINDOW_SEC)엔 pool을 빼지 않고 지금 stock만 돌려줌.
#   disable 칸은 수량만 바뀌고 disable 유지 (아두이노 펌웨어가 restock으로 disable을 안 풂).
#   같은 리셋 스냅샷(seq+updated_at)이 다시 와도(알림 재전송·주기 조회·재시작) 두 번 안 함 - Redis reset:last_id
# ============================================================
RESET_RESTOCK_WINDOW_SEC = 120
_reset_lock = threading.Lock()


def _do_hard_reset():
    """Redis 재고·pool·큐·게이트를 전시 시작 상태로. 모비우스 업로드는 _upload_reset_to_mobius()"""
    for letters in BOARD_LETTERS.values():
        for letter in letters:
            for color in COLORS:
                redis.set(f"stock:{letter}_{color}", RAIL_CAPACITY)
                redis.set(f"pool:{letter}_{color}", KEYCAP_TOTAL_PER_COLOR[letter] - RAIL_CAPACITY)
    for color in BOARD_COLORS:
        redis.set(f"stock:board_{color}", BOARD_CARTRIDGE_CAPACITY)
        redis.set(f"pool:board_{color}", BOARD_POOL_TOTAL - BOARD_CARTRIDGE_CAPACITY)
    redis.set("stock:tray", TRAY_TOTAL)   # [11/09] 빈 트레이도 가득으로 (실물도 다 쌓아 두고 누를 것)
    redis.set("status:tray", "idle")
    for board in BOARD_LETTERS:
        redis.delete(f"queue:{board}")
        redis.delete(f"gate:{board}")


def _upload_reset_to_mobius():
    # status 캐시는 지우지 않음 — 재고 초기화가 고장 상태를 고쳐주는 게 아니라서,
    # 지워버리면 다음 restock 때 실제로는 disable인데 idle로 잘못 올라갈 수 있음
    # [11/04] 고장 칸은 status=null 대신 disable + 사유로 올려서 플랫폼에서도 고장으로 남게 함 (키캡팀 요청)
    results = {}
    for lc in _all_keycap_letter_colors():
        content = {"order_id": None, "count": RAIL_CAPACITY, "status": None}
        if get_last_status(f"status:{lc}") == "disable":
            content.update(status="disable", reason=redis.get(f"reason:{lc}") or "")
        results[lc] = post_cin_to_mobius(f"cnt_warehouse/cnt_keycap_status/cnt_{lc}", content)
    for color in BOARD_COLORS:
        content = {"order_id": None, "count": BOARD_CARTRIDGE_CAPACITY, "status": None}
        results[f"board_{color}"] = post_cin_to_mobius(f"cnt_warehouse/cnt_board_status/cnt_{color}", content)
    _upload_tray(None, TRAY_TOTAL)   # 플랫폼에 컨테이너가 생기기 전엔 늘 실패라 실패 목록에는 안 넣음
    return [k for k, ok in results.items() if not ok]


def _is_platform_reset(con):
    """플랫폼 하드리셋 스냅샷인지 - 보드 main check_hard_reset()과 같은 기준"""
    if not isinstance(con, dict):
        return False
    if con.get("reset") is True:
        return True
    return con.get("orders") == {} and not con.get("keycap_restock") and not con.get("board_restock")


def _auto_hard_reset(con):
    """하드리셋 스냅샷이면 한 번만 하드리셋. 했으면 아두이노에 보낼 스냅샷(keycap_restock 32칸 포함)을 반환"""
    if not _is_platform_reset(con):
        return None
    reset_id = f"{con.get('seq')}|{con.get('updated_at')}"
    with _reset_lock:
        if redis.get("reset:last_id") == reset_id:
            return None
        _do_hard_reset()
        redis.set("reset:last_id", reset_id)
        redis.set("reset:restock_window", 1, ex=RESET_RESTOCK_WINDOW_SEC)
    logger.info(f"[reset] 플랫폼 하드리셋 감지 (빈 주문 목록, seq={con.get('seq')}) -> 재고·pool·큐·게이트 초기화")
    return dict(con, keycap_restock=_all_keycap_letter_colors())
# ============================================================
def _all_keycap_letter_colors():
    return [
        f"{letter}_{color}"
        for letters in BOARD_LETTERS.values()
        for letter in letters
        for color in COLORS
    ]


@app.route("/reset/mobius", methods=["POST"])
def reset_mobius():
    keycap_lcs = _all_keycap_letter_colors()
    keycap_keys = [f"stock:{lc}" for lc in keycap_lcs]
    board_keys = [f"stock:board_{color}" for color in BOARD_COLORS]

    keycap_values = redis.mget(*keycap_keys)
    board_values = redis.mget(*board_keys)

    results = {}

    for lc, value in zip(keycap_lcs, keycap_values):
        count = int(value or 0)
        status = get_last_status(f"status:{lc}")
        content = {"order_id": None, "count": count, "status": status}
        ok = post_cin_to_mobius(f"cnt_warehouse/cnt_keycap_status/cnt_{lc}", content)
        results[lc] = ok

    for color, value in zip(BOARD_COLORS, board_values):
        count = int(value or 0)
        status = get_last_status(f"status:board_{color}")
        content = {"order_id": None, "count": count, "status": status}
        ok = post_cin_to_mobius(f"cnt_warehouse/cnt_board_status/cnt_{color}", content)
        results[f"board_{color}"] = ok

    failed = [k for k, ok in results.items() if not ok]
    logger.info(f"[reset/mobius] 동기화 완료, 실패: {failed if failed else '없음'}")
    return jsonify({"result": "ok" if not failed else "partial_fail", "failed": failed}), (200 if not failed else 207)


@app.route("/reset/hard", methods=["POST"])
def reset_hard():
    # 수동 하드리셋 (reset_stock.bat) - 자동(_auto_hard_reset)과 같은 값. 아두이노 RAM 수량도 _sync_arduinos로
    # 맞추고, 보드 main은 쉬는 동안 30초마다 재고를 다시 읽어서 따라감 -> 재부팅 필요 없음
    with _reset_lock:
        _do_hard_reset()
    synced = _sync_arduinos(_all_keycap_letter_colors())
    failed = _upload_reset_to_mobius()
    logger.info(f"[reset/hard] 하드 리셋 완료, 아두이노 동기화 {'전송' if synced else '실패'}, "
          f"모비우스 업로드 실패: {failed if failed else '없음'}")
    return jsonify({"result": "ok" if not failed else "partial_fail", "failed": failed, "arduino_sync": synced}), \
        (200 if not failed else 207)


# ============================================================
# [11/09] 빈 트레이 재고 (Redis stock:tray / status:tray)
#  - 게이트웨이(트레이 팔)가 집기 전에 GET /stock/tray 로 개수를 읽어 더미·층을 정하고, 놓은 뒤
#    POST /report/tray-used 로 하나 뺌 (덮어쓰지 않고 빼기 - 집는 동안 누가 채워도 안 꼬이게)
#  - 채우기·정정은 /stock/set (상태판). 하드리셋 때 TRAY_TOTAL로
#  - Mobius cnt_warehouse/cnt_tray_status 에도 {order_id, count, status} 로 올림. 플랫폼에 컨테이너가 아직
#    없으면 처음 올릴 때 만듦. 실패해도 Redis는 그대로 씀 (키캡·보드는 Mobius 실패면 Redis도 안 바꾸지만 트레이는 우리만 씀)
# ============================================================
_tray_cnt_ready = False


def _ensure_tray_cnt():
    """[11/09] cnt_warehouse 밑에 cnt_tray_status 컨테이너를 우리가 만듦 (플랫폼에 원래 없음).
    201 = 만듦, 409 = 이미 있음 -> 둘 다 준비 끝. 다른 응답이면 다음 업로드 때 다시 시도"""
    global _tray_cnt_ready
    parent, rn = TRAY_STATUS_PATH.rsplit("/", 1)
    headers = {"Accept": "application/json", "X-M2M-RI": str(uuid.uuid4()), "X-M2M-RVI": "3",
               "X-M2M-Origin": ORIGIN, "Content-Type": "application/json;ty=3"}
    if COSS_API_KEY:
        headers.update({"X-API-KEY": COSS_API_KEY, "X-AUTH-CUSTOM-LECTURE": COSS_LECTURE,
                        "X-AUTH-CUSTOM-CREATOR": COSS_CREATOR})
    try:
        resp = requests.post(f"{COSS_BASE_URL.rstrip('/')}/{AE_NAME}/{parent}", headers=headers,
                             json={"m2m:cnt": {"rn": rn}}, timeout=5)
    except Exception as e:
        logger.info(f"[tray] 컨테이너 만들기 요청 오류: {e}")
        return False
    if resp.status_code in (201, 409):
        _tray_cnt_ready = True
        logger.info(f"[tray] {TRAY_STATUS_PATH} 컨테이너 " + ("만듦" if resp.status_code == 201 else "이미 있음"))
        return True
    logger.info(f"[tray] 컨테이너 만들기 실패 ({resp.status_code}): {resp.text}")
    return False


def _upload_tray(order_id, count):
    content = {"order_id": order_id, "count": count, "status": "empty" if count <= 0 else "idle"}
    ok = post_cin_to_mobius(TRAY_STATUS_PATH, content)
    if not ok and not _tray_cnt_ready and _ensure_tray_cnt():   # 컨테이너가 없어서 실패했으면 만들고 한 번 더
        ok = post_cin_to_mobius(TRAY_STATUS_PATH, content)
    if not ok:
        logger.info(f"[tray] 모비우스 업로드 실패 ({TRAY_STATUS_PATH}) - Redis·상태판은 그대로 동작")
    return ok


def _tray_count():
    value = redis.get("stock:tray")
    return TRAY_TOTAL if value is None else int(value)   # 처음 설치 = 다 쌓여 있음


@app.route("/stock/tray", methods=["GET"])
def get_stock_tray():
    return jsonify({"count": _tray_count(), "total": TRAY_TOTAL}), 200


@app.route("/report/tray-used", methods=["POST"])
def report_tray_used():
    """게이트웨이: 빈 트레이를 벨트에 놓았음. body {"order_id", "n": 1} (못 보냈던 것까지 모아서 n개)"""
    data = request.get_json(force=True, silent=True) or {}
    n = data.get("n", 1)
    if not isinstance(n, int) or not 1 <= n <= TRAY_TOTAL:
        return jsonify({"error": f"n은 1~{TRAY_TOTAL}"}), 400
    if redis.get("stock:tray") is None:
        redis.set("stock:tray", TRAY_TOTAL)
    count = max(0, redis.decrby("stock:tray", n))
    redis.set("stock:tray", count)   # 0 밑으로 안 내려가게
    redis.set("status:tray", "empty" if count <= 0 else "idle")
    ok = _upload_tray(data.get("order_id"), count)
    logger.info(f"[tray] {data.get('order_id')} 빈 트레이 {n}개 씀 -> {count}/{TRAY_TOTAL}")
    return jsonify({"result": "ok", "count": count, "mobius_ok": ok}), 200


@app.route("/stock/set", methods=["POST"])
def stock_set():
    """[11/09] 사람이 센 실제 개수로 카트리지 숫자를 맞춤 (상태판에서 칸을 눌러 입력).
    body {"key": "E_r" | "board_r" | "tray", "count": 28}
    - 창고 pool은 그대로 -> 전체 재고(카트리지 + pool)도 차이만큼 같이 줄거나 늚 (기록 30, 실제 28 -> 전체 -2)
    - 키캡: 아두이노 RAM도 같은 수로 맞춤 (_sync_arduinos). 보드: 보드 main이 쉬는 동안 30초 안에 따라감
    - 배출 중(busy)인 칸은 거절 - 끝난 뒤 그 칸의 보고가 고친 숫자를 덮어씀"""
    data = request.get_json(force=True, silent=True) or {}
    key, count = data.get("key"), data.get("count")
    if key in _all_keycap_letter_colors():
        cap, path = RAIL_CAPACITY, f"cnt_warehouse/cnt_keycap_status/cnt_{key}"
    elif key in [f"board_{c}" for c in BOARD_COLORS]:
        cap, path = BOARD_CARTRIDGE_CAPACITY, f"cnt_warehouse/cnt_board_status/cnt_{key[-1]}"
    elif key == "tray":
        cap, path = TRAY_TOTAL, None
    else:
        return jsonify({"error": f"없는 칸: {key}"}), 400
    if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= cap:
        return jsonify({"error": f"개수는 0~{cap}"}), 400

    before = _tray_count() if key == "tray" else int(redis.get(f"stock:{key}") or 0)
    if path is None:
        redis.set("stock:tray", count)
        redis.set("status:tray", "empty" if count <= 0 else "idle")
        ok = _upload_tray(None, count)
        logger.info(f"[stock/set] tray {before} -> {count}개 (사람이 맞춤)")
        return jsonify({"result": "ok", "before": before, "count": count, "mobius_ok": ok}), 200

    status = get_last_status(f"status:{key}")
    if status == "busy":
        return jsonify({"error": f"{key} 배출 중 - 끝난 뒤 다시"}), 409
    if status != "disable":   # 고장 칸은 고장 그대로 (복구는 따로)
        status = "empty" if count <= 0 else "idle"
    content = {"order_id": None, "count": count, "status": status}
    if status == "disable":
        content["reason"] = redis.get(f"reason:{key}") or ""
    ok = post_cin_to_mobius(path, content)
    if not ok:   # 키캡·보드는 플랫폼도 이 숫자로 판단하므로 어긋난 채로 두지 않음
        return jsonify({"error": "mobius CIN 생성 실패 - 안 바꿈"}), 502
    redis.set(f"stock:{key}", count)
    redis.set(f"status:{key}", status)
    synced = None
    if key in _all_keycap_letter_colors():
        synced = _sync_arduinos([key])
    pool = redis.get(f"pool:{key}")
    logger.info(f"[stock/set] {key} {before} -> {count}개 (사람이 맞춤, pool {pool} 그대로)"
                + ("" if synced is None else f", 아두이노 {'전송' if synced else '전송 실패'}"))
    return jsonify({"result": "ok", "before": before, "count": count, "arduino_sync": synced}), 200


@app.route("/sync/keycap", methods=["POST"])
def sync_keycap():
    """아두이노 RAM 수량을 지금 Redis 값으로 맞춤 (재부팅 대신). body {"rails": ["E_r", ...]} 없으면 32칸 전부.
    숫자를 /report/keycap-status 로 고친 뒤 부르면 됨. pool·status·모비우스는 안 건드림"""
    rails = (request.get_json(force=True, silent=True) or {}).get("rails") or _all_keycap_letter_colors()
    bad = [r for r in rails if r not in _all_keycap_letter_colors()]
    if bad:
        return jsonify({"error": f"없는 칸: {bad}"}), 400
    ok = _sync_arduinos(rails)
    return jsonify({"result": "ok" if ok else "mqtt_fail", "rails": rails}), (200 if ok else 502)


REPAIR_TOPIC = "sjuiot/warehouse/command"   # 키캡팀 11/04 펌웨어 - 4대가 같이 구독, 자기 칸만 처리


@app.route("/repair/<rail>", methods=["POST"])
def repair_rail(rail):
    """고장(disable) 칸 원격 복구 - 시리얼 REPAIR와 같음 (펌웨어 11/04~). 상태판 "복구" 버튼이 부름.
    rail = "E_r" 등 한 칸, 또는 "ALL" + ?board=EI (board 없이 ALL이면 4대 전부라 막음).
    retain 없이 보냄 (retain이면 재접속할 때마다 또 복구됨). 칸 상태만 되돌리고 실패 주문 재배출·플랫폼 fault는 그대로.
    복구 결과는 아두이노가 /report/keycap-status 로 보고 -> Redis·Mobius 갱신. 배출 중인 칸은 아두이노가 무시"""
    if rail == "ALL":
        board = request.args.get("board") or (request.get_json(force=True, silent=True) or {}).get("board")
        if board not in BOARD_LETTERS:
            return jsonify({"error": f"ALL은 board({'/'.join(BOARD_LETTERS)})가 필요함"}), 400
        cmd = {"cmd": "REPAIR", "rail": "ALL", "board": board}
    elif rail in _all_keycap_letter_colors():
        cmd = {"cmd": "REPAIR", "rail": rail}
    else:
        return jsonify({"error": f"없는 칸: {rail}"}), 400
    ok = publish(REPAIR_TOPIC, json.dumps(cmd), retain=False)
    logger.info(f"[repair] 복구 명령 {'전송' if ok else '실패'}: {cmd}")
    return jsonify({"result": "sent" if ok else "mqtt_fail", "cmd": cmd}), (200 if ok else 502)


def _sync_arduinos(rails):
    """아두이노 RAM의 칸 수량을 Redis stock으로 맞추게 함 - 펌웨어 수정 없이 기존 restock 기능을 빌림.
    최신 cnt_process에 keycap_restock=rails를 실어 seq만 바꿔(아두이노는 seq가 직전과 같으면 무시) MQTT로 보내면
    아두이노가 /restock/<글자>/<색>을 부르고, RESET_RESTOCK_WINDOW_SEC 동안은 pool을 안 빼고 지금 stock만 돌려줌.
    플랫폼 주문 목록은 최신 그대로라 주문 처리는 바뀌지 않음 (이미 처리한 주문은 아두이노가 걸러냄)"""
    global _last_relayed, _last_key, _relay_gen
    try:
        latest = _get_latest_process()
    except Exception as e:
        logger.warning(f"[sync] 최신 cnt_process 못 읽음 - 빈 주문 목록으로 보냄 ({e})")
        latest = {"orders": {}}
    # [11/09] 칸별 표시 - 예전엔 전체 창(reset:restock_window)을 열어서 2분 동안 다른 칸의 진짜 재보충도 pool을 안 뺐음
    for rail in rails:
        redis.set(f"sync:{rail}", 1, ex=RESET_RESTOCK_WINDOW_SEC)
    snap = dict(latest, seq=f"{latest.get('seq')}-sync{int(time.time())}", keycap_restock=list(rails))
    payload = json.dumps(snap, ensure_ascii=False)
    with _relay_lock:   # 다른 스냅샷 전송과 순서가 섞이지 않게
        ok = publish(TOPICS["process"], payload)
        if ok:
            # [11/05] 보낸 그대로 기억 - 다음 주기 조회가 원래 스냅샷으로 브로커 retain을 덮음 (restock 실린 retain이
            # 남아 있으면 나중에 재부팅한 아두이노가 또 restock을 부름). 예전엔 원래 스냅샷을 기억해서 15초 타이머가
            # 실패하면 retain이 그대로 남았음
            _last_relayed = payload
            _last_key = (snap.get("seq"), snap.get("updated_at"))
            _relay_gen += 1
    logger.info(f"[sync] 아두이노 수량 맞추기 {'전송' if ok else '실패'}: {len(rails)}칸 (seq={snap['seq']})")
    # 브로커 retain에 restock 실린 스냅샷이 남으면 나중에 재부팅한 아두이노가 또 restock을 부름 -> 잠시 뒤 원래 스냅샷으로 덮음
    threading.Timer(15, _restore_retained).start()
    return ok


def _restore_retained():
    try:
        _relay("process", _get_latest_process(), "동기화 뒤 retain 정리")
    except Exception as e:
        logger.warning(f"[sync] retain 정리 실패 (다음 주문 때 자연히 바뀜): {e}")


if __name__ == "__main__":
    # 개발용 단독 실행 (실제 운영은 Windows: waitress-serve --listen=0.0.0.0:5002 app:app)
    app.run(host="0.0.0.0", port=5002, debug=False)