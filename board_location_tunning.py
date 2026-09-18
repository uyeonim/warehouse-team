import asyncio
import os
import time

import requests
from dotenv import load_dotenv
from DobotRPC import RPCClient, DobotlinkAdapter

load_dotenv()

DOBOT_PORT = "COM17"
FLASK_BASE_URL = os.getenv("FLASK_BASE_URL", "").rstrip("/")

# board_robot_arm.py와 동일한 값 - 좌표 튜닝 결과는 여기 반영해서 본 코드에도 복사할 것
# ⚠️ 2026-09-18 재실측(count=3 기준): X는 통일 안 함(색상별 실측), Z는 (3-1)*20mm
#    빼서 count=1 기준으로 환산 후 4색 평균(-33.27)으로 통일, R은 실측값 그대로.
PICK_POSE_BASE = {
    "red":    (215.5975, 134.2798, -33.27, 31.9158),
    "green":  (249.1647,  48.9685, -33.27, 11.1187),
    "blue":   (248.0774, -36.6399, -33.27, -8.4016),
    "yellow": (205.0976, -132.5483, -33.27, -32.8733),
}
BOARD_THICKNESS_MM = 20.0
BOARD_COLORS = ["red", "yellow", "blue", "green"]

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

dobotlink = DobotlinkAdapter(RPCClient(), is_sync=False)
arm_api = dobotlink.MagicianLite


async def safe_call(coro, timeout_sec=5):
    try:
        return await asyncio.wait_for(coro, timeout=timeout_sec)
    except asyncio.TimeoutError:
        return "TIMEOUT"
    except Exception as e:
        return f"ERROR: {e}"


def get_pose():
    return loop.run_until_complete(safe_call(arm_api.GetPose(portName=DOBOT_PORT, isQueued=False)))


def move_to(x, y, z, r):
    res = loop.run_until_complete(safe_call(
        arm_api.SetPTPCmd(portName=DOBOT_PORT, ptpMode=0, x=x, y=y, z=z, r=r, isQueued=False)
    ))
    print("이동 명령 결과:", res)
    time.sleep(3)
    print("이동 후 실제 위치:", get_pose())


def fetch_stock(color):
    if not FLASK_BASE_URL:
        raise RuntimeError(".env에 FLASK_BASE_URL이 없음")
    resp = requests.get(f"{FLASK_BASE_URL}/stock/board", timeout=5)
    resp.raise_for_status()
    data = resp.json()
    return int(data.get(color, 0))


def compute_pick_pose(color, count):
    x, y, base_z, r = PICK_POSE_BASE[color]
    pick_z = base_z + (count - 1) * BOARD_THICKNESS_MM
    return (x, y, pick_z, r)


def main():
    connect_res = loop.run_until_complete(safe_call(arm_api.ConnectDobot(portName=DOBOT_PORT)))
    print("연결 결과:", connect_res)
    print("\n현재 위치:", get_pose())

    print("\n색깔 입력 (red/yellow/blue/green), 'q'로 종료")
    while True:
        color = input("\n색깔: ").strip().lower()
        if color == "q":
            break
        if color not in BOARD_COLORS:
            print(f"알 수 없는 색상 - {BOARD_COLORS} 중 하나 입력")
            continue

        try:
            count = fetch_stock(color)
        except Exception as e:
            print(f"재고 조회 실패: {e}")
            continue

        if count <= 0:
            print(f"[{color}] 재고 0 - 이동할 위치 없음")
            continue

        pose = compute_pick_pose(color, count)
        print(f"[{color}] 재고={count} -> 계산된 픽업 위치: {pose}")

        confirm = input("이 위치로 이동할까요? (y/n): ").strip().lower()
        if confirm != "y":
            print("취소")
            continue

        move_to(*pose)

    print("\n종료 중...")
    loop.run_until_complete(safe_call(arm_api.DisconnectDobot(portName=DOBOT_PORT)))
    print("연결 해제 완료")


if __name__ == "__main__":
    main()