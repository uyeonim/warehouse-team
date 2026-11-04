# -*- coding: utf-8 -*-
"""
창고 상태판 - 웹 페이지 (http://<노트북 IP>:5080). 2026-11-05 "안돈 보드" 개편
iPad·휴대폰·리눅스 PC 브라우저로 같은 공유기 안에서 봄. 노트북이 테이블 밑에 있을 때용.
- 맨 위: 판정 - 가장 먼저 볼 문제 그 자체(헤드라인) + 영향과 할 일(부제) + "N초 전 갱신"(멈추면 노랑)
- 라인: 장비 램프 9개 + 진행 중 주문 칩 (cnt_process를 5초마다 읽음). 장비마다 진행 중 1개는 큰 칩,
  같은 단계에서 기다리는 주문은 작은 줄("대기 n"), fault 주문은 빨간 칩. 휴대폰은 칩 하나 + "+ 대기 n, 고장 n"
- 재고: 아두이노 보드 4묶음(EI·NS·TF·PJ) x 8칸 숫자 칸 + 보드 스택. 카트리지 / 전체 재고 탭. 고장 칸 복구 버튼
- 아래: 확인할 것, 최근 작업(오늘 로그), 마지막 카메라 사진, 정상 항목(접힘)
- check_all.py 점검을 REFRESH_SEC마다 돌림. 조작은 고장 칸 복구 버튼 하나뿐 (나머지는 원격 데스크톱으로)
- 전시장엔 인터넷이 없어서 외부 글꼴·라이브러리 안 씀 (기기 기본 한글 글꼴)
실행: start_all 이 같이 띄움 / 또는 venv\\Scripts\\python.exe status_page.py
"""
import ast
import html
import json
import re
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import redis as redis_lib
import requests
from dotenv import dotenv_values

import check_all
import test_page

PORT = 5080
REFRESH_SEC = 5        # 점검 간격 (한 번 점검에 수 초 걸림 - 끝난 뒤 이만큼 쉬고 다시)
PAGE_REFRESH_SEC = 5   # 브라우저 자동 새로고침
STALE_SEC = 45         # 마지막 점검이 이보다 오래되면 "갱신 멈춤" (점검 몇 초 + 쉬는 10초보다 넉넉히)
PROCESS_POLL_SEC = 5   # 주문 칩용 cnt_process 조회 간격
STAGE_SLOW_SEC = 60    # 한 단계에 이보다 오래 머물면 칩 노랑 (플랫폼 stage_timeout은 약 2분)
ROOT = Path(__file__).resolve().parent
RECENT_MAX = 12        # "최근 작업"에 보여줄 개수
LOG_TAIL_BYTES = 300_000   # 로그는 끝부분만 읽음 (하루치가 커져도 가볍게)
CAMERA_DIR = ROOT / "board" / "board_camera_capture_log"   # board_config.CAMERA_IMAGE_DIR와 같은 곳
REDIS_URL = "redis://127.0.0.1:6379/0"   # 보드·키캡 Flask와 같은 Redis (.env REDIS_URL)
KEYCAP_FLASK_URL = "http://127.0.0.1:5002"   # 고장 칸 복구 버튼이 부름

# ============================== 카트리지 칸 (키캡 32칸 + 보드 4색) ==============================
# 키캡 Flask가 아두이노 보고(/report/keycap-status)를 받을 때마다 쓰는 Redis status:/stock: 값을 그대로 보여줌
RACK = [("EI", "E", "I"), ("NS", "N", "S"), ("TF", "T", "F"), ("PJ", "P", "J")]   # 아두이노 보드 = 실물 랙 배치
KEYCAP_ROWS = [(board, letter) for board, *letters in RACK for letter in letters]
RAIL_COLORS = [("r", "핑크"), ("y", "옐로우"), ("b", "블루"), ("g", "그린")]
CELL_WORD = {"empty": "비었음", "disable": "고장", "busy": "배출 중"}   # 칸 안 글자는 이 셋만 (평소엔 숫자만)


def _rails():
    """{"E_r": (status, 카트리지 개수, 창고 pool), ..., "board_r": (...)} - 못 읽으면 None"""
    keys = [f"{letter}_{c}" for _, letter in KEYCAP_ROWS for c, _ in RAIL_COLORS] + [f"board_{c}" for c, _ in RAIL_COLORS]
    num = lambda v: None if v is None else int(v)
    try:
        r = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2)
        statuses = r.mget([f"status:{k}" for k in keys])
        counts = r.mget([f"stock:{k}" for k in keys])
        pools = r.mget([f"pool:{k}" for k in keys])
    except Exception:
        return None
    return {k: (s or "idle", num(n), num(p)) for k, s, n, p in zip(keys, statuses, counts, pools)}


def _original_totals():
    """전시 총 재고표 {"E": 190, ..., "board": 330} - 키캡 Flask 코드의 KEYCAP_TOTAL_PER_COLOR / BOARD_POOL_TOTAL을
    import 없이 읽음 (import하면 MQTT 조회 스레드 등이 같이 뜸). 숫자를 두 곳에 적지 않으려고. 못 읽으면 {}"""
    try:
        tree = ast.parse((ROOT / "keycap_flask" / "app.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return {}
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in ("KEYCAP_TOTAL_PER_COLOR", "BOARD_POOL_TOTAL"):
                try:
                    found[node.targets[0].id] = ast.literal_eval(node.value)
                except ValueError:
                    pass
    totals = dict(found.get("KEYCAP_TOTAL_PER_COLOR") or {})
    if "BOARD_POOL_TOTAL" in found:
        totals["board"] = found["BOARD_POOL_TOTAL"]
    return totals


# ============================== 점검 + 주문 조회 (백그라운드) ==============================
_lock = threading.Lock()
_latest = None         # check_all.run_checks() 결과
_error = None
_process = None        # 최신 cnt_process con (dict) - 못 읽으면 None
_stage_seen = {}       # (주문, status) -> 처음 본 시각. 칩의 "N초"와 오래 머묾(노랑) 판정용


def _check_loop():
    global _latest, _error
    while True:
        try:
            result = check_all.run_checks(print_output=False)
            with _lock:
                _latest, _error = result, None
        except Exception as e:   # 점검 코드 자체 오류 - 페이지에 표시하고 계속 돎
            with _lock:
                _error = f"{type(e).__name__}: {e}"
        time.sleep(REFRESH_SEC)


def _process_loop():
    """cnt_process/la를 PROCESS_POLL_SEC마다 읽어 둠 (읽기만). 주소·AE는 board/.env (보드 main과 같은 값)"""
    global _process
    env = dotenv_values(ROOT / "board" / ".env")
    url = f"{(env.get('COSS_BASE_URL') or '').rstrip('/')}/{env.get('AE_NAME')}/cnt_process/la"
    while True:
        try:
            headers = {"Accept": "application/json", "X-M2M-RI": str(uuid.uuid4()), "X-M2M-RVI": "3",
                       "X-M2M-Origin": env.get("ORIGIN") or ""}
            con = requests.get(url, headers=headers, timeout=3).json()["m2m:cin"]["con"]
            con = json.loads(con) if isinstance(con, str) else con
            now = time.time()
            with _lock:
                _process = con if isinstance(con, dict) else None
                live = set()
                for oid, o in ((_process or {}).get("orders") or {}).items():
                    key = (oid, o.get("status"))
                    live.add(key)
                    _stage_seen.setdefault(key, now)
                for key in list(_stage_seen):   # 끝난 단계는 잊음
                    if key not in live:
                        del _stage_seen[key]
        except Exception:
            with _lock:
                _process = None
        time.sleep(PROCESS_POLL_SEC)


# ============================== 라인 (장비별 램프) ==============================
# 주문이 지나가는 순서. program = 그 장비를 움직이는 프로그램(heartbeat 키), com = 필요한 USB(check_all facts["com"])
# job = heartbeat 작업 문구에 이 말이 들어 있으면 "지금 이 장비가 작업 중"
STATIONS = [
    {"name": "트레이 팔", "program": "keycap_tray", "com": ["tray"], "job": ["assigned"]},
    {"name": "트레이 벨트 0", "program": "board_main", "com": ["board"], "job": ["section0"]},
    {"name": "키캡 카트리지", "program": None, "com": [], "job": []},
    {"name": "키캡 벨트", "program": "keycap_tray", "com": ["set1", "set2"], "job": ["keycap_dispensed"]},
    {"name": "키캡 팔", "program": "keycap_tray", "com": ["set1", "set2"], "job": ["keycap_reached"]},
    {"name": "트레이 벨트 1", "program": "board_main", "com": ["board"], "job": ["section1"]},
    {"name": "보드 팔", "program": "board_main", "com": ["board"], "job": ["보드 픽업"]},
    {"name": "트레이 벨트 2", "program": "board_main", "com": ["board"], "job": ["section2"]},
]   # + 픽업대 (AGV 담당, 램프 없음) = 9번째 자리
RANK = {"ok": 0, "busy": 0, "off": 1, "warn": 2, "bad": 3}
PROGRAM_NAME = {"board_main": "보드 main", "keycap_tray": "게이트웨이"}

# 주문 단계 -> 라인 위 자리 (0 트레이 팔 ... 8 픽업대). 여기 없는 단계(loaded·completed 등)는 라인에서 안 보임
STAGE_AT = {"assigned": 2, "keycap_mismatched": 2, "keycap_dispensed": 3, "keycap_reached": 4, "keycap_packed": 5,
            "tray_reached": 6, "board_mismatched": 6, "board_packed": 7, "pickup_reached": 8}
STAGE_WORD = {"assigned": "배출 중", "keycap_mismatched": "키캡 재배출", "keycap_dispensed": "키캡 벨트",
              "keycap_reached": "키캡 놓는 중", "keycap_packed": "벨트 이동", "tray_reached": "보드 놓는 중",
              "board_mismatched": "보드 다시", "board_packed": "벨트 이동", "pickup_reached": "픽업 대기"}
FAULT_WORD = {"stage_timeout": "시간 초과", "keycap_cartridge_failed": "카트리지 실패", "keycap_driver_failed": "서보 드라이버 고장",
              "tray_load_failed": "트레이 투입 실패"}
BOARD_NAME = {"red": "핑크", "yellow": "옐로우", "blue": "블루", "green": "그린",
              "r": "핑크", "y": "옐로우", "b": "블루", "g": "그린"}


def _station_states(facts, rails=None):
    """장비마다 (램프, 아래 문구). 램프는 프로그램 상태·USB·서버 중 가장 나쁜 것"""
    hb = facts.get("hb")
    com = facts.get("com", {})
    ports = facts.get("ports", {})
    out = []
    for st in STATIONS:
        lamp, note = "ok", "대기"
        worst = []   # (램프, 문구)
        if st["program"] is None:   # 키캡 카트리지 = 아두이노 4대. 우리가 아는 건 키캡 Flask와 MQTT 브로커, 칸 상태까지
            if not ports.get("5002"):
                worst.append(("bad", "키캡 Flask 꺼짐"))
            elif not facts.get("mqtt"):
                worst.append(("bad", "MQTT 브로커 접속 안 됨"))
            keycap = [v for k, v in (rails or {}).items() if not k.startswith("board_")]
            broken = sum(1 for v in keycap if v[0] == "disable")
            if broken:   # 그 칸이 든 주문만 막힘 -> 노랑 (나머지 주문은 돎)
                worst.append(("warn", f"고장 {broken}칸"))
            elif any(v[0] == "busy" for v in keycap):
                lamp, note = "busy", "배출 중"
        else:
            if hb is None:
                worst.append(("off", "상태 확인 불가"))
            else:
                h = hb.get(st["program"])
                prog = PROGRAM_NAME[st["program"]]
                if not h:
                    worst.append(("bad", f"{prog} 꺼짐"))
                elif h["state"] == "link_lost":
                    worst.append(("bad", f"연결 끊김 {h['age']}초"))
                elif h["state"] == "waiting_mobius":
                    worst.append(("bad", f"Mobius 대기 {h['age']}초"))
                elif h["state"] == "starting":
                    worst.append(("warn", "시작 중"))
                elif h["state"] == "busy" and any(k in (h.get("job") or "") for k in st["job"]):
                    if h["age"] > check_all.BUSY_WARN_SEC:
                        worst.append(("warn", f"작업 {h['age']}초째 - 멈춤 의심"))
                    else:
                        lamp, note = "busy", f"작업 중 {h['age']}초"
            if [c for c in st["com"] if not com.get(c)]:
                worst.append(("bad", "USB 안 보임"))
        if worst:
            lamp, note = max(worst, key=lambda w: RANK[w[0]])
        out.append({"name": st["name"], "lamp": lamp, "note": note})
    return out


def _lanes(process, rails, hb, seen, now):
    """{자리: {"work": 칩, "fault": [칩], "wait": [칩]}} - 장비마다 한 번에 하나만 처리하므로 같은 단계 주문은 줄 섬.
    진행 중 = 키캡 단계는 4칸 중 하나가 busy인 주문(아두이노 보고), 다른 단계는 heartbeat 작업에 주문번호가
    들어 있는 주문. 둘 다 모르면 order_seq가 가장 앞선 주문. fault 주문은 줄에서 빠져 따로"""
    orders = (process or {}).get("orders") or {}
    jobs = " ".join((h or {}).get("job") or "" for h in (hb or {}).values())
    lanes = {}
    for oid, o in sorted(orders.items(), key=lambda kv: (kv[1].get("order_seq") or 0, kv[0])):
        at = STAGE_AT.get(o.get("status"))
        if at is None:
            continue
        chip = {"id": oid, "o": o, "age": int(now - seen.get((oid, o.get("status")), now))}
        lane = lanes.setdefault(at, {"work": None, "fault": [], "wait": []})
        (lane["fault"] if o.get("fault") else lane["wait"]).append(chip)

    def busy_rail(chip):
        o = chip["o"]
        keys = [f"{L}_{c}" for L, c in zip(o.get("keycap") or "", o.get("colors") or [])]
        return any((rails or {}).get(k, ("",))[0] == "busy" for k in keys)

    for at, lane in lanes.items():
        if not lane["wait"]:
            continue
        pick = next((c for c in lane["wait"] if (at == STAGE_AT["assigned"] and busy_rail(c)) or c["id"] in jobs), None)
        lane["work"] = pick or lane["wait"][0]
        lane["wait"].remove(lane["work"])
    return lanes


# ============================== 최근 작업 (오늘 로그에서) ==============================
# 로봇 프로그램 두 개의 오늘 로그 끝부분에서 "결과"만 뽑음 - 보고(done/failed), 카메라 판정, 도착 실패, 연결 끊김.
# 로그 문구가 바뀌면 여기도 같이 고칠 것: board/main_board_robot_arm.py log(), keycap_tray/warehouse_server.py "→ 보고"
_TS = re.compile(r"^\[(\d\d:\d\d:\d\d)\]")
_BOARD_REPORT = re.compile(r"\] (board-robot-arm|conveyor/tray\(section(\d)\)) (재전송 성공|성공|실패)")
_GW_REPORT = re.compile(r"→ 보고 (\S+): (\{.*\})")


def _tail_lines(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - LOG_TAIL_BYTES))
            return f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _dict_after(line):
    """줄 끝의 {...} (보드 main은 파이썬 dict, 게이트웨이는 JSON) -> dict, 실패하면 {}"""
    i = line.find("{")
    if i < 0:
        return {}
    for parse in (json.loads, ast.literal_eval):
        try:
            v = parse(line[i:])
            return v if isinstance(v, dict) else {}
        except (ValueError, SyntaxError):
            pass
    return {}


def _ok(v):
    return v == "done"


def _recent_events():
    """[(시각, 장비, 내용, 성공여부, 주문)] 최신순. 성공여부 None = 정보"""
    today = time.strftime("%Y-%m-%d")
    ev = []
    for line in _tail_lines(ROOT / "board" / "logs" / f"main_{today}.log"):
        m = _TS.match(line)
        if not m:
            continue
        t = m.group(1)
        r = _BOARD_REPORT.search(line)
        if r:
            what, sec, res = r.groups()
            body = _dict_after(line)
            oid = body.get("order_id")
            if res == "실패":
                ev.append((t, "보드 Flask", f"{what} 보고 실패 - 재시도 중", False, oid))
            elif what == "board-robot-arm":
                ev.append((t, "보드 팔", "보드 놓기 " + ("성공" if _ok(body.get("board")) else "실패"),
                           _ok(body.get("board")), oid))
            else:
                ev.append((t, f"트레이 벨트 {sec}", f"section{sec} 이동", _ok(body.get("status")), oid))
        elif "카메라 확인 결과:" in line:
            reason = line.split("카메라 확인 결과:", 1)[1].strip()
            ev.append((t, "카메라", reason, not ("미감지" in reason or "불일치" in reason), None))
        elif "도달 실패" in line or "도착 실패" in line:
            ev.append((t, "보드 팔", line.split("] ", 2)[-1][:80], False, None))
        elif "[LINK" in line and ("끊김" in line or "재연결 성공" in line):
            ev.append((t, "보드 팔", "USB 연결 끊김" if "끊김" in line else "재연결 성공", "재연결 성공" in line, None))
    t = "--:--:--"
    for line in _tail_lines(ROOT / "keycap_tray" / "logs" / f"gateway_{today}.log"):
        m = _TS.match(line)
        if m:
            t = m.group(1)   # 게이트웨이 보고 줄엔 시각이 없어서 바로 앞 줄 시각을 씀
            continue
        r = _GW_REPORT.search(line)
        if not r:
            continue
        path, body = r.group(1), _dict_after(line)
        oid = body.get("order_id")
        if path.endswith("cnt_tray_robot_arm"):
            ev.append((t, "트레이 팔", "빈 트레이 투입", _ok(body.get("status")), oid))
        elif path.endswith("cnt_keycap_conveyor"):
            vals = [v for k, v in body.items() if k.startswith("keycap")]
            ev.append((t, "키캡 벨트", "키캡 벨트 이동", bool(vals) and all(_ok(v) for v in vals), oid))
        elif path.endswith("cnt_keycap_robot_arm"):
            for k, v in body.items():
                if k.startswith("keycap"):
                    ev.append((t, "키캡 팔", f"{k[6:]}번 키캡 놓기", _ok(v), oid))
    ev.sort(key=lambda e: e[0], reverse=True)   # 같은 날 HH:MM:SS 라 문자열 정렬 = 시간순
    return ev[:RECENT_MAX]


def _latest_camera_image():
    """오늘 찍힌 마지막 카메라 확인 사진 (파일 이름 = YYYYMMDD_HHMMSS_<색>.jpg)"""
    files = sorted(CAMERA_DIR.glob(time.strftime("%Y%m%d") + "_*.jpg"))
    return files[-1] if files else None


# ============================== 판정 / 목록 ==============================
# 원인에 가까운 영역부터 - 네트워크가 끊기면 뒤의 항목이 줄줄이 빨갛게 되므로 맨 앞 원인을 판정으로
SECTION_ORDER = ["설정 파일", "네트워크", "플랫폼 서버", "창고 노트북 프로그램", "로봇 프로그램 상태 (heartbeat)",
                 "로봇 USB (시리얼 -> COM)", "Mobius 구독", "노트북 설정"]

# 판정 헤드라인 = 문제 그 자체, 부제 = 영향 + 할 일. 점검 문구(check_all.report) 패턴 -> (헤드라인, 부제)
# 문구가 바뀌면 여기도 같이. 안 맞으면 점검 문구를 그대로 헤드라인으로 씀
HEADLINES = [
    (re.compile(r"전시장 망 192\.168\.0\.x 필요"), "전시장 Wi-Fi에 연결 안 됨",
     "노트북을 전시장 공유기(192.168.0.x)에 연결하세요. 플랫폼·로봇 점검이 줄줄이 실패합니다."),
    (re.compile(r"Mobius (접속 실패|cnt_process 읽기 실패)"), "플랫폼 서버(Mobius) 접속 안 됨",
     "주문을 받지 못합니다. 노트북 Wi-Fi(sjiot)와 플랫폼 서버 전원을 확인하세요."),
    (re.compile(r"Mosquitto .*(접속 실패|거부)"), "MQTT 브로커 접속 안 됨",
     "키캡 아두이노가 주문을 못 받습니다. 플랫폼 서버를 확인하세요."),
    (re.compile(r"^(.+?): 상태 기록 없음"), r"\1 꺼짐",
     "창고 시작을 다시 누르세요. 이미 떠 있으면 그 탭의 오류를 확인하세요."),
    (re.compile(r"^(.+?)(?: \[리허설\])?: 로봇 연결 끊김"), r"\1 로봇 연결 끊김",
     "USB와 로봇 전원을 확인하세요. 다시 연결되면 자동으로 이어갑니다."),
    (re.compile(r"^(.+?)(?: \[리허설\])?: Mobius 대기 중"), r"\1 Mobius 대기",
     "플랫폼 서버 응답을 기다리는 중입니다. 네트워크를 확인하세요."),
    (re.compile(r"^(.+?)(?: \[리허설\])?: 작업 중 - .*멈춘 것 같음"), r"\1 작업 멈춤 의심",
     "그 프로그램 탭의 로그를 확인하세요. 팔이 관절 한계에 걸렸을 수 있습니다."),
    (re.compile(r"^(.+?) 안 보임 \("), r"\1 USB 안 보임", "USB 케이블과 로봇 전원을 확인하세요."),
    (re.compile(r"^(.+?)\((\d+)\) 꺼져 있음"), r"\1 꺼짐", "창고 시작을 다시 누르세요."),
    (re.compile(r"충전기가 연결 안 된"), "충전기 빠짐", "배터리로 돌고 있습니다. 충전기를 꽂으세요."),
    (re.compile(r"Mobius 5초 조회 실패"), "주문 조회 실패 중", "알림으로만 주문을 받고 있습니다. 네트워크를 확인하세요."),
    (re.compile(r"못 보낸 보고"), "못 보낸 보고 재전송 중", "플랫폼 서버 연결을 확인하세요. 연결되면 자동으로 보냅니다."),
]


def _issues(result):
    order = {t: i for i, t in enumerate(SECTION_ORDER)}
    rows = []
    for s in result["sections"]:
        for it in s["items"]:
            rows.append((it["level"], order.get(s["title"], 99), s["title"], it["msg"]))
    bad = [r for r in rows if r[0] == "XX"]
    warn = [r for r in rows if r[0] == "!!"]
    ok = [r for r in rows if r[0] == "OK"]
    key = lambda r: r[1]
    return sorted(bad, key=key), sorted(warn, key=key), sorted(ok, key=key)


def _section_short(title):
    return title.split(" (")[0]


def _headline(first, total):
    """맨 앞 문제 하나 -> (헤드라인, 부제). 문제가 더 있으면 헤드라인 끝에 '외 n건'"""
    _, _, where, msg = first
    for pattern, head, sub in HEADLINES:
        m = pattern.search(msg)
        if m:
            head = m.expand(head) if "\\" in head else head
            break
    else:
        head, sub = msg, f"{_section_short(where)} 항목을 확인하세요."
    return head + (f" 외 {total - 1}건" if total > 1 else ""), sub


def _order_summary(process):
    if process is None:
        return "주문 정보를 못 읽었습니다 (플랫폼 서버 확인)."
    shown = [o for o in (process.get("orders") or {}).values() if o.get("status") in STAGE_AT]
    return f"주문 {len(shown)}건 진행 중." if shown else "진행 중인 주문이 없습니다."


CSS = """
:root {
  --steel:#D9DEDB; --plate:#EEF1EF; --plate-2:#E2E7E5; --ink:#1F2A2E; --soft:#56625E; --rule:#C3CAC7;
  --track:#8C979A; --level:#CCD4D1;
  --go:#1E7A4F; --caution:#E8A900; --stop:#C21F2E;
  --go-wash:#D5EADF; --caution-wash:#F8E7B0; --stop-wash:#F4D6D9;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --steel:#161C1D; --plate:#1F2729; --plate-2:#263033; --ink:#E4EAE8; --soft:#98A5A8; --rule:#34403F;
    --track:#5F6C6F; --level:#33403F;
    --go:#4CC38A; --caution:#F2B300; --stop:#FF6B6B;
    --go-wash:#163326; --caution-wash:#3A2F0E; --stop-wash:#3D1A1C;
    color-scheme: dark;
  }
}
* { box-sizing:border-box; }
html, body { margin:0; }
body {
  background:var(--steel); color:var(--ink);
  font-family:"Malgun Gothic","Apple SD Gothic Neo","Noto Sans KR","Noto Sans CJK KR",system-ui,sans-serif;
  font-size:16px; line-height:1.5; -webkit-text-size-adjust:100%;
  padding:20px max(16px, env(safe-area-inset-right, 0px)) 36px max(16px, env(safe-area-inset-left, 0px));
}
.wrap { max-width:1120px; margin:0 auto; display:flex; flex-direction:column; gap:22px; }
.num { font-variant-numeric:tabular-nums; }
h2 { font-size:16px; margin:0 0 8px; font-weight:800; }
button:focus-visible, summary:focus-visible { outline:3px solid var(--go); outline-offset:2px; }

/* 판정 - 신호판 머리: 위쪽 굵은 띠 색 = 상태, 헤드라인 = 문제 그 자체 */
.verdict { display:flex; align-items:flex-end; justify-content:space-between; gap:8px 16px; flex-wrap:wrap;
           border-top:8px solid var(--go); padding-top:12px; }
.verdict.warn { border-top-color:var(--caution); }
.verdict.bad { border-top-color:var(--stop); }
.verdict h1 { margin:0; font-size:clamp(28px, 5vw, 44px); line-height:1.15; font-weight:800; letter-spacing:-0.02em; text-wrap:balance; }
.verdict p { margin:4px 0 0; color:var(--soft); font-size:15px; max-width:70ch; }
.clock { color:var(--soft); font-size:14px; white-space:nowrap; }
.clock.stale { color:var(--ink); font-weight:800; background:var(--caution-wash); border-radius:4px; padding:2px 8px; }

/* 라인 - 주인공 */
.line { background:var(--plate); border-radius:10px; padding:22px 10px 14px; }
.stations { list-style:none; margin:0; padding:0; display:grid; grid-template-columns:repeat(9, minmax(0, 1fr)); position:relative; }
.stations::before { content:""; position:absolute; left:calc(100% / 18); right:calc(100% / 18); top:17px; height:6px;
                    background:repeating-linear-gradient(90deg, var(--track) 0 14px, transparent 14px 20px); border-radius:3px; opacity:.6; }
.st { display:flex; flex-direction:column; align-items:center; text-align:center; gap:6px; position:relative; z-index:1; min-width:0; padding:0 2px; }
.lamp { width:40px; height:40px; border-radius:50%; background:var(--go); border:4px solid var(--plate); flex:none; }
.st.warn .lamp { background:var(--caution); }
.st.bad .lamp { background:var(--stop); animation:blink 1.6s ease-in-out infinite; }
.st.off .lamp { background:var(--track); }
.st.busy .lamp { box-shadow:0 0 0 3px var(--go); }
.st.end .lamp { background:var(--plate); border:3px dashed var(--track); }
.st .name { font-weight:700; font-size:14px; line-height:1.25; }
.st .note { font-size:12px; color:var(--soft); line-height:1.3; overflow-wrap:anywhere; }
.st.warn .note { color:var(--ink); font-weight:700; }
.st.bad .note { color:var(--stop); font-weight:700; }
@keyframes blink { 50% { opacity:.35; } }

/* 주문 칩 - 장비 아래, 장비 한 칸 폭. 위치·높이는 스크립트가 실제 칩 높이로 쌓음 */
.orders { position:relative; height:84px; margin-top:12px; }
.orders::before { content:""; position:absolute; left:0; right:0; top:0; height:1px; background:var(--rule); }
.orders .none { position:absolute; left:0; right:0; top:28px; text-align:center; font-size:13px; color:var(--soft); }
.chip { position:absolute; top:14px; left:calc((var(--at) + .5) / 9 * 100%); transform:translateX(-50%);
        width:calc(100% / 9 - 8px); background:var(--steel); border-left:6px solid var(--go); border-radius:6px;
        padding:6px 9px 7px; font-size:12px; line-height:1.35; }
.orders.live .chip { transition:left .7s cubic-bezier(.3,.7,.2,1), top .4s; }
.chip::before { content:""; position:absolute; left:50%; top:-14px; width:2px; height:14px; background:var(--track); }
.chip b { display:block; font-size:14px; overflow-wrap:anywhere; }
.chip span { display:block; color:var(--soft); }
.chip.slow { border-left-color:var(--caution); }
.chip.fault { border-left-color:var(--stop); background:var(--stop-wash); }
.chip.fault span { color:var(--stop); font-weight:700; }
.chip.wait { padding:3px 8px; font-size:11px; white-space:nowrap; width:auto; min-width:calc(100% / 9 - 8px);
             border-left-width:4px; background:var(--plate-2); color:var(--soft); }
.chip.wait::before { display:none; }
.chip.wait b { display:inline; font-size:11px; color:var(--ink); margin-right:6px; }
.chip .more { display:none; }
@media (prefers-reduced-motion: reduce) { .orders.live .chip { transition:none; } .st.bad .lamp { animation:none; } }

/* 재고 - 아두이노 보드 4묶음 x 8칸 (실물 랙). 칸 = 큰 숫자 + 남은 양만큼 차오르는 배경 */
.stock-head { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:8px; }
.stock-head h2 { margin:0; }
.tabs { display:inline-flex; background:var(--plate-2); border-radius:6px; padding:3px; gap:2px; }
.tabs button { font:inherit; font-size:13px; font-weight:700; color:var(--soft); background:transparent; border:0;
               border-radius:4px; padding:6px 12px; min-height:32px; cursor:pointer; }
.tabs button[aria-selected="true"] { background:var(--plate); color:var(--ink); }
.view[hidden] { display:none; }
.rack { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:10px; }
.bd { background:var(--plate); border-radius:8px; padding:10px; min-width:0; }
.bd.has-bad { box-shadow:inset 0 0 0 2px var(--stop); }
.bd-head { display:flex; justify-content:space-between; align-items:baseline; gap:6px; margin-bottom:6px; }
.bd-head b { font-size:14px; }
.bd-head span { font-size:12px; color:var(--soft); }
.bd.has-bad .bd-head span { color:var(--stop); font-weight:700; }
.bd.has-busy .bd-head span { color:var(--go); font-weight:700; }
.cols, .row { display:grid; grid-template-columns:1.1em repeat(4, minmax(0, 1fr)); gap:4px; align-items:center; }
.cols { margin-bottom:3px; }
.dot { justify-self:center; width:10px; height:10px; border-radius:3px; display:inline-block; }
.dot.r { background:#E58AAE; } .dot.y { background:#E9CC52; } .dot.b { background:#5E93D6; } .dot.g { background:#5DB07E; }
.row + .row { margin-top:4px; }
.row > i { font-style:normal; font-weight:800; font-size:15px; }
.cell { position:relative; height:46px; border-radius:6px; background:var(--plate-2); overflow:hidden;
        display:flex; flex-direction:column; align-items:center; justify-content:center; }
.cell .lvl { position:absolute; left:0; right:0; bottom:0; background:var(--level); }
.cell b { position:relative; font-size:17px; line-height:1.05; }
.cell small { position:relative; font-size:11px; font-weight:700; line-height:1.2; }
.cell.empty { box-shadow:inset 0 0 0 2px var(--stop); }
.cell.empty b, .cell.empty small { color:var(--stop); }
.cell.disable { background:var(--stop-wash); box-shadow:inset 0 0 0 2px var(--stop); }
.cell.disable .lvl { background:var(--stop); opacity:.2; }
.cell.disable b, .cell.disable small { color:var(--stop); }
.cell.busy { background:var(--go-wash); box-shadow:inset 0 0 0 2px var(--go); }
.cell.busy .lvl { background:var(--go); opacity:.25; }
.cell.busy small { color:var(--go); }
@media (prefers-reduced-motion: no-preference) {
  .cell.busy { animation:pour 1.4s ease-in-out infinite; }
  @keyframes pour { 50% { box-shadow:inset 0 0 0 4px var(--go); } }
}
.bd-alert { margin-top:8px; display:flex; align-items:center; justify-content:space-between; gap:6px;
            font-size:12px; color:var(--stop); font-weight:700; border-top:1px solid var(--rule); padding-top:6px; }
.fix { font:inherit; font-size:12px; font-weight:700; color:#fff; background:var(--stop); border:0; border-radius:4px;
       padding:4px 10px; min-height:28px; cursor:pointer; }
.fix:disabled { opacity:.5; cursor:default; }
.boardcard { margin-top:10px; }
.boardcard .bcols, .boardcard .brow { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:6px; }
.boardcard .bcols { margin-bottom:4px; }
.cname { display:flex; align-items:center; justify-content:center; gap:5px; font-size:12px; font-weight:700; color:var(--soft); }
.boardcard .cell { height:50px; }
.boardcard .cell b { font-size:20px; }
.legend { font-size:12px; color:var(--soft); margin:8px 0 0; }

/* 아래 - 확인할 것·최근 작업 | 카메라 */
.lower { display:grid; grid-template-columns:minmax(0, 1.35fr) minmax(0, 1fr); gap:22px; align-items:start; }
.col { display:flex; flex-direction:column; gap:22px; min-width:0; }
.issues { display:flex; flex-direction:column; gap:3px; }
.issue { padding:10px 14px; border-radius:6px; background:var(--caution-wash); font-size:15px; overflow-wrap:anywhere; }
.issue.bad { background:var(--stop-wash); }
.issue em { font-style:normal; font-weight:800; margin-right:8px; }
.issue.bad em { color:var(--stop); }
.issue .where { color:var(--soft); font-size:13px; margin-right:6px; }
.events { list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:2px; }
.ev { display:grid; grid-template-columns:auto 6.5em minmax(0, 1fr) auto; gap:10px; align-items:baseline;
      background:var(--plate); border-radius:5px; padding:7px 10px; font-size:14px; }
.ev .t { color:var(--soft); }
.ev .dev { font-weight:700; }
.ev .what { min-width:0; overflow-wrap:anywhere; }
.ev .oid { color:var(--soft); font-size:12px; margin-left:6px; }
.ev .res { color:var(--go); font-weight:700; font-size:13px; white-space:nowrap; }
.ev.fail { background:var(--stop-wash); }
.ev.fail .res { color:var(--stop); }
.ev .res.info { color:var(--soft); }
.cam { margin:0; background:var(--plate); border-radius:10px; padding:10px; }
.cam img { display:block; width:100%; height:auto; border-radius:6px; }
.cam figcaption { font-size:13px; color:var(--soft); margin-top:8px; overflow-wrap:anywhere; }
.cam figcaption b { color:var(--go); }
.cam.fail figcaption b { color:var(--stop); }
.empty { color:var(--soft); font-size:14px; margin:0; }
details { border-top:1px solid var(--rule); padding-top:10px; }
summary { cursor:pointer; color:var(--soft); font-weight:700; font-size:15px; }
.oklist { margin:8px 0 0; padding:0; list-style:none; columns:2 360px; column-gap:28px; }
.oklist li { break-inside:avoid; font-size:14px; padding:3px 0; color:var(--soft); }
.oklist .where { color:var(--ink); margin-right:6px; }
footer { color:var(--soft); font-size:13px; border-top:1px solid var(--rule); padding-top:10px; }

@media (max-width:1000px) { .rack { grid-template-columns:repeat(2, minmax(0, 1fr)); } }
/* 휴대폰: 라인을 세로로, 장비마다 칩 하나 */
@media (max-width:760px) {
  .line { display:grid; grid-template-columns:auto minmax(0, 1fr); gap:0 12px; padding:16px 14px; }
  .stations { grid-template-columns:none; grid-template-rows:repeat(9, 58px); }
  .stations::before { left:17px; right:auto; top:29px; bottom:29px; width:6px; height:auto;
                      background:repeating-linear-gradient(180deg, var(--track) 0 14px, transparent 14px 20px); }
  .st { flex-direction:row; text-align:left; gap:10px; padding:0; }
  .st .text { display:flex; flex-direction:column; }
  .orders { height:auto !important; margin:0; }
  .orders::before { left:0; top:0; bottom:0; right:auto; width:1px; height:auto; }
  .orders .none { top:8px; left:12px; text-align:left; }
  .chip { left:12px; top:calc(var(--at) * 58px + 2px) !important; transform:none; width:auto; max-width:100%; padding:4px 9px; }
  .chip span { display:inline; margin-right:6px; }
  .chip::before { left:-12px; top:50%; width:12px; height:2px; }
  .chip.wait, .chip.later { display:none; }
  .chip .more { display:inline; font-weight:800; color:var(--ink); }
  .lower { grid-template-columns:1fr; }
  .ev { grid-template-columns:auto minmax(0, 1fr) auto; }
  .ev .what { grid-column:1 / -1; }
}
@media (min-width:761px) { .st .text { display:contents; } }
@media (max-width:480px) { .cell small { display:none; } .cell { height:40px; } .bd { padding:8px; } }
"""

JS = """
(function(){
  // 갱신 시각: "N초 전 갱신". 마지막 점검이 오래되면(상태판 멈춤) 노랑으로
  var c = document.getElementById('clock'); if (!c) return;
  var ts = parseInt(c.getAttribute('data-ts'), 10), stale = parseInt(c.getAttribute('data-stale'), 10);
  function tick(){
    var s = Math.max(0, Math.round((Date.now() - ts) / 1000));
    var ago = s < 60 ? s + '초 전' : Math.floor(s / 60) + '분 전';
    c.textContent = s > stale ? '갱신 멈춤 - ' + ago : ago + ' 갱신';
    c.classList.toggle('stale', s > stale);
  }
  tick(); setInterval(tick, 1000);
})();
(function(){
  // 주문 칩: 장비마다 진행 중 -> 고장 -> 대기 순서로, 실제 칩 높이로 쌓고 라인 높이를 맞춤
  var box = document.getElementById('orders'); if (!box) return;
  function layout(){
    var wide = matchMedia('(min-width:761px)').matches, tallest = 84, y = {};
    box.querySelectorAll('.chip').forEach(function(el){
      var at = el.getAttribute('data-at'); if (y[at] === undefined) y[at] = 14;
      el.style.top = y[at] + 'px'; y[at] += el.offsetHeight + 6; tallest = Math.max(tallest, y[at]);
    });
    box.style.height = wide ? tallest + 'px' : '';
  }
  layout(); window.addEventListener('load', layout); window.addEventListener('resize', layout);
  // 10초 새로 고침 사이에 단계가 바뀐 칩만 미끄러지게: 지난번 자리에서 시작해 새 자리로
  var before = {}; try { before = JSON.parse(sessionStorage.getItem('chipAt') || '{}') || {}; } catch(e) {}
  var now = {};
  box.querySelectorAll('.chip[id]').forEach(function(el){
    var at = el.getAttribute('data-at'); now[el.id] = at;
    if (before[el.id] !== undefined && before[el.id] !== at) el.style.setProperty('--at', before[el.id]);
  });
  try { sessionStorage.setItem('chipAt', JSON.stringify(now)); } catch(e) {}
  requestAnimationFrame(function(){ requestAnimationFrame(function(){
    box.classList.add('live');
    box.querySelectorAll('.chip[id]').forEach(function(el){ el.style.setProperty('--at', el.getAttribute('data-at')); });
  }); });
})();
(function(){
  // 재고 탭: 카트리지 / 전체 재고. 10초 새로 고침 뒤에도 보던 탭 유지
  var tabs = document.querySelectorAll('.tabs [role="tab"]'); if (!tabs.length) return;
  function show(v){
    tabs.forEach(function(t){ t.setAttribute('aria-selected', t.getAttribute('data-view') === v ? 'true' : 'false'); });
    document.querySelectorAll('.view').forEach(function(p){ p.hidden = p.getAttribute('data-view') !== v; });
    try { sessionStorage.setItem('stockView', v); } catch(e) {}
  }
  tabs.forEach(function(t){ t.addEventListener('click', function(){ show(t.getAttribute('data-view')); }); });
  var saved = null; try { saved = sessionStorage.getItem('stockView'); } catch(e) {}
  if (saved) show(saved);
})();
(function(){
  var d = document.getElementById('okbox'); if (!d) return;
  try { if (sessionStorage.getItem('okOpen') === '1') d.open = true; } catch(e) {}
  d.addEventListener('toggle', function(){ try { sessionStorage.setItem('okOpen', d.open ? '1' : '0'); } catch(e) {} });
})();
(function(){
  // 고장 칸 "복구": 확인 창에서 "확인"을 눌러야 보냄 (잘못 눌러도 바로 안 나가게)
  var sent = {}; try { sent = JSON.parse(sessionStorage.getItem('repairSent') || '{}') || {}; } catch(e) {}
  document.querySelectorAll('.fix').forEach(function(b){
    // 20초 안에 보낸 칸은 새로 고침(10초마다) 뒤에도 "복구 보냄"으로 잠가 둠 - 아직 보고 전이면 또 누르지 않게
    var at = sent[b.getAttribute('data-rail')];
    if (at && Date.now() - at < 20000) { b.disabled = true; b.textContent = '복구 보냄'; }
    b.addEventListener('click', function(){
      var rail = b.getAttribute('data-rail');
      clearTimeout(window.__refresh);   // 확인 창을 보는 동안 새로 고침이 끼어들어 요청을 취소하지 않게
      if (!window.confirm(rail + ' 칸의 고장 표시를 풀까요? (아두이노에 복구 명령)\\n' +
        '실패했던 주문을 다시 배출하지는 않습니다 - 다시 배출하려면 플랫폼에서 fault 해제.')) {
        window.__refresh = setTimeout(function(){ location.reload(); }, 3000);   // 취소하면 평소대로 다시 새로 고침
        return;
      }
      b.disabled = true; b.textContent = '보내는 중';
      fetch('/repair', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rail: rail }) })
        .then(function(r){ return r.json().then(function(j){ return { ok: r.ok, j: j }; }); })
        .then(function(x){
          if (!x.ok) { window.alert('복구 명령 실패: ' + (x.j.error || JSON.stringify(x.j))); b.disabled = false; b.textContent = '복구'; window.__refresh = setTimeout(function(){ location.reload(); }, 10000); return; }
          // 아두이노가 "대기"를 보고하기까지 몇 초 걸림 - 바로 새로 고치면 아직 고장으로 보여서 또 누르게 됨(11/04)
          sent[rail] = Date.now(); try { sessionStorage.setItem('repairSent', JSON.stringify(sent)); } catch(e) {}
          b.textContent = '복구 보냄'; setTimeout(function(){ location.reload(); }, 5000);
        })
        .catch(function(err){ window.alert('복구 명령 실패: ' + err); b.disabled = false; b.textContent = '복구';
                               window.__refresh = setTimeout(function(){ location.reload(); }, 10000); });
    });
  });
})();
"""


def _render_line(facts, rails, process, now):
    e = html.escape
    parts = ['<section class="line" aria-label="창고 라인과 진행 중 주문"><ol class="stations">']
    for s in _station_states(facts, rails):
        parts.append(f'<li class="st {s["lamp"]}"><span class="lamp" aria-hidden="true"></span>'
                     f'<span class="text"><span class="name">{e(s["name"])}</span><span class="note">{e(s["note"])}</span></span></li>')
    parts.append('<li class="st end"><span class="lamp" aria-hidden="true"></span><span class="text">'
                 '<span class="name">픽업대</span><span class="note">AGV 담당</span></span></li></ol>')
    parts.append('<div class="orders" id="orders" aria-live="polite">')
    with _lock:
        seen = dict(_stage_seen)
    lanes = _lanes(process, rails, facts.get("hb"), seen, now)
    if process is None:
        parts.append('<p class="none">주문 정보를 못 읽었습니다 (플랫폼 서버 확인)</p>')
    elif not lanes:
        parts.append('<p class="none">진행 중인 주문 없음</p>')
    for at in sorted(lanes):
        lane = lanes[at]

        def big(chip, kind, extra=""):
            o = chip["o"]
            board = BOARD_NAME.get(o.get("board"), o.get("board") or "")
            if kind == "fault":
                state = FAULT_WORD.get(o.get("fault"), o.get("fault"))
                cls = "chip fault" + (" later" if lane["work"] else "")
            else:
                state = f'{STAGE_WORD.get(o.get("status"), o.get("status"))} {chip["age"]}초'
                cls = "chip" + (" slow" if chip["age"] > STAGE_SLOW_SEC else "")
            return (f'<div class="{cls}" id="c_{e(chip["id"])}" data-at="{at}" style="--at:{at}"><b>{e(chip["id"])}{extra}</b>'
                    f'<span>{e(board)} {e(o.get("keycap") or "")}</span><span>{e(state)}</span></div>')

        if lane["work"]:
            more = [f"대기 {len(lane['wait'])}"] if lane["wait"] else []
            more += [f"고장 {len(lane['fault'])}"] if lane["fault"] else []
            extra = f' <span class="more">+ {e(", ".join(more))}</span>' if more else ""
            parts.append(big(lane["work"], "work", extra))
        for chip in lane["fault"]:
            parts.append(big(chip, "fault"))
        for n, chip in enumerate(lane["wait"], 1):
            parts.append(f'<div class="chip wait" id="c_{e(chip["id"])}" data-at="{at}" style="--at:{at}">'
                         f'<b>{e(chip["id"])}</b>대기 {n}</div>')
    parts.append("</div></section>")
    return "".join(parts)


def _render_stock(rails):
    """재고 - 아두이노 보드 4묶음 x 8칸 + 보드 스택. 카트리지 / 전체 재고 두 장을 탭으로"""
    e = html.escape
    if rails is None:
        return '<section class="stock"><h2>재고</h2><p class="empty">Redis를 못 읽어서 재고를 볼 수 없습니다.</p></section>'
    totals = _original_totals()

    def cell(key, view, cap):
        status, count, pool = rails[key]
        have = count or 0
        value = have if view == "cart" else have + (pool or 0)
        if view == "cart":
            st = status if status in ("disable", "busy") else ("empty" if count is not None and have <= 0 else "")
        else:
            st = "disable" if status == "disable" else ""
        ratio = max(0.0, min(1.0, value / cap)) if cap else 0
        label = "-" if count is None else str(value)
        word = f"<small>{CELL_WORD[st]}</small>" if st else ""
        return (f'<div class="cell {st}" title="{e(key)} {label}/{cap}"><span class="lvl" style="height:{ratio * 100:.0f}%"></span>'
                f'<b class="num">{e(label)}</b>{word}</div>')

    def view_html(view):
        out = ['<div class="rack">']
        for board, *letters in RACK:
            keys = [f"{L}_{c}" for L in letters for c, _ in RAIL_COLORS]
            bad = [k for k in keys if rails[k][0] == "disable"]
            busy = view == "cart" and any(rails[k][0] == "busy" for k in keys)
            head = f"고장 {len(bad)}칸" if bad else ("배출 중" if busy else "정상")
            cls = " has-bad" if bad else (" has-busy" if busy else "")
            out.append(f'<section class="bd{cls}" aria-label="{board} 보드"><div class="bd-head"><b>{board}</b><span>{head}</span></div>'
                       '<div class="cols"><span></span>' +
                       "".join(f'<span class="dot {c}" title="{n}"></span>' for c, n in RAIL_COLORS) + "</div>")
            for L in letters:
                cap = 30 if view == "cart" else totals.get(L, 0)
                out.append(f'<div class="row"><i>{L}</i>' + "".join(cell(f"{L}_{c}", view, cap) for c, _ in RAIL_COLORS) + "</div>")
            for k in bad:
                out.append(f'<div class="bd-alert"><span>{k} 고장</span>'
                           f'<button type="button" class="fix" data-rail="{k}">복구</button></div>')
            out.append("</section>")
        out.append("</div>")
        cap = 20 if view == "cart" else totals.get("board", 0)
        out.append('<section class="bd boardcard" aria-label="보드 스택"><div class="bd-head"><b>보드 스택</b>'
                   f'<span>{"카트리지 20개" if view == "cart" else f"전시 총량 {cap}개"}</span></div><div class="bcols">' +
                   "".join(f'<span class="cname"><span class="dot {c}"></span>{n}</span>' for c, n in RAIL_COLORS) +
                   '</div><div class="brow">' + "".join(cell(f"board_{c}", view, cap) for c, _ in RAIL_COLORS) + "</div></section>")
        legend = ("숫자 = 카트리지에 남은 개수, 배경 = 남은 양 (키캡 30, 보드 20). 초록 = 지금 뽑는 칸, 고장 칸은 고친 뒤 복구."
                  if view == "cart" else
                  "숫자 = 창고까지 합친 남은 개수, 배경 = 전시 총량 대비 (키캡은 글자별, 보드 330). 총량은 키캡 Flask 코드에서 읽음.")
        out.append(f'<p class="legend">{legend}</p>')
        return "".join(out)

    return ('<section class="stock"><div class="stock-head"><h2>재고</h2>'
            '<div class="tabs" role="tablist" aria-label="재고 보기">'
            '<button type="button" role="tab" data-view="cart" aria-selected="true">카트리지</button>'
            '<button type="button" role="tab" data-view="total" aria-selected="false">전체 재고</button></div></div>'
            f'<div class="view" data-view="cart" role="tabpanel">{view_html("cart")}</div>'
            f'<div class="view" data-view="total" role="tabpanel" hidden>{view_html("total")}</div></section>')


def _render_lower(bad, warn):
    e = html.escape
    left = []
    if bad or warn:
        left.append('<section><h2>확인할 것</h2><div class="issues">')
        for level, _, where, msg in bad + warn:
            c, tag = ("bad", "문제") if level == "XX" else ("warn", "주의")
            left.append(f'<div class="issue {c}"><em>{tag}</em><span class="where">{e(_section_short(where))}</span>{e(msg)}</div>')
        left.append("</div></section>")
    events = _recent_events()
    rows = []
    for t, dev, what, ok, oid in events:
        res = '<span class="res info">-</span>' if ok is None else f'<span class="res">{"성공" if ok else "실패"}</span>'
        oid_html = f'<span class="oid">{e(oid)}</span>' if oid else ""
        rows.append(f'<li class="ev{"" if ok is not False else " fail"}"><span class="t num">{e(t)}</span>'
                    f'<span class="dev">{e(dev)}</span><span class="what">{e(what)}{oid_html}</span>{res}</li>')
    left.append('<section><h2>최근 작업</h2>' +
                (f'<ol class="events">{"".join(rows)}</ol>' if rows else '<p class="empty">오늘 기록된 작업이 없습니다.</p>') +
                "</section>")
    img = _latest_camera_image()
    if img:
        cam_ev = next((ev for ev in events if ev[1] == "카메라"), None)
        shot = f"{img.stem[9:11]}:{img.stem[11:13]}:{img.stem[13:15]}"   # YYYYMMDD_HHMMSS_색
        verdict = ("판정 성공" if cam_ev[3] else "판정 실패") if cam_ev else "판정 기록 없음"
        detail = f" {cam_ev[2]}" if cam_ev else ""
        fail = cam_ev is not None and not cam_ev[3]
        cam = (f'<figure class="cam{" fail" if fail else ""}"><img src="/camera/latest.jpg?f={e(img.name)}" '
               f'alt="마지막 카메라 확인 사진 ({e(shot)})" width="640" height="480">'
               f'<figcaption><b>{e(verdict)}</b> <span class="num">{e(shot)}</span>{e(detail)}</figcaption></figure>')
    else:
        cam = '<p class="empty">오늘 찍은 카메라 사진이 없습니다.</p>'
    return f'<div class="lower"><div class="col">{"".join(left)}</div><div class="col">{cam}</div></div>'


def _render(result, error):
    e = html.escape
    head = (f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            f'<noscript><meta http-equiv="refresh" content="{PAGE_REFRESH_SEC}"></noscript>'
            f'<title>창고 상태판</title><style>{CSS}</style></head><body><main class="wrap">'
            # 자동 새로 고침은 스크립트 타이머 - 복구 버튼을 누르면 멈춤. meta refresh는 확인 창이 떠 있는 동안
            # 밀려 있다가 "확인" 직후 페이지를 넘겨서 막 보낸 복구 요청이 취소됐음 (11/04 두 번 눌러야 하던 원인)
            f'<script>window.__refresh = setTimeout(function(){{ location.reload(); }}, {PAGE_REFRESH_SEC * 1000});</script>')
    if not result:
        msg = f"점검 코드 오류: {e(error)}" if error else "첫 점검을 하고 있습니다"
        sub = "" if error else "<p>20초쯤 걸립니다.</p>"
        return head + f'<header class="verdict warn"><div><h1>{msg}</h1>{sub}</div></header></main></body></html>'

    now = time.time()
    with _lock:
        process = _process
    bad, warn, ok = _issues(result)
    rails = _rails()
    broken = [k for k, v in (rails or {}).items() if v[0] == "disable"]
    rail_issue = None
    if broken:   # 점검(check_all) 항목은 아니지만 주문이 멈추는 원인이라 "확인할 것"에 같이 올림
        rail_issue = ("!!", 0, "카트리지 칸", f"{', '.join(broken)} 고장 - 이 칸이 든 주문은 키캡 단계에서 멈춤")
        warn = warn + [rail_issue]
    total = len(bad) + len(warn)
    if bad:
        cls = "bad"
        say, sub = _headline(bad[0], total)
    elif warn:
        cls = "warn"
        first = rail_issue if rail_issue else warn[0]
        if first is rail_issue:
            say = f"{broken[0]} 카트리지 고장" + (f" 외 {total - 1}건" if total > 1 or len(broken) > 1 else "")
            sub = "이 칸이 든 주문은 키캡 단계에서 멈춥니다. 센서를 확인하고 재고에서 복구를 누르세요."
        else:
            say, sub = _headline(first, total)
    else:
        cls, say, sub = "ok", "정상 가동 중", _order_summary(process)
    if any(w.get("label", "").endswith("[리허설]") for w in result.get("workers", [])):
        sub = "리허설 모드: 로봇은 움직이지 않고 보고는 실제로 나갑니다. " + sub

    checked = datetime.strptime(result["time"], "%Y-%m-%d %H:%M:%S").timestamp()
    parts = [head,
             f'<header class="verdict {cls}" aria-live="polite"><div><h1>{e(say)}</h1><p>{e(sub)}</p></div>'
             f'<span class="clock num" id="clock" data-ts="{int(checked * 1000)}" data-stale="{STALE_SEC}">'
             f'{e(result["time"][11:])} 갱신</span></header>',
             _render_line(result.get("facts", {}), rails, process, now),
             _render_stock(rails),
             _render_lower(bad, warn)]

    parts.append(f'<details id="okbox"><summary>정상 항목 {len(ok)}개</summary><ul class="oklist">')
    for _, _, where, msg in ok:
        parts.append(f'<li><span class="where">{e(_section_short(where))}</span>{e(msg)}</li>')
    parts.append('</ul></details>')
    err = f" 마지막 점검에서 오류가 났습니다: {e(error)}" if error else ""
    parts.append(f'<footer>{PAGE_REFRESH_SEC}초마다 새로 고칩니다. 조작은 원격 데스크톱으로, 위험하면 로봇 전원 스위치를 끕니다.'
                 f' 이 화면에서 할 수 있는 조작은 고장 칸 복구뿐입니다.{err}</footer>')
    parts.append(f'</main><script>{JS}</script></body></html>')
    return "".join(parts)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if test_page.handle(self, "GET"):   # /test - 테스트 페이지 (같은 공유기에서만)
            return
        with _lock:
            result, error, process = _latest, _error, _process
        if self.path in ("/", "/index.html"):
            body, ctype = _render(result, error).encode("utf-8"), "text/html; charset=utf-8"
        elif self.path == "/status.json":
            body = json.dumps({"result": result, "error": error, "process": process}, ensure_ascii=False).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        elif self.path.split("?")[0] == "/camera/latest.jpg":
            # 카메라 폴더의 오늘 마지막 사진만 내보냄 (경로를 요청에서 받지 않음)
            img = _latest_camera_image()
            if not img:
                self.send_response(404)
                self.end_headers()
                return
            body, ctype = img.read_bytes(), "image/jpeg"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if test_page.handle(self, "POST"):   # /test/send - 테스트 페이지에서 스냅샷 올리기
            return
        # 상태판 화면에서 유일한 조작: 고장 칸 복구 -> 키캡 Flask /repair/<칸> (아두이노에 MQTT 복구 명령, 칸 상태만 되돌림)
        if self.path != "/repair":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 1024)
            rail = json.loads(self.rfile.read(length) or b"{}").get("rail")
        except (ValueError, AttributeError):
            rail = None
        valid = {f"{letter}_{c}" for _, letter in KEYCAP_ROWS for c, _ in RAIL_COLORS}
        if rail not in valid:
            code, body = 400, {"error": "없는 칸"}
        else:
            try:
                r = requests.post(f"{KEYCAP_FLASK_URL}/repair/{rail}", timeout=8)
                code, body = r.status_code, r.json()
            except Exception as e:
                code, body = 502, {"error": f"키캡 Flask 응답 없음 ({type(e).__name__})"}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass   # 요청마다 찍히는 접속 로그 끔


def main():
    threading.Thread(target=_check_loop, daemon=True).start()
    threading.Thread(target=_process_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"창고 상태판: http://<이 노트북 IP>:{PORT}  (같은 공유기에서 브라우저로)  - Ctrl+C로 종료")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
