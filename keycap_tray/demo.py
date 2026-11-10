# -*- coding: utf-8 -*-
"""통신 없는 실물 시연 — 실제 주문 흐름과 같은 순서로 dobot_control 함수를 그대로 부른다.
보고를 올리지 않으므로 Mobius·다른 팀에 영향 없음. 게이트웨이가 켜져 있으면 잠금 때문에 시작하지 않음.

  python demo.py check   3대 연결 → 좌표 점검 → 알람·현재 좌표 확인 → 잔류 알람 해제 (팔 안 움직임)
  python demo.py home    한 대씩 홈 복귀 → 집기 자리 위로 비킴 (팔 움직임)
  python demo.py run     트레이 팔 → 키캡 벨트 2대 → 키캡 팔 2대 (단계별 시간 출력)
  python demo.py tray | belts | arms   그 단계만
  (tray 는 빈 트레이 개수를 실제로 하나 줄임 — 0개면 움직이지 않음. 개수는 상태판 또는 python tray_stock.py)

※ 단독 시연에서는 트레이 벨트(section0, 보드팀 담당)가 안 움직이므로,
  실물 트레이로 할 때는 키캡 단계 전에 트레이를 손으로 중앙(키캡 넣는 자리)에 옮길 것.
"""
import sys
import time

import dobot_control as dc
from worker_lock import WorkerLock

MODES = ("check", "home", "run", "tray", "belts", "arms")


def call(coro, timeout=3):
    return dc._loop.run_until_complete(dc.safe_call(coro, timeout))


def stage(name, fn):
    t = time.time()
    dc.log("DEMO", f"===== {name} 시작 =====")
    res = fn()
    dc.log("DEMO", f"===== {name} 끝: {res}  ({time.time() - t:.1f}초) =====")
    return res


def check():
    for name, cfg in dc.arms().items():
        p = cfg["port"]
        state = call(dc._arm_api.GetAlarmsState(portName=p))
        pose = call(dc._arm_api.GetPose(portName=p, isQueued=False))
        if not isinstance(pose, dict) or not isinstance(state, dict):
            dc.log("CHECK", f"{name} {p} 응답 없음 ({pose}) → 전원·USB 확인, DobotLab 이 포트를 잡고 있으면 연결 끊기")
            continue
        xyzr = tuple(round(pose[k], 1) for k in ("x", "y", "z", "r"))
        alarms = [dc.describe_alarm(c) for c in dc.alarm_codes(state)]
        dc.log("CHECK", f"{name} {p} 좌표={xyzr} 알람={alarms or '없음'}")
    dc.clear_alarms()


# 키캡 팔 두 대는 서로 마주 보고 있고(베이스 간격 370mm), 홈 자세는 정면으로 약 240mm 뻗음
# → 둘이 동시에 홈을 하면 가운데에서 겹칠 수 있음 (2026-11-02 사용자 확인). 그래서 한 대씩 하고 바로 비킴
OTHER_ARM_SAFE_X = 100.0  # 상대 팔 끝이 자기 정면(가운데 쪽)으로 이보다 나와 있으면 홈 보류


def wait_still(port, limit=60):
    prev, start = None, time.time()
    while time.time() - start < limit:
        time.sleep(0.5)
        now = call(dc._arm_api.GetPose(portName=port, isQueued=False))
        if time.time() - start > 3 and isinstance(now, dict) and prev and dc._same_pose(now, prev):
            return now
        prev = now
    return prev


def home():
    """한 대씩 홈 → 끝나면 바로 자기 집기 자리 위로 비킴 (다음 팔이 홈 할 때 안 겹치게)."""
    for name, cfg in dc.arms().items():
        p = cfg["port"]
        if name in dc.SETS:
            other = next(c for n, c in dc.SETS.items() if n != name)
            pose = call(dc._arm_api.GetPose(portName=other["port"], isQueued=False))
            if isinstance(pose, dict) and pose["x"] > OTHER_ARM_SAFE_X:
                dc.log("HOME", f"!! {name} 홈 보류 — 상대 팔({other['port']})이 가운데 쪽으로 나와 있음 "
                               f"(x={pose['x']:.0f}). 손으로 비킨 뒤 다시 실행")
                continue
        call(dc._arm_api.ClearAllAlarmsState(portName=p))
        dc.log("HOME", f"{name} {p} 홈 명령: {call(dc._arm_api.SetHOMECmd(portName=p, isQueued=False), 60)}")
        wait_still(p)
        x, y, z, r = cfg["pickup_slots"][1]
        # 홈 높이(z 150)에서 JUMP 하면 더 올라가려다 0x12 알람 → 빈손이라 MOVJ 로 비킴
        # 트레이 팔은 더미 맨 위(z 약 105)라 +50 이면 z 155 로 한계를 넘음 → away_lift_z(20) 만큼만 (2026-11-10)
        _, ok, _ = dc._loop.run_until_complete(
            dc.move_to(dc._arm_api, p, (x, y, z + cfg.get("away_lift_z", dc.LIFT_Z_OFFSET), r), mode=dc.PTP_MODE_MOVJ))
        dc.log("HOME", f"{name} 집기 자리 위로 비킴: {ok}")
    check()


def run(mode):
    total = time.time()
    if mode in ("run", "tray"):
        # 실제 운영과 같은 함수라 빈 트레이 개수(상태판 재고)도 하나 줄어듦. 시연 뒤 실물과 다르면 상태판에서 맞출 것
        try:
            stage("트레이 팔 (assigned)", lambda: dc.run_tray_arm("demo"))
        except dc.tray_stock.TrayEmpty as e:
            dc.log("DEMO", f"트레이 팔 안 움직임 — {e}")
    if mode in ("run", "belts"):
        belts = stage("키캡 벨트 (keycap_dispensed)", lambda: dc.run_keycap_conveyor("demo"))
        if mode == "run" and not all(belts.values()):
            dc.log("DEMO", "벨트 실패 → 키캡 팔은 안 움직임")
            return
    if mode in ("run", "arms"):
        stage("키캡 팔 (keycap_reached)",
              lambda: dc.run_keycap_arms("demo", lambda no, ok: dc.log("DEMO", f"  → keycap{no} {'성공' if ok else '실패'}")))
    dc.log("DEMO", f"전체 {time.time() - total:.1f}초")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode not in MODES:
        raise SystemExit("사용법: python demo.py " + " | ".join(MODES))
    with WorkerLock():
        dc.connect()
        try:
            {"check": check, "home": home}.get(mode, lambda: run(mode))()
        finally:
            dc.disconnect()
