# -*- coding: utf-8 -*-
"""
로봇 4대 자리 확인 (테이블 다시 조립한 뒤 - 어느 매직박스가 어느 자리 팔인지)

꽂혀 있는 Dobot을 USB 시리얼 순서대로 하나씩 골라, 팔 위치는 그대로 두고 끝(그리퍼/흡착컵)의
회전축 R만 30도 돌렸다가 되돌림 -> 어느 자리 팔이 움직였는지 보고 입력.
끝에 "설정상 자리 vs 실제 자리" 표를 보여줌. 설정 파일은 고치지 않음 (결과를 보고 따로 수정).

실행 (저장소 폴더에서):  .\venv\Scripts\python.exe keycap_tray\identify_arms.py
  - DobotLink 켜기, DobotLab 연결 끊기, 창고 프로그램(보드 main·게이트웨이) 끄기
  - 팔 주변에 손·물건 치우기. 이상하면 Ctrl+C 또는 로봇 전원 스위치
"""

import asyncio
import os
import re
import sys
import time

from DobotRPC import RPCClient, DobotlinkAdapter
from DobotRPC.Utils import loggers as _dobot_loggers

from usb_serial import list_ports

_dobot_loggers.set_use_file(False)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
R_DELTA = 30.0        # 끝 회전 각도 (도)
PTP_MOVJ_XYZ = 1      # 같은 x,y,z 로 MOVJ -> R 축만 돌아감
CALL_TIMEOUT = 5

PLACES = {  # 입력 키 -> 자리 이름
    "b": "보드 팔",
    "1": "키캡 팔 세트1 (EI·NS)",
    "2": "키캡 팔 세트2 (TF·PJ)",
    "t": "트레이 팔",
}
SETTING_WHERE = {
    "b": r"board\board_config.py  DOBOT_SERIAL",
    "1": r"keycap_tray\.env.local  SET1_SERIAL",
    "2": r"keycap_tray\.env.local  SET2_SERIAL",
    "t": r"keycap_tray\.env.local  TRAY_SERIAL",
}


def configured_serials():
    """설정에 적힌 자리별 시리얼 (시리얼 값만 읽음 - 다른 설정값은 읽지도 출력하지도 않음)"""
    out = {}
    try:
        with open(os.path.join(REPO, "board", "board_config.py"), encoding="utf-8") as f:
            m = re.search(r'^DOBOT_SERIAL\s*=\s*"([^"]*)"', f.read(), re.M)
            if m:
                out["b"] = m.group(1).strip().upper()
    except OSError:
        pass
    try:
        with open(os.path.join(HERE, ".env.local"), encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*(SET1|SET2|TRAY)_SERIAL\s*=\s*([0-9A-Za-z]*)", line)
                if m:
                    out[{"SET1": "1", "SET2": "2", "TRAY": "t"}[m.group(1)]] = m.group(2).strip().upper()
    except OSError:
        pass
    return out


async def call(coro, timeout=CALL_TIMEOUT):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        return "TIMEOUT"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"


async def wait_still(arm, port, timeout=6.0):
    prev, start = None, time.time()
    while time.time() - start < timeout:
        pose = await call(arm.GetPose(portName=port, isQueued=False))
        if isinstance(pose, dict) and prev and all(abs(pose[k] - prev[k]) < 0.3 for k in ("x", "y", "z", "r")):
            return pose
        prev = pose
        await asyncio.sleep(0.4)
    return prev


async def wiggle(arm, port):
    """끝(R축)만 R_DELTA 도 돌렸다가 원래대로. 반환: (성공 여부, 메시지)"""
    res = await call(arm.ConnectDobot(portName=port))
    if isinstance(res, str) and (res.startswith("ERROR") or res.startswith("TIMEOUT")):
        return False, f"연결 실패: {res} (DobotLab이 잡고 있거나 창고 프로그램이 켜져 있으면 끄기)"
    try:
        await call(arm.ClearAllAlarmsState(portName=port))
        pose = await call(arm.GetPose(portName=port, isQueued=False))
        if not isinstance(pose, dict) or "r" not in pose:
            return False, f"좌표를 못 읽음: {pose}"
        x, y, z, r = (pose[k] for k in ("x", "y", "z", "r"))
        delta = -R_DELTA if r > 0 else R_DELTA  # 0 쪽으로 돌려서 회전 한계를 안 넘게
        for _ in range(2):
            await call(arm.SetPTPCmd(portName=port, ptpMode=PTP_MOVJ_XYZ, x=x, y=y, z=z, r=r + delta,
                                     isQueued=False))
            await asyncio.sleep(0.3)
            await wait_still(arm, port)
            await call(arm.SetPTPCmd(portName=port, ptpMode=PTP_MOVJ_XYZ, x=x, y=y, z=z, r=r, isQueued=False))
            await asyncio.sleep(0.3)
            await wait_still(arm, port)
        alarms = await call(arm.GetAlarmsState(portName=port))
        return True, f"위치 x={x:.0f} y={y:.0f} z={z:.0f}, 끝 R {r:.0f}° -> {r + delta:.0f}° -> {r:.0f}° 2번 (알람 {alarms})"
    finally:
        await call(arm.DisconnectDobot(portName=port))


def main():
    ports = list_ports()
    if not ports:
        print("꽂혀 있는 Dobot이 없음 - 매직박스 전원·USB 확인 (DobotLink 드라이버 필요)")
        return
    cfg = configured_serials()
    by_serial = {s: k for k, s in cfg.items() if s}
    print(f"꽂혀 있는 Dobot {len(ports)}대: " + ", ".join(f"{s}({c})" for s, c in sorted(ports.items())))
    print("하나씩 끝(그리퍼/흡착컵)만 30도 돌렸다 되돌립니다. 팔 위치는 그대로.\n")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    arm = getattr(DobotlinkAdapter(RPCClient(), is_sync=False), "MagicianLite")

    seen = {}  # serial -> 입력한 자리 키
    try:
        for serial, port in sorted(ports.items()):
            expect = by_serial.get(serial)
            print(f"=== 시리얼 {serial} ({port}) - 설정상: {PLACES.get(expect, '설정에 없음')}")
            if input("  Enter = 돌려 보기, s = 건너뛰기: ").strip().lower() == "s":
                continue
            while True:
                ok, msg = loop.run_until_complete(wiggle(arm, port))
                print(("  " if ok else "  !! ") + msg)
                ans = input("  어느 팔이 돌았나요? b=보드 1=키캡세트1 2=키캡세트2 t=트레이 "
                            "r=다시 돌리기 ?=모르겠음: ").strip().lower()
                if ans != "r":
                    break
            seen[serial] = ans if ans in PLACES else "?"
            print()
    except (KeyboardInterrupt, EOFError):
        print("\n중단")

    print("\n===== 결과 =====")
    changes = []
    for serial, key in seen.items():
        expect = by_serial.get(serial)
        actual = PLACES.get(key, "모름")
        same = key == expect
        print(f"  {serial}: 설정상 {PLACES.get(expect, '없음'):<20} 실제 {actual:<20} {'OK' if same else '<- 다름'}")
        if not same and key in PLACES:
            changes.append(f"  {SETTING_WHERE[key]} = {serial}")
    if changes:
        print("\n설정을 실제에 맞추려면 (사람이 확인 후 수정):")
        print("\n".join(changes))
    elif seen:
        print("\n확인한 팔은 모두 설정과 같음")


if __name__ == "__main__":
    main()
