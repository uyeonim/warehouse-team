# -*- coding: utf-8 -*-
"""
창고 상태판 - 읽기 전용 웹 페이지 (http://<노트북 IP>:5080)
iPad·휴대폰·리눅스 PC 브라우저로 같은 공유기 안에서 봄. 노트북이 테이블 밑에 있을 때용.
- 맨 위: 판정 한 문장 (가장 먼저 볼 문제)
- 라인: 주문이 지나가는 순서대로 장비를 놓고 공장 안전 표지색 램프로 표시. 작업 중인 장비엔 고리 + 주문번호
- 아래: 문제·주의 목록, 최근 작업(오늘 로그의 보고·카메라 판정 + 마지막 카메라 사진), 정상 항목은 접어 둠
- check_all.py 점검을 REFRESH_SEC마다 돌림. 버튼 없음, 아무것도 안 바꿈 (조작은 원격 데스크톱으로)
- 전시장엔 인터넷이 없어서 외부 글꼴·라이브러리 안 씀 (기기 기본 한글 글꼴)
실행: start_all 이 같이 띄움 / 또는 venv\\Scripts\\python.exe status_page.py
"""
import ast
import html
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import check_all

PORT = 5080
REFRESH_SEC = 10       # 점검 간격 (한 번 점검에 수 초 걸림 - 끝난 뒤 이만큼 쉬고 다시)
PAGE_REFRESH_SEC = 10  # 브라우저 자동 새로고침
ROOT = Path(__file__).resolve().parent
RECENT_MAX = 12        # "최근 작업"에 보여줄 개수
LOG_TAIL_BYTES = 300_000   # 로그는 끝부분만 읽음 (하루치가 커져도 가볍게)
CAMERA_DIR = ROOT / "board" / "board_camera_capture_log"   # board_config.CAMERA_IMAGE_DIR와 같은 곳

_lock = threading.Lock()
_latest = None         # check_all.run_checks() 결과
_error = None


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
]
RANK = {"ok": 0, "busy": 0, "off": 1, "warn": 2, "bad": 3}
PROGRAM_NAME = {"board_main": "보드 main", "keycap_tray": "게이트웨이"}


def _station_states(facts):
    """장비마다 (램프, 아래 문구, 작업 중 주문). 램프는 프로그램 상태·USB·서버 중 가장 나쁜 것"""
    hb = facts.get("hb")
    com = facts.get("com", {})
    ports = facts.get("ports", {})
    out = []
    for st in STATIONS:
        lamp, note, job_here = "ok", "대기", None
        worst = []   # (램프, 문구)
        if st["program"] is None:   # 키캡 카트리지 = 아두이노 4대. 우리가 아는 건 키캡 Flask와 MQTT 브로커까지
            if not ports.get("5002"):
                worst.append(("bad", "키캡 Flask 꺼짐"))
            elif not facts.get("mqtt"):
                worst.append(("bad", "MQTT 브로커 접속 안 됨"))
            else:
                note = "Flask·브로커 정상"
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
                    job_here = (h.get("job") or "").split(" ")[0]
                    if h["age"] > check_all.BUSY_WARN_SEC:
                        worst.append(("warn", f"작업 {h['age']}초째 - 멈춤 의심"))
                    else:
                        lamp, note = "busy", f"작업 중 {h['age']}초"
            missing = [c for c in st["com"] if not com.get(c)]
            if missing:
                worst.append(("bad", "USB 안 보임"))
        if worst:
            lamp, note = max(worst, key=lambda w: RANK[w[0]])
        out.append({"name": st["name"], "lamp": lamp, "note": note, "job": job_here})
    return out


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


CSS = """
:root {
  --ground:#E8EAE6; --panel:#F6F7F4; --ink:#1C2421; --soft:#56625E; --rule:#C9CFCB;
  --go:#18794E; --caution:#F2B300; --stop:#C8102E; --steel:#6B7A80;
  --go-wash:#D9EDE2; --caution-wash:#FBEBB8; --stop-wash:#F7D9DC;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground:#171B1A; --panel:#1F2523; --ink:#E6ECE9; --soft:#9AA8A3; --rule:#33403C;
    --go:#4CC38A; --caution:#F2B300; --stop:#FF6B6B; --steel:#8A999E;
    --go-wash:#173326; --caution-wash:#3A2F0E; --stop-wash:#3D1A1C;
    color-scheme: dark;
  }
}
* { box-sizing:border-box; }
html, body { margin:0; }
body {
  background:var(--ground); color:var(--ink);
  font-family:"Malgun Gothic","Apple SD Gothic Neo","Noto Sans KR","Noto Sans CJK KR",system-ui,sans-serif;
  font-size:16px; line-height:1.5; -webkit-text-size-adjust:100%;
  padding-left:max(16px, env(safe-area-inset-left, 0px)); padding-right:max(16px, env(safe-area-inset-right, 0px));
  padding-block:20px 32px;
}
.wrap { max-width:1080px; margin:0 auto; display:flex; flex-direction:column; gap:28px; }
.num { font-variant-numeric:tabular-nums; }

/* 판정 - 화면의 주인공 문장 */
.verdict { border-left:10px solid var(--go); padding:4px 0 4px 18px; }
.verdict.warn { border-left-color:var(--caution); }
.verdict.bad { border-left-color:var(--stop); }
.verdict .say { margin:0; font-size:clamp(24px, 4.2vw, 36px); line-height:1.25; font-weight:800; letter-spacing:-0.01em; text-wrap:balance; }
.verdict .sub { margin:6px 0 0; color:var(--soft); font-size:15px; }

/* 라인 - 주문이 지나가는 순서 */
.line { list-style:none; margin:0; padding:20px 8px 14px; display:flex; background:var(--panel); border-radius:6px; }
.st { flex:1 1 0; min-width:0; position:relative; display:flex; flex-direction:column; align-items:center; text-align:center; gap:6px; padding:0 2px; }
.st::before { content:""; position:absolute; top:15px; left:-50%; width:100%; height:4px; background:var(--steel); opacity:.45; z-index:0; }
.st:first-child::before { display:none; }
.lamp { position:relative; z-index:1; width:34px; height:34px; border-radius:50%; background:var(--go); border:3px solid var(--panel); }
.st.warn .lamp { background:var(--caution); }
.st.bad .lamp { background:var(--stop); animation:blink 1.6s ease-in-out infinite; }
.st.off .lamp { background:var(--steel); }
.st.busy .lamp { box-shadow:0 0 0 4px var(--panel), 0 0 0 8px var(--go); }
.st.end .lamp { background:transparent; border:3px dashed var(--steel); }
.st .name { font-weight:700; font-size:15px; line-height:1.3; }
.st .note { font-size:13px; color:var(--soft); line-height:1.3; overflow-wrap:anywhere; }
.st.bad .note { color:var(--stop); font-weight:700; }
.st.warn .note { color:var(--ink); font-weight:700; }
.st .job { font-size:12px; font-weight:700; color:var(--go); }
@keyframes blink { 50% { opacity:.35; } }
@media (prefers-reduced-motion: reduce) { .st.bad .lamp { animation:none; } }

/* 좁은 화면: 위에서 아래로 */
@media (max-width:760px) {
  .line { flex-direction:column; padding:14px 16px; gap:0; }
  .st { flex-direction:row; align-items:center; text-align:left; gap:12px; padding:9px 0; }
  .st::before { top:-50%; left:15px; width:4px; height:100%; }
  .st .text { display:flex; flex-direction:column; min-width:0; }
  .lamp { flex:none; }
}
@media (min-width:761px) { .st .text { display:contents; } }

/* 문제·주의 목록 */
.issues { display:flex; flex-direction:column; gap:2px; }
.issue { display:grid; grid-template-columns:auto 1fr; gap:12px; align-items:baseline; padding:10px 14px; border-radius:4px; }
.issue.bad { background:var(--stop-wash); }
.issue.warn { background:var(--caution-wash); }
.issue .tag { font-size:13px; font-weight:700; white-space:nowrap; }
.issue.bad .tag { color:var(--stop); }
.issue .where { color:var(--soft); font-size:13px; margin-right:6px; }
.issue .msg { min-width:0; overflow-wrap:anywhere; }
h2 { font-size:15px; margin:0 0 8px; color:var(--soft); font-weight:700; }

/* 최근 작업 + 카메라 */
.recent { display:grid; grid-template-columns:minmax(0, 1fr) 280px; gap:20px; align-items:start; }
.events { list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:2px; }
.ev { display:grid; grid-template-columns:auto 7.5em minmax(0, 1fr) auto; gap:12px; align-items:baseline;
      padding:8px 12px; background:var(--panel); border-radius:4px; font-size:14px; }
.ev .t { color:var(--soft); }
.ev .dev { font-weight:700; }
.ev .what { min-width:0; overflow-wrap:anywhere; }
.ev .oid { color:var(--soft); font-size:12px; margin-left:6px; }
.ev .res { font-size:13px; font-weight:700; color:var(--go); white-space:nowrap; }
.ev.fail { background:var(--stop-wash); }
.ev.fail .res { color:var(--stop); }
.ev .res.info { color:var(--soft); }
.cam { margin:0; background:var(--panel); border-radius:6px; padding:10px; }
.cam img { display:block; width:100%; height:auto; border-radius:3px; }
.cam figcaption { font-size:13px; color:var(--soft); margin-top:6px; overflow-wrap:anywhere; }
.cam figcaption b { color:var(--ink); }
.cam.fail figcaption b { color:var(--stop); }
.empty { color:var(--soft); font-size:14px; margin:0; }
@media (max-width:760px) {
  .recent { grid-template-columns:1fr; }
  .ev { grid-template-columns:auto minmax(0, 1fr) auto; }
  .ev .dev { grid-column:2; }
  .ev .what { grid-column:1 / -1; grid-row:2; }
}

details { border-top:1px solid var(--rule); padding-top:10px; }
summary { cursor:pointer; color:var(--soft); font-weight:700; font-size:15px; }
summary:focus-visible { outline:3px solid var(--go); outline-offset:3px; border-radius:2px; }
.oklist { margin:8px 0 0; padding:0; list-style:none; columns:2 360px; column-gap:28px; }
.oklist li { break-inside:avoid; font-size:14px; padding:3px 0; color:var(--soft); }
.oklist .where { color:var(--ink); margin-right:6px; }
footer { color:var(--soft); font-size:13px; border-top:1px solid var(--rule); padding-top:10px; }
"""

JS = """
(function(){
  var d = document.getElementById('okbox'); if (!d) return;
  try { if (sessionStorage.getItem('okOpen') === '1') d.open = true; } catch(e) {}
  d.addEventListener('toggle', function(){ try { sessionStorage.setItem('okOpen', d.open ? '1' : '0'); } catch(e) {} });
})();
"""


def _render_recent():
    e = html.escape
    events = _recent_events()
    rows = []
    for t, dev, what, ok, oid in events:
        res = '<span class="res info">-</span>' if ok is None else \
            f'<span class="res">{"성공" if ok else "실패"}</span>'
        oid_html = f'<span class="oid">{e(oid)}</span>' if oid else ""
        rows.append(f'<li class="ev{"" if ok is not False else " fail"}"><span class="t num">{e(t)}</span>'
                    f'<span class="dev">{e(dev)}</span><span class="what">{e(what)}{oid_html}</span>{res}</li>')
    listing = (f'<ol class="events">{"".join(rows)}</ol>' if rows
               else '<p class="empty">오늘 기록된 작업이 없습니다.</p>')

    img = _latest_camera_image()
    if img:
        cam_ev = next((ev for ev in events if ev[1] == "카메라"), None)
        shot = f"{img.stem[9:11]}:{img.stem[11:13]}:{img.stem[13:15]}"   # YYYYMMDD_HHMMSS_색
        verdict = (("판정 성공" if cam_ev[3] else "판정 실패") + f" - {cam_ev[2]}") if cam_ev else "판정 기록 없음"
        fail = cam_ev is not None and not cam_ev[3]
        cam = (f'<figure class="cam{" fail" if fail else ""}"><img src="/camera/latest.jpg?f={e(img.name)}" '
               f'alt="마지막 카메라 확인 사진 ({e(shot)})" width="640" height="480">'
               f'<figcaption><b>{e(verdict)}</b><br>마지막 카메라 확인 <span class="num">{e(shot)}</span>'
               f' · 초록 네모 안의 보드 색 비율로 판정</figcaption></figure>')
    else:
        cam = '<p class="empty">오늘 찍은 카메라 사진이 없습니다.</p>'
    return (f'<section><h2>최근 작업 (오늘, 최신순)</h2><div class="recent">{listing}{cam}</div></section>')


def _render(result, error):
    e = html.escape
    head = (f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            f'<meta http-equiv="refresh" content="{PAGE_REFRESH_SEC}">'
            f'<title>창고 상태판</title><style>{CSS}</style></head><body><main class="wrap">')
    if not result:
        msg = f"점검 코드 오류: {e(error)}" if error else "첫 점검을 하고 있습니다. 20초쯤 걸립니다."
        return head + f'<section class="verdict warn"><p class="say">{msg}</p></section></main></body></html>'

    bad, warn, ok = _issues(result)
    if bad:
        cls, say = "bad", f"{bad[0][3]}"
        sub = f"문제 {len(bad)}건, 주의 {len(warn)}건. 맨 위 문제부터 해결하면 아래 항목이 같이 풀리는 경우가 많습니다."
    elif warn:
        cls, say = "warn", f"가동 중, 확인할 것 {len(warn)}건"
        sub = warn[0][3]
    else:
        cls, say = "ok", "정상 가동 중"
        sub = "모든 점검 항목이 정상입니다."
    rehearsal = any(w.get("label", "").endswith("[리허설]") for w in result.get("workers", []))
    if rehearsal:
        sub = "리허설 모드: 로봇은 움직이지 않고 보고는 실제로 나갑니다. " + sub

    parts = [head, f'<section class="verdict {cls}" aria-live="polite"><p class="say">{e(say)}</p>'
                   f'<p class="sub">{e(sub)}</p></section>']

    parts.append('<ol class="line" aria-label="창고 라인 - 주문이 지나가는 순서">')
    for s in _station_states(result.get("facts", {})):
        job = f'<span class="job">{e(s["job"])}</span>' if s["job"] else ""
        parts.append(f'<li class="st {s["lamp"]}"><span class="lamp" aria-hidden="true"></span>'
                     f'<span class="text"><span class="name">{e(s["name"])}</span>'
                     f'<span class="note">{e(s["note"])}</span>{job}</span></li>')
    parts.append('<li class="st end"><span class="lamp" aria-hidden="true"></span><span class="text">'
                 '<span class="name">픽업대</span><span class="note">AGV 담당</span></span></li></ol>')

    if bad or warn:
        parts.append('<section><h2>확인할 것</h2><div class="issues">')
        for level, _, where, msg in bad + warn:
            c, tag = ("bad", "문제") if level == "XX" else ("warn", "주의")
            parts.append(f'<div class="issue {c}"><span class="tag">{tag}</span><span class="msg">'
                         f'<span class="where">{e(_section_short(where))}</span>{e(msg)}</span></div>')
        parts.append('</div></section>')

    parts.append(_render_recent())

    parts.append(f'<details id="okbox"><summary>정상 항목 {len(ok)}개</summary><ul class="oklist">')
    for _, _, where, msg in ok:
        parts.append(f'<li><span class="where">{e(_section_short(where))}</span>{e(msg)}</li>')
    parts.append('</ul></details>')

    err = f" 마지막 점검에서 오류가 났습니다: {e(error)}" if error else ""
    parts.append(f'<footer>마지막 점검 <span class="num">{e(result["time"])}</span>, {PAGE_REFRESH_SEC}초마다 새로 고칩니다.'
                 f' 이 화면은 보기 전용입니다. 조작은 원격 데스크톱으로, 위험하면 로봇 전원 스위치를 끕니다.{err}</footer>')
    parts.append(f'</main><script>{JS}</script></body></html>')
    return "".join(parts)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with _lock:
            result, error = _latest, _error
        if self.path in ("/", "/index.html"):
            body, ctype = _render(result, error).encode("utf-8"), "text/html; charset=utf-8"
        elif self.path == "/status.json":
            body = json.dumps({"result": result, "error": error}, ensure_ascii=False).encode("utf-8")
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

    def log_message(self, fmt, *args):
        pass   # 요청마다 찍히는 접속 로그 끔


def main():
    threading.Thread(target=_check_loop, daemon=True).start()
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
