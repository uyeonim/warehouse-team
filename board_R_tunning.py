import asyncio
import time

from DobotRPC import RPCClient, DobotlinkAdapter

DOBOT_PORT = "COM17"

# main_board_robot_arm.py와 동일한 값 - PICK_POSE_ROWS/PLACE_POSE 바뀌면 여기도 맞춰서 갱신
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

PLACE_POSE = (-152.0347, -263.0365, 0.4450, -120.0278)
SAFE_LIFT_MM = 40.0

HOME_POSE = (239.9906, -5.5209, 84.0930, 0.7622)  # main_board_robot_arm.py와 동일값
# ⚠️ 2026-09-19: HOME_POSE의 실제 joint값이 [~0,-0.33,-1.18,~0]로 사실상
# joint 전부 0인 특이 자세와 거의 같아서 웜업 용도로 못 씀(board_location_tunning.py
# 에서 확인된 문제 그대로 적용) - 확실히 다른 joint값이 나오는 지점을 따로 둠.
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


def move_to(x, y, z, r, wait_sec=3.0):
    res = loop.run_until_complete(safe_call(
        arm_api.SetPTPCmd(portName=DOBOT_PORT, ptpMode=0, x=x, y=y, z=z, r=r, isQueued=False)
    ))
    print("이동 명령 결과:", res)
    time.sleep(wait_sec)
    print("이동 후 실제 위치:", get_pose())


def suck(on):
    res = loop.run_until_complete(safe_call(
        arm_api.SetEndEffectorSuctionCup(portName=DOBOT_PORT, enable=True, on=on, isQueued=False)
    ))
    print(f"흡착 {'ON' if on else 'OFF'} 결과:", res)


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

    # board_location_tunning.py와 동일 이유 - joint 전부 0(또는 그에 준하는)
    # 특이 자세에서 멀리 있는 목표로 바로 가면 명령은 접수되는데 실제로는
    # 안 움직이는 문제 대응.
    print(f"\nHOME_POSE{HOME_POSE}로 이동")
    move_to(*HOME_POSE)
    print(f"웜업 이동 - WARMUP_POSE{WARMUP_POSE}로 한 번 거침 (특이 자세 탈출용)")
    move_to(*WARMUP_POSE)

    color = input("\n픽업할 색깔 (red/yellow/blue/green): ").strip().lower()
    if color not in BOARD_COLORS:
        print(f"알 수 없는 색상 - {BOARD_COLORS} 중 하나 입력")
        return

    raw = input(f"줄/개수 (예: 0/4, 줄 0~{NUM_ROWS - 1}, 개수 1~{ROW_CAPACITY}): ").strip()
    parsed = parse_row_count(raw)
    if parsed is None:
        print("형식이 안 맞음")
        return
    row, count_in_row = parsed

    pick_x, pick_y, pick_z, pick_r = compute_pick_pose(color, row, count_in_row)
    place_x, place_y, place_z, place_r = PLACE_POSE
    travel_z = max(pick_z, place_z) + SAFE_LIFT_MM

    print(f"\n[{color}] 줄{row}/{count_in_row}개 -> 픽업 위치: ({pick_x:.1f},{pick_y:.1f},{pick_z:.1f},{pick_r:.1f})")
    confirm = input("이 위치로 이동할까요? (y/n): ").strip().lower()
    if confirm != "y":
        print("취소")
        loop.run_until_complete(safe_call(arm_api.DisconnectDobot(portName=DOBOT_PORT)))
        return
    move_to(pick_x, pick_y, pick_z, pick_r)

    suck(True)
    time.sleep(1.0)

    move_to(pick_x, pick_y, travel_z, pick_r)

    # ⚠️ 2026-09-19: red row3처럼 도달거리가 먼 픽업 위치에서 PLACE_POSE(반대쪽)로
    # 바로 이동하면 명령은 True로 접수되는데 실제로는 전혀 안 움직이는 문제
    # 확인됨(board_location_tunning.py에서 확인된 것과 같은 패턴) - 중간에
    # WARMUP_POSE의 X,Y를 한 번 거쳐서 극단적인 이동을 두 구간으로 나눔.
    print(f"\n웜업 경유 - WARMUP_POSE X,Y({WARMUP_POSE[0]:.1f},{WARMUP_POSE[1]:.1f})")
    move_to(WARMUP_POSE[0], WARMUP_POSE[1], travel_z, pick_r)

    print(f"\nPLACE_POSE X,Y({place_x:.1f},{place_y:.1f})까지만 이동 - 놓기 직전(z={place_z:.1f})까지는 안 내려감, "
          f"안전 높이({travel_z:.1f})에서 멈춤.")
    confirm = input("트레이 위치로 이동할까요? (y/n): ").strip().lower()
    if confirm != "y":
        print("취소 - 흡착 끄고 종료")
        suck(False)
        loop.run_until_complete(safe_call(arm_api.DisconnectDobot(portName=DOBOT_PORT)))
        return
    move_to(place_x, place_y, travel_z, pick_r)

    current_r = pick_r
    step = 1.0  # 기본 스텝(도) - "step 0.5" 입력으로 변경 가능
    print("\n이제부터 R값만 바꿔가며 방향 튜닝 (X,Y,Z는 고정, R만 제자리 회전).")
    print("  +        -> R을 현재 스텝만큼 증가")
    print("  -        -> R을 현재 스텝만큼 감소")
    print("  +5 / -5  -> 그 숫자만큼만 1회 증가/감소 (스텝 값은 안 바뀜)")
    print("  step 0.5 -> 이후 +/- 의 기본 스텝을 0.5도로 변경")
    print("  31.9158  -> 그 값으로 절대 이동")
    print("  release  -> 흡착 끄고 종료 (이 높이에서 바로 떨어짐, 주의)")
    while True:
        cmd = input(f"\n현재 R={current_r:.4f} (스텝={step}) / 명령 입력: ").strip().lower()

        if cmd == "release":
            confirm = input(f"정말 여기(z={travel_z:.1f}, 안전 높이)에서 흡착을 끌까요? "
                             f"보드가 이 높이에서 떨어집니다. (y/n): ").strip().lower()
            if confirm == "y":
                break
            print("취소 - R 튜닝 계속")
            continue

        if cmd.startswith("step"):
            parts = cmd.split()
            if len(parts) == 2:
                try:
                    step = float(parts[1])
                    print(f"스텝을 {step}도로 변경")
                except ValueError:
                    print("사용법: step 0.5")
            else:
                print("사용법: step 0.5")
            continue

        if cmd == "+":
            new_r = current_r + step
        elif cmd == "-":
            new_r = current_r - step
        elif cmd.startswith("+") or cmd.startswith("-"):
            # "+5" / "-5" - 그 값만큼 1회 가감 (스텝 자체는 안 바뀜)
            try:
                delta = float(cmd)
            except ValueError:
                print("숫자, +, -, +숫자, -숫자, 'step 숫자', 'release' 중 하나만 입력 가능")
                continue
            new_r = current_r + delta
        else:
            try:
                new_r = float(cmd)
            except ValueError:
                print("숫자, +, -, +숫자, -숫자, 'step 숫자', 'release' 중 하나만 입력 가능")
                continue

        move_to(place_x, place_y, travel_z, new_r)
        current_r = new_r

    suck(False)
    print(f"\n최종 R값: {current_r:.4f}  -> PLACE_POSE의 R 자리에 이 값 반영할 것")

    print("\n종료 중...")
    loop.run_until_complete(safe_call(arm_api.DisconnectDobot(portName=DOBOT_PORT)))
    print("연결 해제 완료")


if __name__ == "__main__":
    main()