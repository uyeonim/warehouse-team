# -*- coding: utf-8 -*-
"""
cnt_process 통신 테스트 - 플랫폼 대신 cnt_process 스냅샷을 손으로 조립해서 Mobius에 CIN으로 올림.
형식은 "Mobius CNT·CIN 구조 정리"(2026-10-02) 2-1 기준.

경로: 이 스크립트 -> Mobius cnt_process CIN -> (구독) Flask /notify/process -> MQTT -> 메인 등

⚠️ cnt_process는 플랫폼이 쓰는 공용 명령 채널:
  - 키캡 팀 구독도 같은 알림을 받음 -> 테스트 주문에 키캡 쪽 장비가 반응할 수 있음 (미리 공지)
  - 플랫폼이 동시에 자기 스냅샷을 올리면 덮어써짐
  - 메인(main_board_robot_arm.py)이 켜져 있으면 로봇암/컨베이어가 실제로 움직임

비밀값은 .env에서 읽기만 하고 출력하지 않음.
"""

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

COSS_BASE_URL = os.getenv("COSS_BASE_URL")
AE_NAME = os.getenv("AE_NAME")
COSS_API_KEY = os.getenv("COSS_API_KEY")
COSS_LECTURE = os.getenv("COSS_LECTURE")
COSS_CREATOR = os.getenv("COSS_CREATOR")
ORIGIN = os.getenv("ORIGIN")

_missing = [k for k, v in {"COSS_BASE_URL": COSS_BASE_URL, "AE_NAME": AE_NAME, "COSS_API_KEY": COSS_API_KEY,
                           "COSS_LECTURE": COSS_LECTURE, "COSS_CREATOR": COSS_CREATOR, "ORIGIN": ORIGIN}.items()
            if not v]
if _missing:
    raise RuntimeError(f".env에 {', '.join(_missing)}가 비어있음")

CNT_PROCESS_URL = f"{COSS_BASE_URL.rstrip('/')}/{AE_NAME}/cnt_process"
TIMEOUT = 10
KST = timezone(timedelta(hours=9))

BOARD_CODE = {"red": "r", "yellow": "y", "green": "g", "blue": "b"}
CODE_BOARD = {v: k for k, v in BOARD_CODE.items()}
KEYCAP_AXES = ("EI", "SN", "TF", "JP")   # keycap1~4 선반

# 2-2 status 전체
STATUSES = [
    "assigned", "keycap_mismatched", "keycap_dispensed", "keycap_reached", "keycap_packed",
    "tray_reached", "board_mismatched", "board_packed", "pickup_reached",
    "loaded", "arrived", "unclaimed", "verified", "received", "completed",
]
# 'n'(다음 단계)용 정상 흐름
NORMAL_FLOW = [
    "assigned", "keycap_dispensed", "keycap_reached", "keycap_packed", "tray_reached",
    "board_packed", "pickup_reached", "loaded", "arrived", "verified", "received", "completed",
]
# 2-3 fault 전체 - 값은 fault_section 기본값을 만드는 함수 (order -> str)
FAULTS = {
    "keycap_01_belt_jammed":   lambda o: "keycap_conveyor:01",
    "keycap_02_belt_jammed":   lambda o: "keycap_conveyor:02",
    "tray_01_belt_jammed":     lambda o: "tray_conveyor:01",
    "tray_02_belt_jammed":     lambda o: "tray_conveyor:02",
    "keycap_cartridge_failed": lambda o: f"keycap_cartridge:keycap1:{_slot(o, 0)}",
    "keycap_driver_failed":    lambda o: "keycap_cartridge:keycap1",
    "board_cartridge_failed":  lambda o: f"board_cartridge:{BOARD_CODE[o['board']]}",
    "keycap_empty":            lambda o: f"keycap_cartridge:keycap1:{_slot(o, 0)}",
    "board_empty":             lambda o: f"board_cartridge:{BOARD_CODE[o['board']]}",
    "keycap_pack_failed":      lambda o: "keycap_robot_arm:keycap1",
    "board_pack_failed":       lambda o: "board_robot_arm",
    "tray_load_failed":        lambda o: "tray_robot_arm",
    "pickup_failed":           lambda o: "agv",
    "drop_failed":             lambda o: "agv",
    "agv_broken":              lambda o: "agv",
    "stage_timeout":           lambda o: "board_robot_arm",
}


def _slot(order, i):
    """keycap i번째 글자 + colors i번째 -> 'E_r'."""
    return f"{order['keycap'][i]}_{order['colors'][i]}"


def _headers(create=False):
    h = {
        "X-API-KEY": COSS_API_KEY,
        "X-AUTH-CUSTOM-LECTURE": COSS_LECTURE,
        "X-AUTH-CUSTOM-CREATOR": COSS_CREATOR,
        "Accept": "application/json",
        "X-M2M-RI": str(uuid.uuid4()),
        "X-M2M-RVI": "3",
        "X-M2M-Origin": ORIGIN,
    }
    if create:
        h["Content-Type"] = "application/json;ty=4"
    return h


# ============================== Mobius ==============================
def fetch_latest():
    """cnt_process 최신 CIN의 con(dict) 반환. 실패 시 None. con은 이중 인코딩 대비 최대 2회 파싱."""
    try:
        resp = requests.get(f"{CNT_PROCESS_URL}/la", headers=_headers(), timeout=TIMEOUT)
        if resp.status_code != 200:
            print(f"[LA] 최신 CIN 조회 실패 ({resp.status_code})")
            return None
        con = resp.json().get("m2m:cin", {}).get("con")
        for _ in range(2):
            if isinstance(con, str):
                con = json.loads(con)
        return con if isinstance(con, dict) else None
    except Exception as e:
        print(f"[LA] 최신 CIN 조회 에러: {e}")
        return None


def post_snapshot(snapshot):
    body = {"m2m:cin": {"con": json.dumps(snapshot, ensure_ascii=False)}}
    try:
        resp = requests.post(CNT_PROCESS_URL, headers=_headers(create=True), json=body, timeout=TIMEOUT)
        ok = 200 <= resp.status_code < 300
        print(f"[SEND] {'성공' if ok else '실패'} ({resp.status_code})" + ("" if ok else f": {resp.text[:300]}"))
        return ok
    except Exception as e:
        print(f"[SEND] 요청 에러: {e}")
        return False


# ============================== 상태 ==============================
class State:
    def __init__(self, seq):
        self.seq = seq
        self.order_id = None
        self.order = None            # orders[order_id] 내용, None이면 orders 비어있음
        self.board_restock = []      # 색 코드 (r/y/g/b)
        self.keycap_restock = []     # E_r 형식

    def snapshot(self):
        snap = {
            "seq": self.seq,
            "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
            "orders": {self.order_id: dict(self.order)} if self.order else {},
        }
        if self.keycap_restock:
            snap["keycap_restock"] = list(self.keycap_restock)
        if self.board_restock:
            snap["board_restock"] = list(self.board_restock)
        return snap

    def summary(self):
        if self.order:
            o = self.order
            head = (f"{self.order_id} board={o['board']} keycap={o['keycap']} station={o['station_id']} "
                    f"status={o['status']} fault={o['fault']} fault_section={o['fault_section']}")
        else:
            head = "주문 없음 (orders={})"
        return f"[현재] {head} | board_restock={self.board_restock} keycap_restock={self.keycap_restock} | 다음 seq={self.seq}"


def ask(prompt, default=None):
    raw = input(f"{prompt}" + (f" [{default}]" if default is not None else "") + ": ").strip()
    return raw if raw else default


def choose(title, items):
    for i, it in enumerate(items, 1):
        print(f"  {i:>2}) {it}")
    raw = input(f"{title} 번호 (엔터=취소): ").strip()
    if not raw:
        return None
    try:
        idx = int(raw)
        if 1 <= idx <= len(items):
            return items[idx - 1]
    except ValueError:
        pass
    print("잘못된 번호")
    return None


# ============================== 메뉴 동작 ==============================
def new_order(st):
    oid = ask("order_id", f"order_test_{datetime.now(KST).strftime('%m%d%H%M%S')}")
    board = ask("board (red/yellow/green/blue)", "blue").lower()
    board = CODE_BOARD.get(board, board)
    if board not in BOARD_CODE:
        print("board 색이 잘못됨")
        return
    keycap = ask("keycap (MBTI 4글자)", "ESFJ").upper()
    if len(keycap) != 4 or any(keycap[i] not in KEYCAP_AXES[i] for i in range(4)):
        print("keycap은 EI/SN/TF/JP 순서 4글자 (예: ESFJ)")
        return
    colors = [c.strip() for c in ask("colors (쉼표 4개, r/y/g/b)", "r,y,b,g").split(",")]
    if len(colors) != 4 or any(c not in CODE_BOARD for c in colors):
        print("colors는 r/y/g/b 4개")
        return
    station = ask("station_id (1~3)", "1")
    order_seq = int(ask("order_seq", "1"))
    st.order_id = oid
    st.order = {
        "station_id": station, "order_seq": order_seq, "status": "assigned",
        "board": board, "keycap": keycap, "colors": colors,
        "fault": None, "fault_section": None,
    }
    print(f"새 주문 {oid} 생성 (status=assigned) - 아직 전송 안 됨")


def need_order(st):
    if not st.order:
        print("주문이 없음 - 1)로 먼저 만들 것")
        return False
    return True


def set_status(st):
    if not need_order(st):
        return
    s = choose("status", STATUSES)
    if s:
        st.order["status"] = s
        print(f"status={s}")


def next_status(st):
    if not need_order(st):
        return
    cur = st.order["status"]
    if cur not in NORMAL_FLOW:
        print(f"{cur}는 정상 흐름 밖 - 2)로 직접 고를 것")
        return
    i = NORMAL_FLOW.index(cur)
    if i + 1 >= len(NORMAL_FLOW):
        print("이미 completed")
        return
    st.order["status"] = NORMAL_FLOW[i + 1]
    print(f"status: {cur} -> {st.order['status']}")


def set_fault(st):
    if not need_order(st):
        return
    f = choose("fault", list(FAULTS))
    if not f:
        return
    section = ask("fault_section", FAULTS[f](st.order))
    st.order["fault"] = f
    st.order["fault_section"] = section
    print(f"fault={f}, fault_section={section} (status는 그대로 {st.order['status']})")


def clear_fault(st):
    if not need_order(st):
        return
    st.order["fault"] = None
    st.order["fault_section"] = None
    print("fault 해제 (null)")


def set_board_restock(st):
    raw = input("board_restock (예: b,r 또는 blue,red / 엔터=비우기): ").strip()
    if not raw:
        st.board_restock = []
        print("board_restock 비움 (키 생략)")
        return
    codes = []
    for t in (x.strip().lower() for x in raw.split(",")):
        code = BOARD_CODE.get(t, t)
        if code not in CODE_BOARD:
            print(f"'{t}' 잘못됨 - r/y/g/b 또는 red/yellow/green/blue")
            return
        if code not in codes:
            codes.append(code)
    st.board_restock = codes
    print(f"board_restock={codes}")


def set_keycap_restock(st):
    raw = input("keycap_restock (예: E_r,J_g / 엔터=비우기): ").strip()
    if not raw:
        st.keycap_restock = []
        print("keycap_restock 비움 (키 생략)")
        return
    slots = []
    for t in (x.strip() for x in raw.split(",")):
        parts = t.split("_")
        if (len(parts) != 2 or len(parts[0]) != 1 or parts[0].upper() not in "".join(KEYCAP_AXES)
                or parts[1].lower() not in CODE_BOARD):
            print(f"'{t}' 잘못됨 - 글자_색 (예: E_r)")
            return
        slot = f"{parts[0].upper()}_{parts[1].lower()}"
        if slot not in slots:
            slots.append(slot)
    st.keycap_restock = slots
    print(f"keycap_restock={slots}")


def remove_order(st):
    if not st.order:
        print("이미 주문 없음")
        return
    print(f"{st.order_id}를 orders에서 뺌 (완료/폐기 후 사라진 상태) - 1)로 새로 만들 수 있음")
    st.order = None
    st.order_id = None


def preview(st):
    print(json.dumps(st.snapshot(), ensure_ascii=False, indent=2))


def send(st):
    snap = st.snapshot()
    print(json.dumps(snap, ensure_ascii=False, indent=2))
    if input("이 스냅샷을 cnt_process에 올릴까요? (y/n): ").strip().lower() != "y":
        print("취소")
        return
    if post_snapshot(snap):
        st.seq += 1


def load_latest(st):
    con = fetch_latest()
    if not con:
        return
    print(json.dumps(con, ensure_ascii=False, indent=2))
    orders = con.get("orders") or {}
    if len(orders) > 1:
        oid = choose("가져올 주문", list(orders))
        if not oid:
            return
    elif orders:
        oid = next(iter(orders))
    else:
        oid = None
    if input("이 내용으로 현재 상태를 덮어쓸까요? (y/n): ").strip().lower() != "y":
        return
    st.order_id = oid
    st.order = dict(orders[oid]) if oid else None
    if st.order:
        st.order.setdefault("fault", None)
        st.order.setdefault("fault_section", None)
    st.board_restock = list(con.get("board_restock") or [])
    st.keycap_restock = list(con.get("keycap_restock") or [])
    print("불러옴 (seq는 그대로 유지)")


MENU = """
 1) 새 주문          2) status 선택       n) 다음 단계로
 3) fault 걸기        4) fault 해제
 5) board_restock    6) keycap_restock    7) 주문 빼기
 l) 최신 cnt_process 불러오기   p) 미리보기   s) 전송   q) 종료"""

ACTIONS = {
    "1": new_order, "2": set_status, "n": next_status,
    "3": set_fault, "4": clear_fault,
    "5": set_board_restock, "6": set_keycap_restock, "7": remove_order,
    "l": load_latest, "p": preview, "s": send,
}


def main():
    print(f"cnt_process 통신 테스트 - 대상 AE: {AE_NAME}")
    latest = fetch_latest()
    if latest and isinstance(latest.get("seq"), int):
        seq = latest["seq"] + 1
        print(f"최신 cnt_process seq={latest['seq']}, 진행 중 주문={list((latest.get('orders') or {}).keys())}")
    else:
        seq = int(time.time())
        print(f"최신 seq를 못 읽음 - 시각 기반 seq={seq}로 시작")
    st = State(seq)

    while True:
        print("\n" + st.summary() + MENU)
        cmd = input("> ").strip().lower()
        if cmd == "q":
            break
        action = ACTIONS.get(cmd)
        if action is None:
            print("없는 메뉴")
            continue
        try:
            action(st)
        except (ValueError, KeyError) as e:
            print(f"입력 오류: {e}")


if __name__ == "__main__":
    main()
