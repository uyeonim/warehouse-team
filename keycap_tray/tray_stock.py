# -*- coding: utf-8 -*-
"""
빈 트레이 더미 재고 — 남은 개수로 다음에 집을 더미·층을 계산 (board/board_config.py compute_pick_pose 와 같은 방식)

배치: 더미 여러 개(pose_tray.json "stacks", 1·2·3 순서) × 한 더미 FLOORS(5)층
집는 순서: 층별로 — 맨 위층을 더미 1→2→3 으로 하나씩 걷고, 다 걷으면 다음 층
  예) 15개(3더미×5층): 15 → 더미1 5층, 14 → 더미2 5층, 13 → 더미3 5층, 12 → 더미1 4층 ... 1 → 더미3 1층
  그래서 사람이 채울 때도 이 순서대로 남아 있어야 함 (중간에 빼거나 끼우면 개수를 다시 맞출 것)
그리퍼에 센서가 없어서 "집기 자리 도착 + 그리퍼 명령 응답 + 벨트에 놓고 빠짐" 이면 하나 쓴 것으로 셈

pose_tray.json
  "stacks": [더미1, 더미2, 더미3]   더미 = 맨 위층 좌표 [x, y, z, r] 하나
                                   또는 위층부터 층마다 찍은 좌표 [[x,y,z,r] × FLOORS] (찍은 값 그대로 씀)
  "pitch": 28.9                    한 층 높이(mm). 좌표가 하나뿐인 더미의 아래층 z 계산용
                                   (2026-11-08 실측: 5층 z 104.6 ~ 1층 z -11 → 115.6 / 4 = 28.9)
  "travel_z": 125                  JUMP 로 옮길 때 이 높이까지 올라가서 이동 (집으러 갈 때·들고 갈 때·빠질 때 모두)
                                   기본 20mm 만 들면 벨트(낮은 곳)에서 출발할 때 5층 트레이 옆면을 침.
                                   150 은 329mm 벨트 자리 쪽에서 0x12 로 거부, 125 는 성공 (2026-11-08)

[11/09] 남은 개수의 원본은 키캡 Flask(5002)의 Redis stock:tray - 상태판에서 보고·채우고·맞춤
  - 집기 전에 GET /stock/tray 로 읽고, 놓은 뒤 POST /report/tray-used 로 하나 뺌 (덮어쓰지 않고 빼기)
  - Flask가 안 되면 runtime/tray_stock.json (마지막으로 안 값) 으로 계속 돌고, 못 보낸 개수는 모아 뒀다가 다음에 보냄
  - 0개면 팔을 움직이지 않고 TrayEmpty -> failed + reason "empty" 보고 -> 플랫폼 fault tray_load_failed
    사람이 다 쌓고 상태판에서 개수 입력 -> 관리자 페이지에서 fault 해제 -> 다시 집음
  python tray_stock.py          지금 개수·다음 집을 자리 보기
  python tray_stock.py full     다 채웠을 때 (더미 수 × 층 수)
  python tray_stock.py set 7    직접 맞추기 (상태판에서 해도 같음)
"""

import json
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
POSE_FILE = os.path.join(HERE, "pose_tray.json")
STOCK_FILE = os.path.join(HERE, "runtime", "tray_stock.json")
FLOORS = 5
DEFAULT_PITCH = 28.9


def _load_layout():
    """[[더미1 1~5층 좌표(위층부터)], ...] — stacks 가 없으면 [] (예전처럼 집기 좌표 하나만 씀)"""
    try:
        with open(POSE_FILE, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return []
    pitch = float(saved.get("pitch", DEFAULT_PITCH))
    stacks = []
    for stack in saved.get("stacks") or []:
        if stack and isinstance(stack[0], (list, tuple)):  # 층마다 찍은 좌표
            if len(stack) != FLOORS:
                raise ValueError(f"pose_tray.json stacks: 층마다 찍으려면 {FLOORS}개 (지금 {len(stack)}개)")
            stacks.append([tuple(map(float, p)) for p in stack])
        else:  # 맨 위층 하나 → 아래층은 pitch 만큼씩 낮게
            x, y, z, r = map(float, stack)
            stacks.append([(x, y, round(z - k * pitch, 1), r) for k in range(FLOORS)])
    return stacks


STACKS = _load_layout()
TOTAL = len(STACKS) * FLOORS


def travel_z():
    try:
        with open(POSE_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        value = saved.get("travel_z") or saved.get("carry_z")
        return float(value) if value else None
    except (OSError, ValueError, TypeError):
        return None


def enabled():
    return TOTAL > 0


def pick_pose(count):
    """남은 개수 count(1~TOTAL)일 때 집을 (더미 번호, 층, 좌표). 층 = 그 더미에 남은 개수 = 맨 위 트레이 층."""
    if not 1 <= count <= TOTAL:
        raise ValueError(f"트레이 개수 {count} 가 1~{TOTAL} 밖")
    emptied = TOTAL - count
    stack_no = emptied % len(STACKS)
    floor = FLOORS - emptied // len(STACKS)
    return stack_no + 1, floor, STACKS[stack_no][FLOORS - floor]


KEYCAP_FLASK_URL = os.environ.get("KEYCAP_FLASK_URL", "http://127.0.0.1:5002").rstrip("/")


class TrayEmpty(Exception):
    """빈 트레이 0개 - 팔을 움직이지 않고 실패(reason: empty)로 보고"""


def _read_file():
    try:
        with open(STOCK_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        return max(0, min(TOTAL, int(saved["count"]))), int(saved.get("unsent", 0))
    except (OSError, ValueError, KeyError, TypeError):
        return TOTAL, 0


def _write_file(count, unsent=0):
    os.makedirs(os.path.dirname(STOCK_FILE), exist_ok=True)
    tmp = STOCK_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"count": count, "total": TOTAL, "unsent": unsent,
                   "updated": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    os.replace(tmp, STOCK_FILE)  # 쓰다가 꺼져도 파일이 깨지지 않게


_warned = False


def _flask_down(error):
    global _warned
    if not _warned:
        print(f"!! 트레이 개수: 키캡 Flask({KEYCAP_FLASK_URL}) 안 됨 - 파일 값으로 계속, 다시 되면 맞춤: {error}")
        _warned = True


def get():
    """남은 개수. Flask(Redis) 값 - 못 보낸 사용 개수가 있으면 먼저 보냄. Flask가 안 되면 파일 값"""
    global _warned
    count, unsent = _read_file()
    try:
        if unsent:
            r = requests.post(f"{KEYCAP_FLASK_URL}/report/tray-used", json={"order_id": None, "n": unsent}, timeout=3)
            r.raise_for_status()
            _write_file(int(r.json()["count"]))   # 바로 지움 - 아래 읽기가 실패해도 두 번 빼지 않게
            print(f"트레이 개수: 못 보냈던 {unsent}개 사용 기록 보냄")
        r = requests.get(f"{KEYCAP_FLASK_URL}/stock/tray", timeout=3)
        r.raise_for_status()
        count = max(0, min(TOTAL, int(r.json()["count"])))
    except Exception as error:
        _flask_down(error)
        return count
    _warned = False
    _write_file(count)
    return count


def cached():
    """마지막으로 안 값 (네트워크 안 씀 - heartbeat 용)"""
    return _read_file()[0]


def used(order_id):
    """빈 트레이 하나를 벨트에 놓았음 -> Flask에서 하나 뺌. 못 보내면 파일에 모아 둠"""
    count, unsent = _read_file()
    count = max(0, count - 1)
    try:
        r = requests.post(f"{KEYCAP_FLASK_URL}/report/tray-used", json={"order_id": order_id, "n": unsent + 1}, timeout=3)
        r.raise_for_status()
        _write_file(int(r.json()["count"]))
    except Exception as error:
        _flask_down(error)
        _write_file(count, unsent + 1)


def put(count):
    """사람이 맞춤 (python tray_stock.py full / set N). Flask가 안 되면 파일만"""
    try:
        r = requests.post(f"{KEYCAP_FLASK_URL}/stock/set", json={"key": "tray", "count": count}, timeout=5)
        r.raise_for_status()
    except Exception as error:
        _flask_down(error)
    _write_file(count)


def describe(count=None):
    count = get() if count is None else count
    if not enabled():
        return "트레이 더미 설정 없음 (pose_tray.json stacks)"
    if count <= 0:
        return f"트레이 0/{TOTAL} — 다 씀. {TOTAL}개 다 쌓은 뒤 상태판에서 트레이 칸을 눌러 {TOTAL} 입력 (또는 python tray_stock.py full) → fault 해제"
    stack_no, floor, pose = pick_pose(count)
    return f"트레이 {count}/{TOTAL} 남음 — 다음: 더미{stack_no} {floor}층 {pose}"


if __name__ == "__main__":
    if not enabled():
        raise SystemExit(describe())
    args = sys.argv[1:]
    if args[:1] == ["full"]:
        put(TOTAL)
    elif args[:1] == ["set"] and len(args) == 2 and args[1].isdigit() and int(args[1]) <= TOTAL:
        put(int(args[1]))
    elif args:
        raise SystemExit(f"사용법: python tray_stock.py [full | set 0~{TOTAL}]")
    print(describe())
