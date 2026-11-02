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
                     "NOTIFY_PORT", "NOTIFY_BASE_URL", "SET1_PORT", "SET2_PORT", "TRAY_PORT", "LAYOUT"]),
}
counts = {"OK": 0, "!!": 0, "XX": 0}


def report(level, msg):
    counts[level] += 1
    print(f"  [{level}] {msg}")


def section(title):
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
            report("XX", f"{path.relative_to(ROOT)} 없음 - .example 복사해서 채우기")
            envs[name] = {}
            continue
        values = dotenv_values(path)
        missing = [k for k in keys if not values.get(k)]
        if missing:
            report("XX", f"{path.relative_to(ROOT)} 빈 값: {', '.join(missing)}")
        else:
            report("OK", f"{path.relative_to(ROOT)}")
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
            r = requests.get(url, headers=headers, timeout=5)
            con = r.json()["m2m:cin"]["con"] if r.status_code == 200 else None
            con = json.loads(con) if isinstance(con, str) else con
            mobius_ok = isinstance(con, dict)
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
            with socket.create_connection((host, port), timeout=5) as s:
                s.sendall(b"\x10" + bytes([len(body)]) + body)
                ack = s.recv(4)
                s.sendall(b"\xe0\x00")
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
            r = requests.get(f"{base}/{rn}", headers={**headers, "X-M2M-RI": str(uuid.uuid4())}, timeout=5)
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
    raw = ps("Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 6379,5000,5001,5002,5003 } | "
             "Select-Object LocalPort, OwningProcess | ConvertTo-Json -Compress")
    rows = json.loads(raw) if raw else []
    rows = rows if isinstance(rows, list) else [rows]
    listeners = {}
    for row in rows:
        listeners.setdefault(row["LocalPort"], set()).add(row["OwningProcess"])
    names = {6379: "Redis", 5000: "보드 Flask", 5002: "키캡 Flask", 5001: "보드 main", 5003: "키캡·트레이 게이트웨이"}
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


def check_robots(envs):
    section("로봇 USB (COM 포트)")
    present = set(re.findall(r"\((COM\d+)\)", ps("Get-CimInstance Win32_PnPEntity | Where-Object { $_.Name -match '\\(COM\\d+\\)' } "
                                                  "| ForEach-Object { $_.Name }")))
    spec = importlib.util.spec_from_file_location("board_config", ROOT / "board" / "board_config.py")
    board_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(board_config)
    t = envs["keycap_tray"]
    wanted = {"보드 팔(board_config.DOBOT_PORT)": board_config.DOBOT_PORT,
              "키캡 팔 세트1 EI·NS(SET1_PORT)": t.get("SET1_PORT"),
              "키캡 팔 세트2 TF·PJ(SET2_PORT)": t.get("SET2_PORT"),
              "트레이 팔(TRAY_PORT)": t.get("TRAY_PORT")}
    used = [p for p in wanted.values() if p]
    if len(used) != len(set(used)):
        report("XX", f"같은 COM 포트를 두 곳에서 씀: {used}")
    for name, port in wanted.items():
        report("OK" if port in present else "XX", f"{name} = {port} - {'연결됨' if port in present else '안 보임 (USB/전원 확인)'}")
    extra = present - set(used)
    if extra:
        report("!!", f"설정에 없는 COM 포트: {', '.join(sorted(extra))} (USB를 다른 구멍에 꽂아 번호가 바뀌었을 수 있음)")
    print("     ※ 어느 팔이 어느 번호인지는 확인 못 함 - USB 구멍 라벨대로 꽂혀 있는지 눈으로 확인")


def check_windows():
    section("노트북 설정")
    raw = ps("Get-NetFirewallRule -Direction Inbound -Enabled True -Action Allow | Get-NetFirewallPortFilter | "
             "Where-Object { $_.LocalPort -in '5001','5002','5003' } | ForEach-Object { $_.LocalPort }")
    opened = set(raw.split())
    for port in ("5001", "5002", "5003"):
        report("OK" if port in opened else "XX", f"방화벽 인바운드 {port} " + ("허용" if port in opened else "안 열림 (알림이 안 들어옴)"))

    def ac_value(sub, setting):
        # "현재 AC 전원 설정 색인: 0x..." 줄. 제조사 전원 구성표가 항목을 숨기면 None
        m = re.search(r"AC.*?0x([0-9a-fA-F]{8})", run(["powercfg", "/q", "scheme_current", sub, setting]))
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


def main():
    print("창고 노트북 시작 전 점검 (읽기 전용)")
    envs = check_env()
    check_network(envs)
    mobius_ok = check_platform(envs)
    check_subscriptions(envs, mobius_ok)
    check_local_services()
    check_robots(envs)
    check_windows()
    print(f"\n결과: OK {counts['OK']} / 주의 {counts['!!']} / 문제 {counts['XX']}")
    if counts["XX"]:
        print("[XX] 항목부터 해결 (README '문제가 생겼을 때', 운영_매뉴얼.md 참고)")


if __name__ == "__main__":
    main()
