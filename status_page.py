# -*- coding: utf-8 -*-
"""
창고 상태판 - 읽기 전용 웹 페이지 (http://<노트북 IP>:5080)
iPad·휴대폰·리눅스 PC 브라우저로 같은 공유기 안에서 봄. 노트북이 테이블 밑에 있을 때용.
- check_all.py의 점검(설정·네트워크·Mobius·구독·서버·heartbeat·로봇 USB·노트북 설정)을 REFRESH_SEC마다 돌려서 보여줌
- 버튼 없음, 아무것도 안 바꿈 (조작은 원격 데스크톱으로)
- 인터넷 없이 동작하게 외부 글꼴·라이브러리 안 씀
실행: start_all 이 같이 띄움 / 또는 venv\\Scripts\\python.exe status_page.py
"""
import html
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import check_all

PORT = 5080
REFRESH_SEC = 10       # 점검 간격 (한 번 점검에 수 초 걸림 - 끝난 뒤 이만큼 쉬고 다시)
PAGE_REFRESH_SEC = 10  # 브라우저 자동 새로고침

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


LEVEL_CLASS = {"OK": "ok", "!!": "warn", "XX": "bad"}
LEVEL_TEXT = {"OK": "정상", "!!": "주의", "XX": "문제"}

CSS = """
:root { --bg:#f3f5f4; --card:#ffffff; --fg:#17201d; --muted:#5d6b66; --line:#d9e0dd;
        --ok:#1f7a4d; --ok-bg:#e3f3ea; --warn:#9a6200; --warn-bg:#fbf0d8; --bad:#b3261e; --bad-bg:#fbe4e2; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#111614; --card:#1a211e; --fg:#e4ebe8; --muted:#97a6a0; --line:#2c3833;
          --ok:#5fd39a; --ok-bg:#163326; --warn:#f0b54c; --warn-bg:#3a2d12; --bad:#ff8a80; --bad-bg:#3d1714; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font-size:15px; line-height:1.5;
       font-family: "Malgun Gothic","Apple SD Gothic Neo","Noto Sans KR",system-ui,sans-serif;
       padding: 16px max(16px, env(safe-area-inset-left)); }
.wrap { max-width: 980px; margin: 0 auto; display:flex; flex-direction:column; gap:16px; }
header { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:8px; }
h1 { font-size:20px; margin:0; }
.time { color:var(--muted); font-size:13px; font-variant-numeric: tabular-nums; }
.overall { border-radius:10px; padding:14px 16px; font-size:17px; font-weight:700; display:flex; gap:12px; flex-wrap:wrap; }
.overall.ok { background:var(--ok-bg); color:var(--ok); }
.overall.warn { background:var(--warn-bg); color:var(--warn); }
.overall.bad { background:var(--bad-bg); color:var(--bad); }
.workers { display:grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap:12px; }
.worker { background:var(--card); border:1px solid var(--line); border-left:6px solid var(--line); border-radius:10px;
          padding:12px 14px; min-width:0; }
.worker.ok { border-left-color:var(--ok); } .worker.warn { border-left-color:var(--warn); } .worker.bad { border-left-color:var(--bad); }
.worker .name { color:var(--muted); font-size:13px; }
.worker .state { font-size:22px; font-weight:700; margin:2px 0; }
.worker.ok .state { color:var(--ok); } .worker.warn .state { color:var(--warn); } .worker.bad .state { color:var(--bad); }
.worker .detail { font-size:13px; color:var(--muted); overflow-wrap:anywhere; }
.grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap:12px; }
.sec { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; min-width:0; }
.sec h2 { font-size:14px; margin:0 0 8px; color:var(--muted); font-weight:600; }
.item { display:flex; gap:8px; align-items:flex-start; padding:4px 0; border-top:1px solid var(--line); font-size:13px; }
.item:first-of-type { border-top:0; }
.badge { flex:none; font-size:11px; font-weight:700; padding:1px 7px; border-radius:999px; margin-top:2px; }
.badge.ok { background:var(--ok-bg); color:var(--ok); } .badge.warn { background:var(--warn-bg); color:var(--warn); }
.badge.bad { background:var(--bad-bg); color:var(--bad); }
.msg { overflow-wrap:anywhere; min-width:0; }
footer { color:var(--muted); font-size:12px; }
"""


def _render(result, error):
    e = html.escape
    parts = [f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
             f'<meta name="viewport" content="width=device-width, initial-scale=1">'
             f'<meta http-equiv="refresh" content="{PAGE_REFRESH_SEC}">'
             f'<title>창고 상태판</title><style>{CSS}</style></head><body><div class="wrap">']
    when = result["time"] if result else "아직 점검 전"
    parts.append(f'<header><h1>창고 상태판</h1><span class="time">마지막 점검 {e(when)} · {PAGE_REFRESH_SEC}초마다 새로고침</span></header>')
    if error:
        parts.append(f'<div class="overall bad">점검 오류: {e(error)}</div>')
    if not result:
        parts.append('<div class="overall warn">첫 점검 중... (10~20초)</div></div></body></html>')
        return "".join(parts)

    c = result["counts"]
    overall = "bad" if c["XX"] else "warn" if c["!!"] else "ok"
    headline = {"ok": "모두 정상", "warn": "주의 항목 있음", "bad": "문제 있음 - 빨간 항목부터 확인"}[overall]
    parts.append(f'<div class="overall {overall}"><span>{headline}</span>'
                 f'<span>문제 {c["XX"]} · 주의 {c["!!"]} · 정상 {c["OK"]}</span></div>')

    if result["workers"]:
        parts.append('<div class="workers">')
        for w in result["workers"]:
            cls = LEVEL_CLASS[w["level"]]
            parts.append(f'<div class="worker {cls}"><div class="name">{e(w["label"])}</div>'
                         f'<div class="state">{e(w["state"])}</div><div class="detail">{e(w.get("detail") or "")}</div></div>')
        parts.append('</div>')

    # 문제 많은 영역이 위로
    order = {"XX": 0, "!!": 1, "OK": 2}
    secs = sorted(result["sections"], key=lambda s: min([order[i["level"]] for i in s["items"]] or [2]))
    parts.append('<div class="grid">')
    for s in secs:
        if s["title"].startswith("로봇 프로그램 상태"):
            continue   # 위 카드로 보여줌
        parts.append(f'<section class="sec"><h2>{e(s["title"])}</h2>')
        for i in sorted(s["items"], key=lambda i: order[i["level"]]):
            cls = LEVEL_CLASS[i["level"]]
            parts.append(f'<div class="item"><span class="badge {cls}">{LEVEL_TEXT[i["level"]]}</span>'
                         f'<span class="msg">{e(i["msg"])}</span></div>')
        parts.append('</section>')
    parts.append('</div><footer>읽기 전용 · 조작은 원격 데스크톱 · 비상 정지는 로봇 전원 스위치 · 운영_매뉴얼.md</footer>')
    parts.append('</div></body></html>')
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
