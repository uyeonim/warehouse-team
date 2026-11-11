# -*- coding: utf-8 -*-
"""
저장된 좌표를 숫자로 조금씩 옮기기 (팔은 안 움직임, pose_<세트>.json 만 고침 - 저장 전 .bak-날짜 백업)

사용: python adjust_pose.py <set1|set2> <pickup|place> <슬롯 1|2> <항목> <+-값> [<항목> <+-값> ...] [--dry]
  항목   radius = 반경(팔 베이스에서 멀어지는 방향, mm).  + 는 바깥쪽, - 는 안쪽 (각도·z는 그대로)
         z      = 높이 (mm)
         x, y   = 그 축으로 (mm)
         r      = 좌표의 네 번째 값 (손목 회전, 도)
  --dry  파일에 안 쓰고 결과만 보여 줌

예)  python adjust_pose.py set1 pickup 2 radius +3         키캡1 집는 곳 슬롯2 를 바깥으로 3mm
     python adjust_pose.py set2 pickup 2 radius +5 z -1    키캡2 집는 곳 슬롯2 바깥 5mm, 1mm 아래로
     python adjust_pose.py set1 place 1 x -2 --dry         결과만 보기
기준: 반경 230mm 이상 330mm 이하 (그 밖이면 경고 - 위로 들 때 알람 0x12)
※ 게이트웨이가 켜져 있으면 끄고 다시 켜야 새 좌표가 적용됨 (창고 끄기 -> 창고 시작)
"""
import json
import math
import os
import shutil
import sys
import time

REACH_MIN, REACH_MAX = 230.0, 330.0
KIND = {"pickup": "pickup_slots", "place": "place_slots"}


def die(msg):
    print(msg)
    sys.exit(1)


def main():
    args = [a for a in sys.argv[1:] if a != "--dry"]
    dry = "--dry" in sys.argv[1:]
    if len(args) < 5 or len(args) % 2 == 0:   # 세트 종류 슬롯 + (항목 값) 쌍들
        die(__doc__)
    set_key, kind, slot, *ops = args
    if kind not in KIND or slot not in ("1", "2"):
        die("pickup/place 와 슬롯 1/2 를 확인하세요.")
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"pose_{set_key}.json")
    if not os.path.isfile(path):
        die(f"파일이 없음: {path}")
    data = json.load(open(path, encoding="utf-8"))
    pose = data[KIND[kind]][slot]
    old = list(pose)
    x, y, z, r = pose[:4]
    for name, raw in zip(ops[0::2], ops[1::2]):
        try:
            v = float(raw)
        except ValueError:
            die(f"값이 숫자가 아님: {raw}")
        if name == "radius":
            rad = math.hypot(x, y)
            if rad == 0:
                die("반경이 0 이라 못 옮김")
            k = (rad + v) / rad
            x, y = x * k, y * k
        elif name == "z":
            z += v
        elif name == "x":
            x += v
        elif name == "y":
            y += v
        elif name == "r":
            r += v
        else:
            die(f"항목은 radius / z / x / y / r 중 하나: {name}")
    new = [round(x, 1), round(y, 1), round(z, 1), round(r, 1)]
    rad_old, rad_new = math.hypot(old[0], old[1]), math.hypot(new[0], new[1])
    print(f"{set_key} {kind} 슬롯{slot}")
    print(f"  이전 {old}  반경 {rad_old:.1f}mm")
    print(f"  이후 {new}  반경 {rad_new:.1f}mm  (이동 {math.hypot(new[0] - old[0], new[1] - old[1]):.1f}mm)")
    if not REACH_MIN <= rad_new <= REACH_MAX:
        print(f"  !! 반경 {rad_new:.1f}mm 가 {REACH_MIN:g}~{REACH_MAX:g}mm 밖 - 알람 0x12 가 날 수 있음")
    if dry:
        print("  (--dry: 저장 안 함)")
        return
    data[KIND[kind]][slot] = new
    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, backup)
    json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"  저장함 (백업: {os.path.basename(backup)})")


if __name__ == "__main__":
    main()
