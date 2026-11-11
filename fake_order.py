# -*- coding: utf-8 -*-
"""
Mobius 가 꺼져 있을 때 쓰는 임시 주문 보내기 (가짜 Mobius + 주문 메뉴)

하는 일
  - 127.0.0.1:7579 에서 Mobius 흉내: cnt_process/la 를 돌려줌 (게이트웨이는 시작할 때 이걸 읽어야 로봇을 연결함)
  - 메뉴로 주문 스냅샷을 만들어 보내면, 게이트웨이(5003)·보드 main(5001)의 /notify/process 로 알림을 직접 밀어 줌
  - 보고(done/failed)는 원래 보드 Flask(5000)로 나가므로 Mobius 없이도 그대로 동작
  - status 는 사람이 n) 으로 하나씩 넘김 (자동으로 넘기지 않음). 플랫폼이 하던 일을 대신하는 것뿐이라
    실제 플랫폼과 순서가 다를 수 있음

쓰는 법 (PowerShell, 창 3개)
  1) 이 스크립트를 먼저 켬:      cd C:\\warehouse\\warehouse-team; .\\venv\\Scripts\\python.exe fake_order.py
  2) 게이트웨이를 가짜 Mobius 로: cd C:\\warehouse\\warehouse-team\\keycap_tray
                                  $env:MOBIUS_URL="http://127.0.0.1:7579"; ..\\venv\\Scripts\\python.exe local_gateway.py
  3) 보드 main 을 가짜 Mobius 로: cd C:\\warehouse\\warehouse-team\\board
                                  $env:COSS_BASE_URL="http://127.0.0.1:7579"; $env:AE_NAME="Mobius/ae_fake"; $env:ORIGIN="fake"
                                  ..\\venv\\Scripts\\python.exe main_board_robot_arm.py
     (실제 시작 방식은 start_all.ps1 과 같게. 환경변수는 그 창에서만 적용됨 - .env 파일은 안 건드림)

⚠️ orders 가 빈 스냅샷은 플랫폼 "하드리셋" 신호라 재고가 40/20/15 로 초기화됨 → 이 스크립트는 빈 스냅샷을 보내지 않음
⚠️ 진짜 Mobius 로 안 나감 - 플랫폼·다른 팀 장비에는 영향 없음
"""
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

PORT = 7579
NOTIFY_URLS = ["http://127.0.0.1:5003/notify/process",   # 키캡 게이트웨이
               "http://127.0.0.1:5001/notify/process",   # 보드 main
               "http://127.0.0.1:5002/notify/process"]   # 키캡 Flask -> MQTT -> 키캡 아두이노 4대 (카트리지 배출)
KST = timezone(timedelta(hours=9))
BOARD_CODE = {"red": "r", "yellow": "y", "green": "g", "blue": "b"}
AXES = ("EI", "SN", "TF", "JP")
NORMAL_FLOW = [
    "assigned", "keycap_dispensed", "keycap_reached", "keycap_packed", "tray_reached",
    "board_packed", "pickup_reached", "loaded", "arrived", "verified", "received", "completed",
]
FAULTS = ["keycap_pack_failed", "board_pack_failed", "tray_00_belt_jammed", "tray_01_belt_jammed",
          "tray_02_belt_jammed", "stage_timeout"]

_store = {}          # 컨테이너 경로 -> 최신 con (dict)
_lock = threading.Lock()


def container_of(path):
    """/Mobius/<AE>/cnt_process/la  ->  cnt_process (AE 이름은 상관없이 뒤쪽 컨테이너 경로만 씀)"""
    parts = [p for p in path.split("?")[0].split("/") if p]
    if parts and parts[-1] == "la":
        parts = parts[:-1]
    for i, p in enumerate(parts):
        if p.startswith("cnt_"):
            return "/".join(parts[i:])
    return "/".join(parts)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8") if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        with _lock:
            con = _store.get(container_of(self.path))
        if con is None:
            self._send(404, {"error": "no cin"})
        else:
            self._send(200, {"m2m:cin": {"con": json.dumps(con, ensure_ascii=False)}})

    def do_POST(self):   # 상태판 "창고 테스트" 페이지(test_page.py)가 주문을 올리는 곳. 보관하고 알림도 밀어 줌
        con = None
        name = container_of(self.path)
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            con = data["m2m:cin"]["con"]
            con = json.loads(con) if isinstance(con, str) else con
            with _lock:
                _store[name] = con
        except Exception:
            pass
        self._send(201, {})
        if name == "cnt_process" and isinstance(con, dict):
            if not con.get("orders"):
                print("[가짜 Mobius] 빈 스냅샷(하드리셋 신호)은 알림을 안 보냄")
            else:
                notify(con)

    def log_message(self, *a):
        pass


def notify(snap):
    """게이트웨이·보드 main 에 Mobius 구독 알림 모양으로 스냅샷을 밀어 줌"""
    body = {"m2m:sgn": {"nev": {"rep": {"m2m:cin": {"con": json.dumps(snap, ensure_ascii=False)}}}}}
    for url in NOTIFY_URLS:
        try:
            r = requests.post(url, json=body, timeout=3)
            print(f"  -> {url}  {r.status_code}")
        except Exception as e:
            print(f"  -> {url}  실패 (그 프로그램이 안 떠 있나?): {type(e).__name__}")


def start_server():
    try:
        srv = HTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        print(f"!! 127.0.0.1:{PORT} 를 못 열었음 (이미 떠 있나?): {e}")
        sys.exit(1)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[가짜 Mobius] http://127.0.0.1:{PORT} 대기 중")


def ask(label, default=""):
    v = input(f"{label}" + (f" [{default}]" if default else "") + ": ").strip()
    return v or default


class State:
    def __init__(self):
        self.seq = int(datetime.now(KST).strftime("%H%M%S")) * 100   # 매번 커지게
        self.oid = None
        self.order = None

    def snapshot(self):
        return {"seq": self.seq, "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
                "orders": {self.oid: dict(self.order)}}

    def show(self):
        if not self.order:
            print("주문 없음 (1 로 만들기)")
            return
        o = self.order
        print(f"{self.oid}  status={o['status']}  fault={o['fault']}  board={o['board']}  "
              f"keycap={o['keycap']}  colors={o['colors']}  station={o['station_id']}")


def new_order(st):
    oid = ask("order_id", f"order_test_{datetime.now(KST).strftime('%m%d%H%M%S')}")
    board = ask("board (red/yellow/green/blue)", "green").lower()
    board = {v: k for k, v in BOARD_CODE.items()}.get(board, board)
    if board not in BOARD_CODE:
        return print("board 색이 잘못됨")
    keycap = ask("keycap (MBTI 4글자, EI/SN/TF/JP 순서)", "ENTP").upper()
    if len(keycap) != 4 or any(keycap[i] not in AXES[i] for i in range(4)):
        return print("keycap 은 EI/SN/TF/JP 순서 4글자 (예: ESFJ)")
    colors = [c.strip() for c in ask("colors (쉼표 4개, r/y/g/b)", "r,r,r,r").split(",")]
    if len(colors) != 4 or any(c not in "rybg" or len(c) != 1 for c in colors):
        return print("colors 는 r/y/g/b 4개")
    st.oid = oid
    st.order = {"station_id": ask("station_id (1~3)", "1"), "order_seq": int(ask("order_seq", "1")),
                "status": "assigned", "board": board, "keycap": keycap, "colors": colors,
                "fault": None, "fault_section": None}
    print(f"새 주문 {oid} (status=assigned) - 아직 안 보냈음. s 로 보내기")


def need(st):
    if not st.order:
        print("주문이 없음 - 1 로 먼저 만들 것")
        return False
    return True


def next_status(st):
    if not need(st):
        return
    cur = st.order["status"]
    if cur not in NORMAL_FLOW or NORMAL_FLOW.index(cur) + 1 >= len(NORMAL_FLOW):
        return print(f"{cur} 다음 단계 없음 (2 로 직접 고르기)")
    st.order["status"] = NORMAL_FLOW[NORMAL_FLOW.index(cur) + 1]
    print(f"status: {cur} -> {st.order['status']}  (보내려면 s)")


def set_status(st):
    if not need(st):
        return
    for i, s in enumerate(NORMAL_FLOW, 1):
        print(f"  {i:>2}) {s}")
    raw = ask("번호 또는 이름")
    s = NORMAL_FLOW[int(raw) - 1] if raw.isdigit() and 1 <= int(raw) <= len(NORMAL_FLOW) else raw
    if s not in NORMAL_FLOW + ["keycap_mismatched", "board_mismatched"]:
        return print("모르는 status")
    st.order["status"] = s
    print(f"status={s}  (보내려면 s)")


def set_fault(st):
    if not need(st):
        return
    for i, f in enumerate(FAULTS, 1):
        print(f"  {i}) {f}")
    raw = ask("번호 (해제는 빈 Enter)")
    if not raw:
        st.order["fault"] = st.order["fault_section"] = None
        return print("fault 해제  (보내려면 s)")
    st.order["fault"] = FAULTS[int(raw) - 1]
    st.order["fault_section"] = ask("fault_section", "keycap")
    print(f"fault={st.order['fault']}  (보내려면 s)")


def send(st):
    if not need(st):
        return
    st.seq += 1
    snap = st.snapshot()
    with _lock:
        _store["cnt_process"] = snap
    print(f"[보냄] seq={st.seq} status={st.order['status']} fault={st.order['fault']}")
    notify(snap)


MENU = """
 1) 새 주문 만들기      n) 다음 status 로        2) status 직접 고르기
 f) fault 넣기/해제     s) 지금 상태 보내기      x) 현재 주문 보기      q) 종료"""


def main():
    start_server()
    st = State()
    actions = {"1": new_order, "n": next_status, "2": set_status, "f": set_fault, "s": send,
               "x": lambda s: s.show()}
    while True:
        print(MENU)
        try:
            c = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if c == "q":
            break
        fn = actions.get(c)
        if fn:
            try:
                fn(st)
            except (ValueError, IndexError):
                print("입력이 잘못됨")


if __name__ == "__main__":
    main()
