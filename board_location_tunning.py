import asyncio
import time

from DobotRPC import RPCClient, DobotlinkAdapter

DOBOT_PORT = "COM3"

# main_board_robot_arm.py와 동일한 값 - PICK_POSE_ROWS 바뀌면 여기도 맞춰서 갱신
PICK_POSE_ROWS = {
    "red":    [(229.4263, 190, -38, 41), (255.1454, 190, -38, 38),
               (290.2512, 190, -38, 35), (320.1271, 165.9958, -37.1079, 27.3997)],
    "green":  [(240.0187, 85, -38, 20), (267.8990, 85, -38, 20),
               (296.3269, 85, -38, 20), (326.5881, 66.0017, -38, 20)],
    "blue":   [(245.9397, -0.5, -38, 0.5), (271.3919, -0.5, -38, 0.5),
               (298.0619, -0.5, -38, 0.5), (325.5233, -28.8626, -38, 0.5)],
    "yellow": [(253.0182, -100, -38, -20), (279.8343, -100, -38, -18),
               (303.9048, -100, -38, -16), (332.5840, -121.5756, -37.2979, -20.0798)],
}
BOARD_THICKNESS_MM = 20.0
BOARD_COLORS = ["red", "yellow", "blue", "green"]
NUM_ROWS = 4          # 0=제일 가까운 줄 ~ 3=제일 먼 줄
ROW_CAPACITY = 5       # 줄 하나당 최대 적재 개수 (입력값 범위 체크용)

HOME_POSE = (239.9906, -5.5209, 84.0930, 0.7622)  # main_board_robot_arm.py와 동일값
# ⚠️ 2026-09-19: HOME_POSE의 실제 joint값이 [~0,-0.33,-1.18,~0]로 사실상
# joint 전부 0인 특이 자세와 거의 같아서 웜업 용도로 못 씀 - 확실히 다른
# joint값이 나오는 지점(green row2/3에서 실제로 통했던 값)을 별도로 둠.
WARMUP_POSE = (296.3269, 85.0, 2.0, 20.0)

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


def compute_pick_pose(color, row, count_in_row):
    x, y, base_z, r = PICK_POSE_ROWS[color][row]
    pick_z = base_z + (count_in_row - 1) * BOARD_THICKNESS_MM
    return (x, y, pick_z, r)


def parse_row_count(raw):
    """'0/4' -> (row=0, count_in_row=4). 형식/범위 안 맞으면 None 반환."""
    parts = raw.strip().split("/")
    if len(parts) != 2:
        return None
    try:
        row = int(parts[0])
        count_in_row = int(parts[1])
    except ValueError:
        return None
    if not (0 <= row < NUM_ROWS):
        print(f"줄 번호는 0~{NUM_ROWS - 1} 범위")
        return None
    if not (1 <= count_in_row <= ROW_CAPACITY):
        print(f"개수는 1~{ROW_CAPACITY} 범위")
        return None
    return row, count_in_row


def main():
    connect_res = loop.run_until_complete(safe_call(arm_api.ConnectDobot(portName=DOBOT_PORT)))
    print("연결 결과:", connect_res)
    print("\n현재 위치:", get_pose())

    # ⚠️ 2026-09-19: joint 전부 0인(또는 그에 준하는) 특이 자세에서 멀리 있는
    # 목표(특히 Y/R이 큰 red 뒤쪽 줄)로 바로 이동하면 명령은 True로 접수되는데
    # 실제로는 전혀 안 움직이는 문제 확인됨 - 확실히 다른 joint값이 나오는
    # WARMUP_POSE를 한 번 거친 뒤에는 정상 도달함(joint2 리밋 근처 경로를 못
    # 찾는 것으로 추정, 01_보드_로봇암.md의 "joint2 negative limit 알람" 사례와
    # 결이 비슷함). 그래서 HOME -> WARMUP -> 실제 목표 순서로 항상 거치게 함.
    print(f"\nHOME_POSE{HOME_POSE}로 이동")
    move_to(*HOME_POSE)
    print(f"웜업 이동 - WARMUP_POSE{WARMUP_POSE}로 한 번 거침 (특이 자세 탈출용)")
    move_to(*WARMUP_POSE)

    print(f"\n색깔 입력 (red/yellow/blue/green), 'home'이면 웜업 거쳐서 HOME 복귀, 'q'로 종료")
    print(f"줄/개수는 '0/4' 형식 - 줄 0(제일 가까움)~{NUM_ROWS - 1}(제일 멈), "
          f"개수는 그 줄에 몇 개 쌓여있는지(1~{ROW_CAPACITY})")
    print("⚠️ 이번엔 매 이동마다 자동으로 웜업/홈 거치지 않음 - 직전 위치에서 바로 다음 목표로 이동함")
    while True:
        color = input("\n색깔: ").strip().lower()
        if color == "q":
            break
        if color == "home":
            print(f"웜업 경유 - WARMUP_POSE{WARMUP_POSE}")
            move_to(*WARMUP_POSE)
            move_to(*HOME_POSE)
            continue
        if color not in BOARD_COLORS:
            print(f"알 수 없는 색상 - {BOARD_COLORS} 중 하나 입력")
            continue

        raw = input("줄/개수 (예: 0/4): ").strip()
        parsed = parse_row_count(raw)
        if parsed is None:
            print("형식이 안 맞음 - '줄/개수' 형식으로 다시 입력 (예: 0/4)")
            continue
        row, count_in_row = parsed

        pose = compute_pick_pose(color, row, count_in_row)
        print(f"[{color}] 줄{row}/{count_in_row}개 -> 계산된 픽업 위치: {pose}")

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