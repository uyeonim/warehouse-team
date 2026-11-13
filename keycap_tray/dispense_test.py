# -*- coding: utf-8 -*-
"""
키캡 배출만 시험 - 로봇팔·컨베이어는 안 움직이고, 키캡 아두이노가 카트리지에서 키캡을 내보내기만 함.

원리: 아두이노 4대는 MQTT 토픽 sjuiot/warehouse/process 의 주문 스냅샷(status=assigned)을 보고 자기 축 글자를 배출함.
      그 토픽에 시험용 스냅샷을 직접 발행 (Mobius·게이트웨이·보드 main 은 건드리지 않음 -> 팔·벨트는 안 움직임).
      retain 없이 발행하므로 아두이노가 재부팅해도 다시 배출하지 않음.

사용 (대화형 - 인자 없이 실행):
  python dispense_test.py
    알파벳> E            <- 알파벳을 입력하면
    E 핑크·옐로우·블루·그린 4색 한 개씩 = 4개 배출합니다. 보낼까요? (y/n): y    <- y 로 4개가 차례로 나옴
  알파벳: E I N S T F P J.  여러 개도 됨: "E N" (다른 보드는 동시에), "E I" (같은 보드라 차례로 8개), "all" (8글자 x 4색 = 32개)
  특정 색만: "E_r" (칸 이름 = 글자_색, 색 r 핑크 y 옐로우 b 블루 g 그린). q = 끝

인자로 바로 (대화 없이 계획만 보려면 --dry):
  python dispense_test.py E              E 4색 한 개씩 (4개)
  python dispense_test.py E N T P        네 보드 동시에 앞 글자 4색 = 16개
  python dispense_test.py E_r N_y        칸 지정
  python dispense_test.py all --wait 15  라운드 사이 기다리는 초 (기본 10초 - 앞 배출이 끝나야 다음 라운드가 나감)
  python dispense_test.py --servo E_r    그 칸 서보만 푸시 -> 대기로 움직여 봄 (배출 주문 없이). 서보가 움직이는지 눈으로 확인용
  python dispense_test.py --repair E_r   꺼진(disable) 칸을 복구 (상태판 복구 버튼과 같음)

보내기 전에 대상 칸이 disable(카트리지 실패로 꺼짐)이면 알려 주고 복구할지 물음. 꺼진 칸은 아두이노가 배출하지 않음.
복구해도 센서·카트리지 문제가 그대로면 배출 직후 또 꺼짐 (= 아두이노가 키캡이 떨어지는 걸 못 봄 -> 실제로 안 나왔거나 IR 센서 문제).

한 보드는 한 번에 한 개만 배출하므로, 같은 알파벳의 4색은 4번에 나눠서(라운드) 차례로 나옴. 다른 보드 글자는 같은 라운드에서 동시에.

준비: 키캡 Flask(5002)가 켜져 있어야 함 (아두이노가 결과를 거기로 보고함). 창고 시작 후 쓰거나, 가짜 Mobius 로 시작해서 쓰면
      보고가 진짜 Mobius 로 안 나감 (창고 테스트 2번). 진짜 Mobius 면 이 시험 주문(dispense_test_...) 보고가 플랫폼 채널에 올라감.
배출하면 그 칸 재고가 하나 줄어듦 -> 끝난 뒤 상태판 재고에서 칸 숫자를 다시 맞출 것.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FLASK = os.path.join(HERE, "..", "keycap_flask", ".env")
TOPIC = "sjuiot/warehouse/process"
FLASK_URL = os.environ.get("KEYCAP_FLASK_URL", "http://127.0.0.1:5002").rstrip("/")
AXES = ("EI", "NS", "TF", "PJ")   # 키캡 4글자 중 몇 번째 글자를 어느 보드가 담당하는지 (EI·NS·TF·PJ 보드)
COLORS = "rybg"
COLOR_NAME = {"r": "핑크", "y": "옐로우", "b": "블루", "g": "그린"}
KST = timezone(timedelta(hours=9))
RAIL = re.compile(r"^([EINSTFPJ])_([rybg])$")
LETTER = re.compile(r"^[EINSTFPJ]$")


def broker():
    """키캡 Flask .env 의 MQTT_HOST/PORT (없으면 192.168.0.100:1883). 비밀값은 읽지 않음"""
    host, port = "192.168.0.100", 1883
    try:
        for line in open(ENV_FLASK, encoding="utf-8"):
            line = line.strip()
            if line.startswith("MQTT_HOST="):
                host = line.split("=", 1)[1].strip() or host
            elif line.startswith("MQTT_PORT="):
                port = int(line.split("=", 1)[1].strip() or port)
    except OSError:
        pass
    return host, port


def axis_of(letter):
    return next(i for i, pair in enumerate(AXES) if letter in pair)


def build_snapshot(rails, order_id=None):
    """칸 이름들(보드마다 하나씩) -> (스냅샷 dict, 설명 목록).
    축마다 칸이 있으면 그 글자·색, 없으면 어느 보드에도 없는 글자 'X' 를 넣어 그 보드는 아무것도 안 하게 함"""
    letters = ["X"] * 4
    colors = ["r"] * 4
    used = []
    for rail in rails:
        m = RAIL.match(rail)
        if not m:
            raise ValueError(f"칸 이름이 잘못됨: {rail} (예: E_r, N_y)")
        letter, color = m.groups()
        axis = axis_of(letter)
        if letters[axis] != "X":
            raise ValueError(f"{AXES[axis]} 보드 칸이 두 개 들어 있음 ({letters[axis]}, {letter}) - 한 번에 보드마다 하나씩만")
        letters[axis], colors[axis] = letter, color
        used.append((axis, letter, color))
    if not used:
        raise ValueError("칸을 하나 이상 주세요 (예: E_r)")
    now = datetime.now(KST)
    oid = order_id or f"dispense_test_{now.strftime('%m%d%H%M%S')}"
    snap = {
        "seq": int(time.time()),   # 아두이노는 직전과 다르면 새 스냅샷으로 봄 (10자리 정수)
        "updated_at": now.isoformat(timespec="seconds"),
        "orders": {oid: {"station_id": "1", "order_seq": 1, "status": "assigned", "board": "green",
                         "keycap": "".join(letters), "colors": colors, "fault": None, "fault_section": None}},
    }
    notes = [f"{AXES[a]} 보드: {l}_{c} ({COLOR_NAME[c]})" for a, l, c in sorted(used)]
    return snap, notes


def expand(tokens):
    """입력(알파벳 / 칸 이름 / all) -> 칸 이름 목록 (입력 순서 유지).
    알파벳 하나 = 그 글자의 4색 (r y b g 순서)"""
    rails = []
    for tok in tokens:
        up = tok.strip()
        if not up:
            continue
        if up.lower() == "all":
            letters = [l for pair in AXES for l in pair]
            rails += [f"{l}_{c}" for l in letters for c in COLORS]
        elif LETTER.match(up.upper()) and "_" not in up:
            rails += [f"{up.upper()}_{c}" for c in COLORS]
        elif len(up) == 3 and RAIL.match(up[0].upper() + up[1:].lower()):
            rails.append(up[0].upper() + up[1:].lower())
        else:
            raise ValueError(f"모르는 입력: {tok} (알파벳 E I N S T F P J, 칸 이름 E_r, 또는 all)")
    seen, out = set(), []
    for r in rails:   # 같은 칸이 두 번 들어와도 한 번만
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def plan_rounds(rails):
    """칸 목록 -> 라운드 목록. 한 보드는 한 라운드에 한 개만이라, 보드별로 줄을 세워 i 번째끼리 묶음"""
    per_board = {}
    for r in rails:
        per_board.setdefault(axis_of(r[0]), []).append(r)
    longest = max(len(v) for v in per_board.values())
    return [[per_board[a][i] for a in sorted(per_board) if i < len(per_board[a])] for i in range(longest)]


def read_stock(rails):
    """키캡 Flask 에서 칸별 {칸: (재고, 상태)}. 못 읽으면 None (Flask 꺼짐 등)"""
    try:
        import requests
        out = {}
        for axis in sorted({axis_of(r[0]) for r in rails}):
            res = requests.get(f"{FLASK_URL}/stock/keycap", params={"board": AXES[axis]}, timeout=4)
            res.raise_for_status()
            for rail, v in res.json().items():
                out[rail] = (int(v["count"]), v["status"])
        return out
    except Exception:
        return None


def check_round(rails, before, after):
    """한 라운드의 칸마다 실제 배출 여부 한 줄씩. 반환: 안 나온 칸 목록"""
    failed = []
    for r in rails:
        if after is None or before is None:
            print(f"      {r}  ? 확인 못 함 (키캡 Flask {FLASK_URL} 응답 없음)")
            continue
        b, a = before.get(r), after.get(r)
        if a is None or b is None:
            print(f"      {r}  ? 재고를 못 읽음")
        elif a[1] in ("disable", "empty"):
            why = "카트리지 실패로 꺼짐(disable) - 복구 필요" if a[1] == "disable" else "빈 칸(empty)"
            print(f"      {r}  ✖ 안 나옴: 상태 {a[1]} ({why}), 재고 {b[0]} -> {a[0]}")
            failed.append(r)
        elif a[0] < b[0]:
            print(f"      {r}  ✔ 배출 확인: 재고 {b[0]} -> {a[0]}")
        else:
            print(f"      {r}  ✖ 안 나온 듯: 재고 변화 없음 ({a[0]}, 상태 {a[1]}) - 아두이노가 안 받았거나 아직 처리 중일 수 있음")
            failed.append(r)
    return failed


def repair(rail):
    """키캡 Flask 에 복구(REPAIR) 요청 - 상태판 복구 버튼과 같음. 성공 여부"""
    import requests
    try:
        return requests.post(f"{FLASK_URL}/repair/{rail}", timeout=8).ok
    except Exception as e:
        print(f"      {rail} 복구 요청 실패: {type(e).__name__}")
        return False


def calib(rail, pos):
    """서보를 home / push / off 위치로 (키캡 Flask /calib -> MQTT command). 배출 주문이 아님"""
    import requests
    try:
        return requests.post(f"{FLASK_URL}/calib/{rail}", json={"pos": pos}, timeout=8).ok
    except Exception as e:
        print(f"      {rail} {pos} 요청 실패: {type(e).__name__}")
        return False


def precheck(rails, ask):
    """보내기 전 칸 상태 확인. 꺼진 칸이 있으면 복구할지 물음. 반환: 계속할지"""
    stock = read_stock(rails)
    if stock is None:
        print(f"  ? 칸 상태를 못 읽음 (키캡 Flask {FLASK_URL} 응답 없음) - 보낼 수는 있지만 결과 확인이 안 됨")
        return True
    bad = [r for r in rails if stock.get(r, (0, "idle"))[1] in ("disable", "empty")]
    if not bad:
        return True
    for r in bad:
        print(f"  !! {r} 상태 {stock[r][1]} - 아두이노가 이 칸은 배출하지 않음")
    ans = ask(f"  {', '.join(bad)} 을(를) 복구할까요? (y = 복구 후 진행 / n = 복구 없이 진행 / q = 취소): ").strip().lower()
    if ans == "q":
        return False
    if ans == "y":
        for r in bad:
            print(f"  {r} 복구 요청: {'보냄' if repair(r) else '실패'}")
        time.sleep(4)   # 아두이노가 복구 처리하고 상태를 올리는 시간
        again = read_stock(bad)
        still = [r for r in bad if again and again.get(r, (0, "idle"))[1] in ("disable", "empty")]
        print("  !! 복구해도 아직 꺼져 있음: " + ", ".join(still) if still else "  복구됨 - 이제 idle")
    return True


_last_seq = 0   # 이 프로그램이 보낸 가장 큰 seq (아두이노는 직전과 같은 seq 를 중복으로 버리므로 항상 늘어나게)


def next_seq():
    global _last_seq
    _last_seq = max(int(time.time()), _last_seq + 1)
    return _last_seq


def publish_all(snaps, wait, rounds=None):
    import paho.mqtt.client as mqtt
    host, port = broker()
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="dispense_test")
    except AttributeError:   # paho 1.x
        client = mqtt.Client(client_id="dispense_test")
    client.connect(host, port, keepalive=30)
    client.loop_start()
    ok_all = True
    failed_all = []
    try:
        for i, (snap, notes) in enumerate(snaps, 1):
            rails = rounds[i - 1] if rounds else []
            before = read_stock(rails) if rails else None
            snap["seq"] = next_seq()   # 보내는 순간 값으로, 항상 늘어남 - 직전과 같으면 아두이노가 "중복 스냅샷"으로 버림
            snap["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
            info = client.publish(TOPIC, json.dumps(snap, ensure_ascii=False), qos=1, retain=False)
            info.wait_for_publish(timeout=5)
            ok = info.is_published()
            ok_all = ok_all and ok
            print(f"  [{i}/{len(snaps)}] {', '.join(n.split(': ')[1] for n in notes)} MQTT 로 보냄: {'브로커가 받음' if ok else '실패(타임아웃)'}"
                  "  ※ 아두이노가 실제로 내보냈는지는 아래 확인 줄")
            time.sleep(wait)   # 아두이노가 배출을 끝내길 기다림 (마지막 라운드도 확인하려고 기다림. 다음 주문은 앞 주문이 목록에서 사라져야 받음)
            if rails:
                failed_all += check_round(rails, before, read_stock(rails))
    except KeyboardInterrupt:
        print("\n중단 - 이미 보낸 라운드는 아두이노가 계속 처리합니다")
    finally:
        client.loop_stop()
        client.disconnect()
    return ok_all, failed_all


def describe(rails):
    """'E 핑크·옐로우·블루·그린 4색' 같은 한 줄 요약"""
    by_letter = {}
    for r in rails:
        by_letter.setdefault(r[0], []).append(COLOR_NAME[r[2]])
    return " / ".join(f"{l} {'·'.join(cs)}" for l, cs in by_letter.items())


def run(tokens, wait, dry=False, ask=None):
    """입력 한 번 처리: 계획 보여 주고 y 면 배출. 반환: 보냈는지"""
    ask = ask or input
    rails = expand(tokens)
    if not rails:
        return False
    rounds = plan_rounds(rails)
    stamp = datetime.now(KST).strftime("%m%d%H%M%S")
    snaps = [build_snapshot(g, order_id=f"dispense_test_{stamp}_{i}") for i, g in enumerate(rounds, 1)]   # 라운드마다 다른 주문 번호 (같으면 아두이노 게이트가 안 풀림)
    host, port = broker()
    print(f"브로커 {host}:{port}  토픽 {TOPIC} (retain 없음)")
    for i, (_, notes) in enumerate(snaps, 1):
        print(f"  라운드 {i}: " + " / ".join(notes))
    print(f"  {describe(rails)} = 총 {len(rails)}개 배출, 라운드 {len(rounds)}번"
          + (f", 라운드 사이 {wait:g}초 (약 {wait * (len(rounds) - 1):.0f}초)" if len(rounds) > 1 else "")
          + "  (로봇팔·벨트는 안 움직임)")
    if dry:
        print("(--dry: 보내지 않음)")
        return False
    if not precheck(rails, ask):
        print("취소")
        return False
    if ask(f"보낼까요? 아두이노가 실제로 키캡 {len(rails)}개를 내보냅니다. (y/n): ").strip().lower() != "y":
        print("취소")
        return False
    ok, failed = publish_all(snaps, wait, rounds)
    if not ok:
        print("끝: 일부는 MQTT 로 못 보냄 (MQTT 연결 확인)")
    if failed:
        print(f"끝: 안 나온 칸 {len(failed)}개 → {', '.join(failed)}")
        print("  disable 이면 상태판의 복구 버튼(또는 python dispense_test.py 와 같은 PC 에서 키캡 Flask /repair/<칸>)으로 풀고, "
              "센서·카트리지(키캡 걸림, IR 센서)를 키캡팀과 확인하세요.")
    else:
        print("끝: 확인된 칸은 모두 재고가 줄었음 (키캡이 벨트에 떨어졌는지는 눈으로 확인)")
    return ok and not failed


def main():
    argv = sys.argv[1:]
    dry = "--dry" in argv
    wait = 10.0
    if "--wait" in argv:
        k = argv.index("--wait")
        try:
            wait = float(argv[k + 1])
        except (IndexError, ValueError):
            raise SystemExit("사용법: --wait 15")
        del argv[k:k + 2]
    args = [a for a in argv if a != "--dry"]
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return
    if args and args[0] in ("--servo", "--repair"):
        rails = expand(args[1:]) if len(args) > 1 else []
        if not rails or any(not RAIL.match(r) for r in rails):
            raise SystemExit("칸 이름을 주세요 (예: python dispense_test.py --servo E_r)")
        for r in rails:
            if args[0] == "--repair":
                print(f"{r} 복구 요청: {'보냄' if repair(r) else '실패'}")
                continue
            print(f"{r} 서보를 푸시 위치로 ... (키캡이 걸려 있으면 밀려 나올 수 있음)")
            print(f"  푸시: {'보냄' if calib(r, 'push') else '실패'}")
            time.sleep(3)
            print(f"  대기 위치로 복귀: {'보냄' if calib(r, 'home') else '실패'}")
            print("  서보가 움직였나요? 움직였는데 키캡이 안 나오면 카트리지·푸시 각도, 안 움직였으면 서보 전원·배선 문제입니다.")
        return
    if args:   # 인자로 바로
        try:
            run(args, wait, dry)
        except ValueError as e:
            raise SystemExit(str(e))
        return
    # 대화형
    print("키캡 배출 시험 (팔·벨트는 안 움직임). 알파벳을 입력하면 그 알파벳의 4색을 한 개씩 배출합니다.")
    print("  알파벳 E I N S T F P J / 여러 개 'E N' / 전부 'all' / 칸 하나 'E_r' / q = 끝")
    while True:
        try:
            line = input("\n알파벳> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if line.lower() in ("q", "quit", "exit"):
            break
        if not line:
            continue
        try:
            run(line.replace(",", " ").split(), wait, dry)
        except ValueError as e:
            print("  " + str(e))


if __name__ == "__main__":
    main()
