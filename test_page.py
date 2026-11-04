# -*- coding: utf-8 -*-
"""
창고 테스트 페이지 (http://<노트북 IP>:5080/test) - status_page.py가 /test 요청을 여기로 넘김
플랫폼 대신 cnt_process 스냅샷을 만들어 Mobius에 올림 = board/tools/process_comm_test.py의 웹 버전.
읽기·보내기 함수는 그 스크립트 것을 그대로 씀 (터미널 스크립트와 똑같이 나감)

⚠️ cnt_process는 플랫폼·키캡팀도 쓰는 공용 주문 채널 - 올리면 보드 main·키캡 장비가 실제로 움직임
- 같은 공유기(노트북과 같은 /24 망)에서만 열림. 상태판 화면엔 링크 없음 (바탕화면 '창고 테스트'가 탭으로 열어 줌)
- seq는 보내는 순간 서버가 "최신 + 1"로 붙임. 화면을 연 뒤 플랫폼이 새 스냅샷을 올렸으면 한 번 더 물어봄
- 주문이 빈 스냅샷(= 플랫폼 하드리셋 신호)은 하드리셋 버튼으로만 나감
비밀값(.env)은 읽기만 하고 화면·터미널에 안 냄
"""
import ipaddress
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "board" / "tools"))
_comm = None
RAIL_RE = re.compile(r"^[EINSTFPJ]_[rybg]$")
MAX_BODY = 64 * 1024


def comm():
    """process_comm_test 모듈 (처음 쓸 때 import - board/.env를 읽고, 비어 있으면 RuntimeError)"""
    global _comm
    if _comm is None:
        import process_comm_test
        _comm = process_comm_test
    return _comm


def allowed(handler):
    """노트북 자신(127.0.0.1) 또는 같은 공유기(노트북 IP와 같은 /24)에서 온 요청만"""
    try:
        client = ipaddress.ip_address(handler.client_address[0])
        if client.is_loopback:
            return True
        local = handler.connection.getsockname()[0]
        return client in ipaddress.ip_network(f"{local}/24", strict=False)
    except ValueError:
        return False


def _reply(handler, code, body, ctype="application/json; charset=utf-8"):
    data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def handle(handler, method):
    """/test 로 시작하는 요청 처리. 처리했으면 True"""
    path = handler.path.split("?")[0]
    if path not in ("/test", "/test/", "/test/latest", "/test/send"):
        return False
    if not allowed(handler):
        _reply(handler, 403, "<p>테스트 페이지는 창고 노트북과 같은 공유기에서만 열립니다.</p>", "text/html; charset=utf-8")
        return True
    try:
        m = comm()
    except Exception as e:   # .env 없음 등
        _reply(handler, 500, {"error": f"테스트 스크립트를 못 불러옴 ({type(e).__name__}: {e})"})
        return True
    if method == "GET" and path in ("/test", "/test/"):
        _reply(handler, 200, PAGE, "text/html; charset=utf-8")
    elif method == "GET" and path == "/test/latest":
        con = m.fetch_latest()
        if con is None:
            _reply(handler, 502, {"error": "플랫폼 서버(Mobius)에서 최신 주문을 못 읽었습니다. 네트워크를 확인하세요."})
        else:
            _reply(handler, 200, {"con": con, "statuses": m.STATUSES, "flow": m.NORMAL_FLOW, "faults": list(m.FAULTS),
                                  "read_at": datetime.now().strftime("%H:%M:%S")})
    elif method == "POST" and path == "/test/send":
        _send(handler, m)
    else:
        _reply(handler, 405, {"error": "지원 안 하는 요청"})
    return True


def _check_order(m, oid, o):
    """주문 하나 검사 + fault_section 채우기. 문제가 있으면 문구, 없으면 None"""
    if not isinstance(oid, str) or not oid or not isinstance(o, dict):
        return "주문 형식이 잘못됨"
    if o.get("board") not in m.BOARD_CODE:
        return f"{oid}: 보드 색이 잘못됨"
    keycap, colors = o.get("keycap"), o.get("colors")
    if not (isinstance(keycap, str) and len(keycap) == 4 and all(keycap[i] in m.KEYCAP_AXES[i] for i in range(4))):
        return f"{oid}: 키캡은 EI/SN/TF/JP 순서 4글자"
    if not (isinstance(colors, list) and len(colors) == 4 and all(c in m.CODE_BOARD for c in colors)):
        return f"{oid}: 칸 색은 r/y/g/b 4개"
    if o.get("status") not in m.STATUSES:
        return f"{oid}: 없는 단계 {o.get('status')}"
    fault = o.get("fault")
    if fault is None:
        o["fault_section"] = None
    elif fault not in m.FAULTS:
        return f"{oid}: 없는 fault {fault}"
    elif not o.get("fault_section"):
        o["fault_section"] = m.FAULTS[fault](o)
    return None


def _send(handler, m):
    try:
        length = int(handler.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("too big")
        req = json.loads(handler.rfile.read(length) or b"{}")
        if not isinstance(req, dict):
            raise ValueError("not dict")
    except ValueError:
        _reply(handler, 400, {"error": "요청 형식이 잘못됨"})
        return
    latest = m.fetch_latest()
    if latest is None:   # 최신 seq를 모르면 안 보냄 (seq가 꼬이면 플랫폼·키캡 쪽이 무시하거나 중복 처리)
        _reply(handler, 502, {"error": "플랫폼 서버에서 최신 주문을 못 읽어서 보내지 않았습니다."})
        return
    latest_seq = latest.get("seq")
    if not req.get("force") and req.get("base_seq") != latest_seq:
        _reply(handler, 409, {"error": "화면을 연 뒤 새 스냅샷이 올라왔습니다.", "latest_seq": latest_seq})
        return
    seq = latest_seq + 1 if isinstance(latest_seq, int) else int(time.time())

    snap = {"seq": seq, "updated_at": datetime.now(m.KST).isoformat(timespec="seconds"), "orders": {}}
    if req.get("reset"):
        what = "하드리셋 (orders 비움)"
    else:
        orders = req.get("orders")
        if not isinstance(orders, dict) or not orders:
            _reply(handler, 400, {"error": "주문이 없습니다. 빈 목록은 하드리셋 버튼으로만 보냅니다."})
            return
        for oid, o in orders.items():
            problem = _check_order(m, oid, o)
            if problem:
                _reply(handler, 400, {"error": problem})
                return
        snap["orders"] = orders
        krest = req.get("keycap_restock") or []
        brest = req.get("board_restock") or []
        if not (isinstance(krest, list) and all(isinstance(k, str) and RAIL_RE.match(k) for k in krest)):
            _reply(handler, 400, {"error": "키캡 보충 칸이 잘못됨"})
            return
        if not (isinstance(brest, list) and all(b in m.CODE_BOARD for b in brest)):
            _reply(handler, 400, {"error": "보드 보충 색이 잘못됨"})
            return
        if krest:
            snap["keycap_restock"] = krest
        if brest:
            snap["board_restock"] = brest
        what = f"주문 {len(orders)}건" + (f", 키캡 보충 {krest}" if krest else "") + (f", 보드 보충 {brest}" if brest else "")
    ok = m.post_snapshot(snap)
    print(f"[테스트 페이지] seq {seq} {what} -> {'올림' if ok else '실패'} (보낸 곳 {handler.client_address[0]})")
    if ok:
        _reply(handler, 200, {"ok": True, "seq": seq})
    else:
        _reply(handler, 502, {"error": "Mobius가 받지 않았습니다 (상태판 탭 로그 확인)."})


PAGE = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>창고 테스트</title>
<style>
:root {
  --steel:#D9DEDB; --plate:#EEF1EF; --plate-2:#E2E7E5; --ink:#1F2A2E; --soft:#56625E; --rule:#C3CAC7;
  --go:#1E7A4F; --caution:#E8A900; --stop:#C21F2E;
  --go-wash:#D5EADF; --caution-wash:#F8E7B0; --stop-wash:#F4D6D9;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --steel:#161C1D; --plate:#1F2729; --plate-2:#263033; --ink:#E4EAE8; --soft:#98A5A8; --rule:#34403F;
    --go:#4CC38A; --caution:#F2B300; --stop:#FF6B6B;
    --go-wash:#163326; --caution-wash:#3A2F0E; --stop-wash:#3D1A1C;
    color-scheme: dark;
  }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--steel); color:var(--ink); padding:0 16px 40px;
       font-family:"Malgun Gothic","Apple SD Gothic Neo","Noto Sans KR",system-ui,sans-serif; font-size:15px; line-height:1.5; }
.wrap { max-width:1120px; margin:0 auto; display:flex; flex-direction:column; gap:18px; }
.band { margin:0 -16px; padding:10px 16px; background:repeating-linear-gradient(135deg, var(--caution) 0 18px, #1F2A2E 18px 36px); }
.band div { max-width:1120px; margin:0 auto; background:var(--caution); color:#1F2A2E; font-weight:800;
            padding:8px 14px; border-radius:4px; display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; }
.band a { color:#1F2A2E; }
h1 { margin:6px 0 0; font-size:28px; }
h2 { margin:0 0 10px; font-size:16px; }
.sub { color:var(--soft); margin:2px 0 0; }
.grid { display:grid; grid-template-columns:minmax(0, 1.5fr) minmax(0, 1fr); gap:18px; align-items:start; }
.card { background:var(--plate); border-radius:10px; padding:16px; }
.col { display:flex; flex-direction:column; gap:18px; min-width:0; }
.tablewrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th { text-align:left; color:var(--soft); font-weight:700; font-size:12px; padding:4px 6px; border-bottom:1px solid var(--rule); }
td { padding:8px 6px; border-bottom:1px solid var(--rule); vertical-align:middle; }
tr.sel td { background:var(--go-wash); }
tr.changed td:first-child { box-shadow:inset 4px 0 0 var(--caution); }
.mono { font-family:Consolas, "D2Coding", monospace; }
.fault { color:var(--stop); font-weight:700; }
.dim { color:var(--soft); }
.err { color:var(--stop); font-weight:700; }
select, input[type=text] { font:inherit; font-size:14px; padding:5px 8px; border:1px solid var(--rule); border-radius:5px;
                           background:var(--steel); color:var(--ink); min-height:34px; }
button { font:inherit; font-size:14px; font-weight:700; border:0; border-radius:6px; padding:7px 14px; min-height:36px;
         cursor:pointer; background:var(--plate-2); color:var(--ink); }
button:disabled { opacity:.5; cursor:default; }
button.primary { background:var(--go); color:#fff; }
button.danger { background:var(--stop); color:#fff; }
button.small { padding:4px 10px; min-height:30px; font-size:13px; }
button:focus-visible, .chip:focus-visible { outline:3px solid var(--go); outline-offset:2px; }
.row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.field { display:flex; flex-direction:column; gap:4px; }
.field > span { font-size:12px; color:var(--soft); font-weight:700; }
.seg { display:inline-flex; background:var(--plate-2); border-radius:6px; padding:3px; gap:2px; flex-wrap:wrap; }
.seg button { background:transparent; min-height:30px; padding:4px 10px; color:var(--soft); }
.seg button[aria-pressed="true"] { background:var(--plate); color:var(--ink); box-shadow:0 0 0 1px var(--rule); }
.swatch { width:12px; height:12px; border-radius:3px; display:inline-block; vertical-align:-1px; margin-right:4px; }
.r { background:#E58AAE; } .y { background:#E9CC52; } .b { background:#5E93D6; } .g { background:#5DB07E; }
.slots { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:8px; }
.slot { background:var(--plate-2); border-radius:6px; padding:8px; display:flex; flex-direction:column; gap:6px; align-items:center; }
.slot b { font-size:13px; color:var(--soft); }
.chips { display:flex; gap:4px; flex-wrap:wrap; }
.slot .chips { flex-wrap:nowrap; justify-content:center; }
.chip { font-size:13px; padding:3px 8px; border-radius:4px; background:var(--plate-2); cursor:pointer; border:1px solid transparent; }
.chip[aria-pressed="true"] { border-color:var(--go); background:var(--go-wash); font-weight:700; }
pre { margin:0; background:var(--steel); border-radius:6px; padding:10px; font-size:12px; max-height:360px; overflow:auto; white-space:pre-wrap; }
.diff { font-size:13px; margin:0 0 10px; padding-left:18px; }
.diff li { margin:2px 0; }
.send { display:flex; gap:8px; align-items:center; justify-content:space-between; margin-top:12px; flex-wrap:wrap; }
.log { list-style:none; margin:0; padding:0; font-size:13px; }
.log li { padding:4px 0; border-bottom:1px solid var(--rule); }
.log .ok { color:var(--go); font-weight:700; }
.log .bad { color:var(--stop); font-weight:700; }
.zone { border:2px dashed var(--stop); }
.note { font-size:12px; color:var(--soft); margin:8px 0 0; }
.tag { font-size:11px; font-weight:800; padding:1px 6px; border-radius:3px; background:var(--caution-wash); margin-left:6px; }
@media (max-width:860px) { .grid { grid-template-columns:1fr; } .slots { grid-template-columns:repeat(2, minmax(0,1fr)); } }
</style>
</head>
<body>
<div class="band"><div><span>테스트 모드 - 여기서 보내면 진짜 주문 채널(cnt_process)에 올라가고 로봇이 실제로 움직입니다</span>
<a href="/">← 상태판으로</a></div></div>
<main class="wrap">
  <header>
    <h1>창고 테스트</h1>
    <p class="sub">플랫폼 대신 주문 스냅샷을 만들어 올립니다. 고친 내용은 오른쪽 "보낼 내용"에 모였다가 <b>cnt_process에 올리기</b>를 눌러야 나갑니다.
      키캡팀·플랫폼 장비도 같은 채널을 보니 테스트 전에 미리 알려 주세요.</p>
  </header>

  <div class="grid">
    <div class="col">
      <section class="card">
        <div class="row" style="justify-content:space-between;margin-bottom:8px">
          <h2 style="margin:0">지금 주문 목록 <span class="dim" style="font-weight:400;font-size:13px" id="baseinfo">읽는 중</span></h2>
          <button class="small" id="reload">최신으로 다시 읽기</button>
        </div>
        <div class="tablewrap"><table>
          <thead><tr><th>주문</th><th>보드</th><th>키캡</th><th>단계</th><th>fault</th></tr></thead>
          <tbody id="orders"></tbody>
        </table></div>
        <div class="row" id="editor" style="margin-top:12px"></div>
        <p class="note">줄을 누르면 그 주문을 고칩니다. 끝난 주문은 플랫폼처럼 목록에서 빼지 않고 completed로 남깁니다.</p>
      </section>

      <section class="card">
        <h2>새 주문</h2>
        <div class="row" style="gap:16px;align-items:flex-end">
          <label class="field"><span>주문 번호</span><input type="text" id="nid" class="mono" size="22"></label>
          <div class="field"><span>보드 색</span><div class="seg" id="nboard"></div></div>
          <label class="field"><span>스테이션</span><select id="nstation"><option>1</option><option>2</option><option>3</option></select></label>
        </div>
        <div class="field" style="margin-top:12px"><span>키캡 4칸 - 글자와 칸 색</span><div class="slots" id="nslots"></div></div>
        <div class="row" style="margin-top:12px;justify-content:space-between">
          <span class="dim" id="npreview"></span>
          <button class="primary" id="add">목록에 추가</button>
        </div>
      </section>

      <section class="card">
        <h2>재고 보충 신호</h2>
        <div class="field"><span>키캡 칸 (keycap_restock)</span><div class="chips" id="krest"></div></div>
        <div class="field" style="margin-top:10px"><span>보드 색 (board_restock)</span><div class="chips" id="brest"></div></div>
        <p class="note">고른 칸이 다음 스냅샷에 같이 실립니다. 올린 뒤에는 자동으로 비워집니다.</p>
      </section>

      <section class="card zone">
        <h2>하드리셋</h2>
        <p class="sub" style="margin:0 0 10px">주문이 빈 스냅샷을 올립니다. 창고·키캡 쪽 진행 중 주문 기억과 재고가 전부 처음으로 돌아갑니다 (전시 시작 때 한 번 쓰는 신호).</p>
        <button class="danger" id="reset">하드리셋 보내기</button>
      </section>
    </div>

    <div class="col">
      <section class="card" style="position:sticky;top:12px">
        <h2>보낼 내용</h2>
        <ul class="diff" id="diff"></ul>
        <details><summary class="dim" style="cursor:pointer;font-size:13px">스냅샷 전체 보기 (JSON, seq는 보낼 때 정해짐)</summary><pre class="mono" id="json"></pre></details>
        <div class="send">
          <button id="undo">되돌리기</button>
          <button class="primary" id="send">cnt_process에 올리기</button>
        </div>
      </section>
      <section class="card">
        <h2>보낸 기록</h2>
        <ul class="log" id="log"><li class="dim">아직 없음</li></ul>
      </section>
    </div>
  </div>
</main>
<script>
var FLOW = [], ALL = [], FAULTS = [];
var WORD = {assigned:"키캡 배출",keycap_mismatched:"키캡 재배출",keycap_dispensed:"키캡 벨트",keycap_reached:"키캡 팔",keycap_packed:"벨트 이동",
            tray_reached:"보드 팔",board_mismatched:"보드 다시",board_packed:"벨트 이동",pickup_reached:"픽업 대기",completed:"완료"};
var FAULT_WORD = {stage_timeout:"시간 초과", keycap_cartridge_failed:"카트리지 실패", keycap_driver_failed:"서보 드라이버 고장",
                  tray_load_failed:"트레이 투입 실패", board_pack_failed:"보드 놓기 실패", keycap_pack_failed:"키캡 놓기 실패",
                  keycap_empty:"키캡 없음", board_empty:"보드 없음", board_cartridge_failed:"보드 카트리지 실패"};
var BOARD = {red:"핑크", yellow:"옐로우", blue:"블루", green:"그린"}, CODE = {red:"r", yellow:"y", blue:"b", green:"g"};
var COLORS = [["r","핑크"],["y","옐로우"],["b","블루"],["g","그린"]];
var AXES = ["EI","SN","TF","JP"];   // 플랫폼 keycap 글자 순서 (1~4번 칸)

var base = null, draft = {}, sel = null, krest = [], brest = [], newOrder, busy = false;
function clone(x){ return JSON.parse(JSON.stringify(x)); }
function esc(s){ return String(s).replace(/[&<>"]/g, function(c){ return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]; }); }
function p2(n){ return ("0"+n).slice(-2); }
function stamp(){ var d = new Date(); return "order_test_" + p2(d.getMonth()+1) + p2(d.getDate()) + p2(d.getHours()) + p2(d.getMinutes()) + p2(d.getSeconds()); }
function freshNew(){ newOrder = {id:stamp(), board:"green", letters:["E","N","T","P"], colors:["r","r","r","r"]}; }
function fw(f){ return FAULT_WORD[f] ? FAULT_WORD[f] + " (" + f + ")" : f; }
freshNew();

function load(){
  document.getElementById("baseinfo").textContent = "읽는 중";
  fetch("/test/latest").then(function(r){ return r.json().then(function(j){ return {ok:r.ok, j:j}; }); }).then(function(x){
    if (!x.ok) { document.getElementById("baseinfo").innerHTML = '<span class="err">' + esc(x.j.error || "읽기 실패") + "</span>"; return; }
    FLOW = x.j.flow; ALL = x.j.statuses; FAULTS = x.j.faults;
    var con = x.j.con || {};
    base = {seq: con.seq, orders: con.orders || {}};
    document.getElementById("baseinfo").textContent = "플랫폼 최신 스냅샷 seq " + con.seq + " (" + x.j.read_at + " 읽음)";
    draft = clone(base.orders); krest = []; brest = []; if (sel && !draft[sel]) sel = null;
    draw();
  }).catch(function(e){ document.getElementById("baseinfo").innerHTML = '<span class="err">읽기 실패: ' + esc(e) + "</span>"; });
}

function swatchRow(o){ return (o.colors || []).map(function(c, i){ return '<span class="swatch '+esc(c)+'"></span>'+esc((o.keycap || "")[i] || "?"); }).join(" "); }

function drawOrders(){
  var ids = Object.keys(draft).sort(function(a,b){ return (draft[a].order_seq||0) - (draft[b].order_seq||0); });
  document.getElementById("orders").innerHTML = ids.length ? ids.map(function(id){
    var o = draft[id], was = base && base.orders[id], changed = !was || JSON.stringify(was) !== JSON.stringify(o);
    return '<tr data-id="'+esc(id)+'" class="'+(id===sel?"sel ":"")+(changed?"changed":"")+'" style="cursor:pointer">'+
      '<td class="mono">'+esc(id)+(was?"":'<span class="tag">새</span>')+'</td><td><span class="swatch '+esc(CODE[o.board]||"")+'"></span>'+esc(BOARD[o.board]||o.board)+'</td>'+
      '<td>'+swatchRow(o)+'</td><td>'+esc(o.status)+' <span class="dim">'+esc(WORD[o.status]||"")+'</span></td>'+
      '<td class="'+(o.fault?"fault":"dim")+'">'+(o.fault?esc(FAULT_WORD[o.fault]||o.fault):"-")+'</td></tr>';
  }).join("") : '<tr><td colspan="5" class="dim">주문 없음</td></tr>';
  document.querySelectorAll("#orders tr[data-id]").forEach(function(tr){ tr.onclick = function(){ sel = tr.getAttribute("data-id"); draw(); }; });
  var ed = document.getElementById("editor");
  if (!sel || !draft[sel]) { ed.innerHTML = '<span class="dim">고칠 주문을 위 목록에서 누르세요.</span>'; return; }
  var o = draft[sel], k = FLOW.indexOf(o.status), nxt = k >= 0 && k < FLOW.length - 1 ? FLOW[k+1] : null;
  ed.innerHTML = '<b class="mono">'+esc(sel)+'</b>'+
    '<button class="small primary" id="next"'+(nxt?"":" disabled")+'>다음 단계'+(nxt?" → "+esc(nxt):"")+'</button>'+
    '<select id="stat" aria-label="단계">'+ALL.map(function(s){ return '<option'+(s===o.status?" selected":"")+'>'+esc(s)+'</option>'; }).join("")+'</select>'+
    '<select id="flt" aria-label="fault"><option value="">fault 없음</option>'+FAULTS.map(function(f){ return '<option value="'+esc(f)+'"'+(f===o.fault?" selected":"")+'>'+esc(fw(f))+'</option>'; }).join("")+'</select>'+
    '<button class="small" id="done">완료 처리</button>';
  document.getElementById("next").onclick = function(){ o.status = nxt; draw(); };
  document.getElementById("stat").onchange = function(e){ o.status = e.target.value; draw(); };
  document.getElementById("flt").onchange = function(e){ o.fault = e.target.value || null; o.fault_section = null; draw(); };   // fault_section은 서버가 채움
  document.getElementById("done").onclick = function(){ o.status = "completed"; o.fault = null; o.fault_section = null; draw(); };
}

function drawNew(){
  document.getElementById("nid").value = newOrder.id;
  document.getElementById("nboard").innerHTML = Object.keys(BOARD).map(function(b){
    return '<button type="button" aria-pressed="'+(newOrder.board===b)+'" data-b="'+b+'"><span class="swatch '+CODE[b]+'"></span>'+BOARD[b]+'</button>'; }).join("");
  document.querySelectorAll("#nboard button").forEach(function(btn){ btn.onclick = function(){ newOrder.board = btn.getAttribute("data-b"); drawNew(); }; });
  document.getElementById("nslots").innerHTML = AXES.map(function(ax, i){
    return '<div class="slot"><b>'+(i+1)+'번 칸</b><div class="seg">'+ax.split("").map(function(L){
      return '<button type="button" aria-pressed="'+(newOrder.letters[i]===L)+'" data-i="'+i+'" data-l="'+L+'">'+L+'</button>'; }).join("")+'</div>'+
      '<div class="chips">'+COLORS.map(function(c){ return '<span class="chip" role="button" tabindex="0" title="'+c[1]+'" aria-pressed="'+(newOrder.colors[i]===c[0])+'" data-i="'+i+'" data-c="'+c[0]+'"><span class="swatch '+c[0]+'"></span></span>'; }).join("")+'</div></div>';
  }).join("");
  document.querySelectorAll("#nslots button").forEach(function(b){ b.onclick = function(){ newOrder.letters[+b.getAttribute("data-i")] = b.getAttribute("data-l"); drawNew(); }; });
  document.querySelectorAll("#nslots .chip").forEach(function(c){ c.onclick = function(){ newOrder.colors[+c.getAttribute("data-i")] = c.getAttribute("data-c"); drawNew(); }; });
  document.getElementById("npreview").innerHTML = BOARD[newOrder.board]+" 보드 · "+newOrder.letters.map(function(L,i){ return '<span class="swatch '+newOrder.colors[i]+'"></span>'+L+"_"+newOrder.colors[i]; }).join(" ");
}

function drawRestock(){
  var rails = []; "EINSTFPJ".split("").forEach(function(L){ COLORS.forEach(function(c){ rails.push(L+"_"+c[0]); }); });
  document.getElementById("krest").innerHTML = rails.map(function(r){ return '<span class="chip mono" role="button" tabindex="0" aria-pressed="'+(krest.indexOf(r)>=0)+'" data-r="'+r+'">'+r+'</span>'; }).join("");
  document.getElementById("brest").innerHTML = COLORS.map(function(c){ return '<span class="chip" role="button" tabindex="0" aria-pressed="'+(brest.indexOf(c[0])>=0)+'" data-b="'+c[0]+'"><span class="swatch '+c[0]+'"></span>'+c[1]+'</span>'; }).join("");
  function toggle(list, v){ var i = list.indexOf(v); if (i >= 0) list.splice(i, 1); else list.push(v); draw(); }
  document.querySelectorAll("#krest .chip").forEach(function(c){ c.onclick = function(){ toggle(krest, c.getAttribute("data-r")); }; });
  document.querySelectorAll("#brest .chip").forEach(function(c){ c.onclick = function(){ toggle(brest, c.getAttribute("data-b")); }; });
}

function payload(){
  var s = {orders:draft};
  if (krest.length) s.keycap_restock = krest.slice();
  if (brest.length) s.board_restock = brest.slice();
  return s;
}
function changes(){
  var out = [];
  if (!base) return out;
  Object.keys(draft).forEach(function(id){
    var o = draft[id], w = base.orders[id];
    if (!w) { out.push("새 주문 <b class=mono>"+esc(id)+"</b> "+esc(BOARD[o.board])+" "+esc(o.keycap)+" ("+esc(o.status)+")"); return; }
    if (w.status !== o.status) out.push('<b class="mono">'+esc(id)+"</b> "+esc(w.status)+" → <b>"+esc(o.status)+"</b>");
    if ((w.fault||null) !== (o.fault||null)) out.push('<b class="mono">'+esc(id)+"</b> fault "+esc(w.fault||"없음")+" → <b>"+esc(o.fault||"없음")+"</b>");
  });
  if (krest.length) out.push("키캡 보충 "+krest.join(", "));
  if (brest.length) out.push("보드 보충 "+brest.map(function(c){ return COLORS.filter(function(x){ return x[0]===c; })[0][1]; }).join(", "));
  return out;
}
function draw(){
  drawOrders(); drawRestock();
  var ch = changes();
  document.getElementById("diff").innerHTML = ch.length ? ch.map(function(c){ return "<li>"+c+"</li>"; }).join("") : '<li class="dim">바뀐 것 없음</li>';
  document.getElementById("send").disabled = busy || !ch.length || !Object.keys(draft).length;
  document.getElementById("reset").disabled = busy || !base;
  document.getElementById("json").textContent = JSON.stringify(payload(), null, 2);
}
var sentLog = [];
function logIt(ok, text){
  sentLog.unshift(new Date().toTimeString().slice(0,8)+'  <span class="'+(ok?"ok":"bad")+'">'+(ok?"올림":"실패")+'</span>  '+esc(text));
  document.getElementById("log").innerHTML = sentLog.map(function(s){ return "<li>"+s+"</li>"; }).join("");
}

function post(body, label){
  busy = true; draw();
  body.base_seq = base ? base.seq : null;
  fetch("/test/send", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)})
    .then(function(r){ return r.json().then(function(j){ return {code:r.status, j:j}; }); })
    .then(function(x){
      busy = false;
      if (x.code === 409) {
        if (confirm("화면을 연 뒤 플랫폼에 새 스냅샷(seq " + x.j.latest_seq + ")이 올라왔습니다.\n\n그래도 지금 화면 내용으로 덮어서 보낼까요?\n취소하면 최신 내용을 다시 읽습니다.")) {
          body.force = true; post(body, label);
        } else { load(); }
        return;
      }
      if (x.code !== 200) { logIt(false, label + " - " + (x.j.error || x.code)); alert("보내기 실패: " + (x.j.error || x.code)); draw(); return; }
      logIt(true, "seq " + x.j.seq + " - " + label);
      load();
    })
    .catch(function(e){ busy = false; logIt(false, label + " - " + e); alert("보내기 실패: " + e); draw(); });
}

document.getElementById("add").onclick = function(){
  var id = document.getElementById("nid").value.trim() || stamp();
  if (draft[id]) { alert("같은 주문 번호가 이미 목록에 있습니다."); return; }
  var seqs = Object.keys(draft).map(function(k){ return draft[k].order_seq || 0; });
  draft[id] = {station_id:document.getElementById("nstation").value, order_seq:Math.max.apply(null, seqs.concat([0]))+1, status:"assigned",
               board:newOrder.board, keycap:newOrder.letters.join(""), colors:newOrder.colors.slice(), fault:null, fault_section:null};
  sel = id; freshNew(); drawNew(); draw();
};
document.getElementById("undo").onclick = function(){ if (!base) return; draft = clone(base.orders); krest = []; brest = []; if (sel && !draft[sel]) sel = null; draw(); };
document.getElementById("reload").onclick = load;
document.getElementById("send").onclick = function(){
  var ch = changes().map(function(c){ return c.replace(/<[^>]+>/g, ""); });
  if (!confirm("cnt_process에 이 스냅샷을 올릴까요?\n\n- " + ch.join("\n- ") + "\n\n보드 main·키캡 장비가 실제로 움직입니다.")) return;
  post(payload(), ch.join(" / "));
};
document.getElementById("reset").onclick = function(){
  if (!confirm("⚠ 하드리셋\n\n주문이 빈 스냅샷을 올립니다.\n창고·키캡 쪽 진행 중 주문 기억과 재고가 전부 처음으로 돌아갑니다.\n\n정말 보낼까요?")) return;
  post({reset:true}, "하드리셋 (orders 비움)");
};
drawNew(); draw(); load();
</script>
</body>
</html>
"""
