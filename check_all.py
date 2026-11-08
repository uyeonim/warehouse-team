# -*- coding: utf-8 -*-
"""
창고 노트북 시작 전 점검 - 읽기 전용 (아무것도 안 바꾸고, 로봇도 안 움직임)
실행: check_all.bat 더블클릭  (또는 venv\\Scripts\\python.exe check_all.py)
켜기 전 / 노트북을 옮긴 뒤 / 원격 데스크톱으로 상태 볼 때. [XX] 가 있으면 README '문제가 생겼을 때' 참고.
"""
import importlib.util
import json
import locale
import re
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import requests
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
ENV_FILES = {
    "board": (ROOT / "board" / ".env",
              ["COSS_BASE_URL", "AE_NAME", "ORIGIN", "REDIS_URL", "FLASK_BASE_URL", "NOTIFY_PORT", "NOTIFY_BASE_URL"]),
    "keycap_flask": (ROOT / "keycap_flask" / ".env",
                     ["COSS_BASE_URL", "AE_NAME", "ORIGIN", "REDIS_URL", "MQTT_HOST", "MQTT_PORT"]),
    "keycap_tray": (ROOT / "keycap_tray" / ".env.local",
                    ["WAREHOUSE_PROFILE", "MOBIUS_URL", "MOBIUS_CB", "MOBIUS_AE_RN", "MOBIUS_AEI", "BOARD_API_URL",
                     "NOTIFY_PORT", "NOTIFY_BASE_URL", "SET1_PORT", "SET2_PORT", "TRAY_PORT", "LAYOUT",
                     "PLACE_LIFT"]),   # PLACE_LIFT 가 빠지면 코드 기본값 1(놓은 뒤 들기·당기기)로 조용히 돌아가 키캡 단계가 약 10초 느려짐
}
# 한 번 점검한 결과 - run_checks()가 매번 비우고 채움. 명령창(check_all.bat)과 상태판(status_page.py)이 같이 씀
counts = {"OK": 0, "!!": 0, "XX": 0}
sections = []     # [{"title": ..., "items": [{"level": "OK"|"!!"|"XX", "msg": ...}]}]
workers = []      # heartbeat 요약 - 상태판 맨 위에 크게 표시
facts = {}        # 상태판이 장비별 램프를 정하는 데 쓰는 원자료 (프로그램 상태 코드, COM 인식, 포트, 접속 여부)
_print = True


def report(level, msg):
    counts[level] += 1
    if sections:
        sections[-1]["items"].append({"level": level, "msg": msg})
    if _print:
        print(f"  [{level}] {msg}")


def section(title):
    sections.append({"title": title, "items": []})
    if _print:
        print(f"\n== {title}")


def ps(command):
    """PowerShell 한 줄 실행 -> 출력 문자열 (읽기 전용 조회에만 사용)"""
    cmd = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command
    out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, timeout=30)
    return out.stdout.decode("utf-8", "replace").strip()


def short_err(e):
    """긴 예외 메시지 -> 한눈에 보이는 이유"""
    text = str(e)
    if "timed out" in text or "Timeout" in type(e).__name__:
        return "응답 없음 (시간 초과 - 같은 망인지, 서버가 켜져 있는지)"
    if "10061" in text or "refused" in text.lower():
        return "연결 거부 (서버 프로그램이 꺼져 있음)"
    return text[:120]


def run(args):
    out = subprocess.run(args, capture_output=True, timeout=30)
    return out.stdout.decode(locale.getpreferredencoding(False), "replace")


def check_env():
    section("설정 파일")
    envs = {}
    for name, (path, keys) in ENV_FILES.items():
        if not path.is_file():
            report("XX", f"{path.relative_to(ROOT).as_posix()} 없음 - .example 복사해서 채우기")
            envs[name] = {}
            continue
        values = dotenv_values(path)
        missing = [k for k in keys if not values.get(k)]
        if missing:
            report("XX", f"{path.relative_to(ROOT).as_posix()} 빈 값: {', '.join(missing)}")
        else:
            report("OK", f"{path.relative_to(ROOT).as_posix()}")
        envs[name] = values
    return envs


def check_network(envs):
    section("네트워크")
    ips = ps("Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and "
             "$_.IPAddress -notlike '169.254.*' } | ForEach-Object { $_.IPAddress }").split()
    report("OK" if any(ip.startswith("192.168.0.") for ip in ips) else "XX",
           f"이 노트북 IP: {', '.join(ips) or '없음'} (전시장 망 192.168.0.x 필요)")
    ssid = re.search(r"^\s*SSID\s*:\s*(.+)$", run(["netsh", "wlan", "show", "interfaces"]), re.M)
    if ssid:
        report("!!" if ssid.group(1).strip() != "sjiot" else "OK",
               f"Wi-Fi: {ssid.group(1).strip()} (전시장은 sjiot 또는 유선. 다른 망 자동 연결 꺼둘 것)")
    for name in ("board", "keycap_flask", "keycap_tray"):
        url = envs[name].get("NOTIFY_BASE_URL") or ("http://192.168.0.12:5002" if name == "keycap_flask" else "")
        host = re.sub(r"^https?://", "", url).split(":")[0]
        if host:
            report("OK" if host in ips else "XX",
                   f"{name} 알림 주소 {url} - {'이 노트북 IP와 일치' if host in ips else '이 노트북 IP가 아님 (구독/알림 안 옴)'}")


def check_platform(envs):
    """Mobius·Mosquitto 접속 확인. 반환: Mobius 접속 성공 여부 (구독 확인을 할지 결정)"""
    section("플랫폼 서버")
    b = envs["board"]
    mobius_ok = False
    if b.get("COSS_BASE_URL"):
        url = f"{b['COSS_BASE_URL'].rstrip('/')}/{b.get('AE_NAME')}/cnt_process/la"
        headers = {"Accept": "application/json", "X-M2M-RI": str(uuid.uuid4()), "X-M2M-RVI": "3",
                   "X-M2M-Origin": b.get("ORIGIN") or ""}
        try:
            r = requests.get(url, headers=headers, timeout=3)
            con = r.json()["m2m:cin"]["con"] if r.status_code == 200 else None
            con = json.loads(con) if isinstance(con, str) else con
            mobius_ok = isinstance(con, dict)
            facts["mobius"] = mobius_ok
            if mobius_ok:
                orders = {oid: o.get("status") for oid, o in con.get("orders", {}).items()}
                report("OK", f"Mobius 응답 - cnt_process seq={con.get('seq')}, 진행 중 주문 {orders or '없음'}")
            else:
                report("XX", f"Mobius cnt_process 읽기 실패 (HTTP {r.status_code})")
        except Exception as e:
            report("XX", f"Mobius 접속 실패: {short_err(e)}")
    k = envs["keycap_flask"]
    host, port = k.get("MQTT_HOST"), int(k.get("MQTT_PORT") or 1883)
    if host:
        try:
            cid = b"check_all"
            body = b"\x00\x04MQTT\x04\x02\x00\x0a" + len(cid).to_bytes(2, "big") + cid
            with socket.create_connection((host, port), timeout=3) as s:
                s.sendall(b"\x10" + bytes([len(body)]) + body)
                ack = s.recv(4)
                s.sendall(b"\xe0\x00")
            facts["mqtt"] = len(ack) == 4 and ack[3] == 0
            report("OK" if len(ack) == 4 and ack[3] == 0 else "XX",
                   f"Mosquitto {host}:{port} 익명 접속 {'허용' if len(ack) == 4 and ack[3] == 0 else '거부'}")
        except Exception as e:
            report("XX", f"Mosquitto {host}:{port} 접속 실패: {short_err(e)} (키캡 아두이노가 주문을 못 받음)")
    return mobius_ok


def check_subscriptions(envs, mobius_ok):
    section("Mobius 구독")
    b = envs["board"]
    if not mobius_ok:
        report("XX", "Mobius에 접속이 안 돼서 확인 못 함")
        return
    base = f"{b['COSS_BASE_URL'].rstrip('/')}/{b.get('AE_NAME')}/cnt_process"
    headers = {"Accept": "application/json", "X-M2M-RVI": "3", "X-M2M-Origin": b.get("ORIGIN") or ""}
    expected = {
        "sub_warehouse_robotarm": envs["board"].get("NOTIFY_BASE_URL"),
        "sub_warehouse_keycap_robotarm": envs["keycap_tray"].get("NOTIFY_BASE_URL"),
        "sub_warehouse_keycap_cartridge": envs["keycap_flask"].get("NOTIFY_BASE_URL") or "http://192.168.0.12:5002",
    }
    for rn, want in expected.items():
        try:
            r = requests.get(f"{base}/{rn}", headers={**headers, "X-M2M-RI": str(uuid.uuid4())}, timeout=3)
        except Exception as e:
            report("XX", f"{rn} 조회 실패: {short_err(e)}")
            continue
        if r.status_code == 404:
            report("XX", f"{rn} 없음 - 해당 프로그램 켜고 구독 등록 스크립트 실행")
            continue
        nu = (r.json().get("m2m:sub") or {}).get("nu") or []
        want_nu = f"{(want or '').rstrip('/')}/notify/process"
        report("OK" if want_nu in nu else "XX",
               f"{rn} -> {', '.join(nu)}" + ("" if want_nu in nu else f" (기대: {want_nu} - 다시 등록 필요)"))


def check_local_services():
    section("창고 노트북 프로그램")
    raw = ps("Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 6379,5000,5001,5002,5003,5080 } | "
             "Select-Object LocalPort, OwningProcess | ConvertTo-Json -Compress")
    rows = json.loads(raw) if raw else []
    rows = rows if isinstance(rows, list) else [rows]
    listeners = {}
    for row in rows:
        listeners.setdefault(row["LocalPort"], set()).add(row["OwningProcess"])
    facts["ports"] = {str(p): len(listeners.get(p, ())) for p in (6379, 5000, 5001, 5002, 5003, 5080)}
    names = {6379: "Redis", 5000: "보드 Flask", 5002: "키캡 Flask", 5001: "보드 main", 5003: "키캡·트레이 게이트웨이",
             5080: "상태판"}
    for port, name in names.items():
        n = len(listeners.get(port, ()))
        if n > 1:
            report("XX", f"{name}({port}) {n}개가 동시에 떠 있음 - 하나 끄기")
        elif n == 1:
            report("OK", f"{name}({port}) 실행 중")
        else:
            report("XX" if port in (6379, 5000, 5002) else "!!",
                   f"{name}({port}) 꺼져 있음" + ("" if port in (6379, 5000, 5002) else " (start_all로 켜기)"))
    try:
        stock = requests.get("http://127.0.0.1:5000/stock/board", timeout=3).json()
        report("OK", f"보드 Flask -> Redis 재고: {stock}")
    except Exception:
        pass
    procs = ps("Get-Process | Select-Object -ExpandProperty ProcessName").split()
    report("OK" if "DobotLink" in procs else "!!", "DobotLink " + ("실행 중" if "DobotLink" in procs else "꺼져 있음 (start_all이 켬)"))
    if "DobotLab" in procs:
        report("!!", "DobotLab 실행 중 - 거기서 로봇을 연결하면 COM 포트 충돌")


BUSY_WARN_SEC = 90   # 한 작업이 이보다 오래 busy면 멈춘 것으로 의심 (가장 긴 단계: 키캡 적재 약 33초)


def check_heartbeats(envs):
    """로봇 프로그램이 Redis hb:* 에 5초마다 남기는 상태 (heartbeat.py). 30초 넘게 안 남기면 키가 사라짐"""
    section("로봇 프로그램 상태 (heartbeat)")
    from datetime import datetime
    from redis import Redis
    try:
        r = Redis.from_url(envs["board"].get("REDIS_URL") or "redis://127.0.0.1:6379/0",
                           decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
        raw = {name: r.get(f"hb:{name}") for name in ("board_main", "keycap_tray")}
    except Exception as e:
        report("XX", f"Redis 접속 실패 - 상태 확인 못 함: {short_err(e)}")
        for label in ("보드 main", "키캡·트레이 게이트웨이"):
            workers.append({"label": label, "level": "XX", "state": "확인 불가", "detail": "Redis 꺼짐 - 상태 기록을 못 읽음"})
        facts["hb"] = None
        return
    labels = {"board_main": "보드 main", "keycap_tray": "키캡·트레이 게이트웨이"}
    states = {"starting": "시작 중", "waiting_mobius": "Mobius 대기 중", "idle": "대기 중",
              "busy": "작업 중", "link_lost": "로봇 연결 끊김 (재연결 시도 중)"}
    facts["hb"] = {}
    for name, value in raw.items():
        label = labels[name]
        if not value:
            report("XX", f"{label}: 상태 기록 없음 - 꺼져 있거나 30초 넘게 멈춤")
            workers.append({"label": label, "level": "XX", "state": "꺼짐 / 멈춤", "detail": "30초 넘게 상태 기록 없음"})
            continue
        hb = json.loads(value)
        age = (datetime.now() - datetime.strptime(hb["since"], "%Y-%m-%d %H:%M:%S")).total_seconds()
        mode = " [리허설]" if hb.get("mode") == "rehearsal" else ""
        snap = hb.get("last_snapshot") or {}
        last = f" / 마지막 스냅샷 seq={snap.get('seq')} {snap.get('time', '')[11:]}({snap.get('via')})" if snap else ""
        state = hb.get("state")
        facts["hb"][name] = {"state": state, "job": hb.get("job"), "age": round(age), "mode": hb.get("mode")}
        text = f"{label}{mode}: {states.get(state, state)}"
        if state == "busy":
            text += f" - {hb.get('job')} ({age:.0f}초째)"
        if state in ("link_lost", "waiting_mobius"):
            level, detail = "XX", f"{age:.0f}초째"
            report(level, text + f" ({detail})")
        elif state == "busy" and age > BUSY_WARN_SEC:
            level, detail = "!!", f"{hb.get('job')} - {age:.0f}초째, 멈춘 것 같음"
            report(level, text + " - 너무 오래 걸림, 멈춘 것 같음 (창/로그 확인)")
        else:
            level = "OK"
            detail = f"{hb.get('job')} ({age:.0f}초째)" if state == "busy" else last.lstrip(" /")
            report(level, text + last)
        workers.append({"label": label + mode, "level": level, "state": states.get(state, state), "detail": detail})
        if hb.get("poll_ok") is False:
            report("!!", f"{label}: Mobius 5초 조회 실패 중 (알림만으로 받는 중)")
        if hb.get("pending_reports"):
            report("!!", f"{label}: 못 보낸 보고 {hb['pending_reports']}건 재전송 대기 중")


def check_robots(envs):
    section("로봇 USB (시리얼 -> COM)")
    if str(ROOT / "board") not in sys.path:
        sys.path.insert(0, str(ROOT / "board"))   # board_config가 usb_serial을 import
    import usb_serial
    usb = usb_serial.list_ports()                  # {시리얼: COM} 지금 꽂힌 Dobot
    present = usb_serial._present_coms()
    spec = importlib.util.spec_from_file_location("board_config", ROOT / "board" / "board_config.py")
    board_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(board_config)
    t = envs["keycap_tray"]
    arms = {"board": ("보드 팔", board_config.DOBOT_SERIAL, board_config.DOBOT_PORT_FALLBACK),
            "set1": ("키캡 팔 세트1 EI·NS", t.get("SET1_SERIAL"), t.get("SET1_PORT")),
            "set2": ("키캡 팔 세트2 TF·PJ", t.get("SET2_SERIAL"), t.get("SET2_PORT")),
            "tray": ("트레이 팔", t.get("TRAY_SERIAL"), t.get("TRAY_PORT"))}
    used, facts["com"] = [], {}
    for key, (name, serial, port) in arms.items():
        serial = (serial or "").strip().upper()
        com = usb_serial.resolve_port(serial, port, usb)
        ok = com is not None and com in present
        facts["com"][key] = ok
        used.append(com)
        how = f"시리얼 {serial}" if serial else "시리얼 미설정 - COM 번호 고정 (USB 다시 꽂으면 바뀔 수 있음)"
        if ok:
            report("OK" if serial else "!!", f"{name} = {com} ({how})")
        else:
            report("XX", f"{name} 안 보임 ({how}) - USB/전원 확인")
    found = [c for c in used if c]
    if len(found) != len(set(found)):
        report("XX", f"같은 COM 포트를 두 팔이 씀: {found}")
    extra = {s: c for s, c in usb.items() if c not in found}
    if extra:
        report("!!", "설정에 없는 Dobot: " + ", ".join(f"{c}(시리얼 {s})" for s, c in sorted(extra.items(), key=lambda x: x[1])))


def check_windows():
    section("노트북 설정")
    # 우리가 만든 규칙(warehouse-*)만 조회 - 전체 규칙을 훑으면 30초 걸림
    raw = ps("Get-NetFirewallRule -DisplayName 'warehouse-*' -ErrorAction SilentlyContinue | "
             "Where-Object { $_.Direction -eq 'Inbound' -and $_.Enabled -eq 'True' -and $_.Action -eq 'Allow' } | "
             "Get-NetFirewallPortFilter | ForEach-Object { $_.LocalPort }")
    opened = set(raw.split())
    for port in ("5001", "5002", "5003"):
        report("OK" if port in opened else "XX", f"방화벽 인바운드 {port} " + ("허용" if port in opened else "안 열림 (알림이 안 들어옴)"))
    report("OK" if "5080" in opened else "!!", "방화벽 인바운드 5080 " +
           ("허용" if "5080" in opened else "안 열림 (상태판을 다른 기기에서 못 봄)"))

    def ac_value(sub, setting):
        # "현재 AC 전원 설정 색인: 0x..." 줄. /qh = 제조사 구성표(ASUS Silent 등)가 숨긴 항목(덮개)까지. 못 읽으면 None
        m = re.search(r"AC.*?0x([0-9a-fA-F]{8})", run(["powercfg", "/qh", "scheme_current", sub, setting]))
        return int(m.group(1), 16) if m else None
    sleep = ac_value("SUB_SLEEP", "STANDBYIDLE")
    lid = ac_value("4f971e89-eebd-4455-a8de-9e59040e7347", "5ca83367-6e45-459f-a27b-476b1d01c936")  # 덮개 닫기 동작
    report("OK" if sleep == 0 else "!!", f"전원 연결 시 절전: {'안 함' if sleep == 0 else f'{sleep // 60}분 뒤 절전' if sleep else '확인 못 함'}")
    report("OK" if lid == 0 else "!!", "전원 연결 시 덮개 닫기: " + ("아무것도 안 함" if lid == 0 else
           "절전/종료 등 (테이블 밑에 두면 멈춤)" if lid is not None else
           "자동 확인 불가 - 제어판 > 전원 옵션 > 덮개를 닫으면 수행할 작업에서 직접 확인"))
    on_ac = ps("(Get-CimInstance Win32_Battery | Select-Object -First 1).BatteryStatus")
    if on_ac and on_ac != "2":
        report("!!", "충전기가 연결 안 된 것 같음 (배터리로 동작 중)")


def run_checks(print_output=True):
    """전체 점검 1회. 결과 dict 반환 (상태판이 그대로 씀). 아무것도 안 바꿈"""
    global _print
    _print = print_output
    counts.update({"OK": 0, "!!": 0, "XX": 0})
    sections.clear()
    workers.clear()
    facts.clear()
    envs = check_env()
    check_network(envs)
    mobius_ok = check_platform(envs)
    check_subscriptions(envs, mobius_ok)
    check_local_services()
    check_heartbeats(envs)
    check_robots(envs)
    check_windows()
    import time
    return {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "counts": dict(counts),
            "workers": list(workers), "facts": dict(facts), "sections": [dict(s, items=list(s["items"])) for s in sections]}


def main():
    print("창고 노트북 시작 전 점검 (읽기 전용)")
    run_checks(print_output=True)
    print(f"\n결과: OK {counts['OK']} / 주의 {counts['!!']} / 문제 {counts['XX']}")
    if counts["XX"]:
        print("[XX] 항목부터 해결 (README '문제가 생겼을 때', 운영_매뉴얼.md 참고)")


if __name__ == "__main__":
    main()
