# -*- coding: utf-8 -*-
import json, os, sys
d = sys.argv[1]
def load(n): return json.load(open(os.path.join(d, f"r_{n}.json"), encoding="utf-8"))
def merge(names):
    tot = {"orders": 0, "results": {}, "incidents": {}, "faults": {}, "errors": 0}
    for n in names:
        r = load(n); tot["orders"] += r["orders"]; tot["errors"] += len(r["errors"])
        for k in ("results", "incidents", "faults"):
            for a, b in r[k].items(): tot[k][a] = tot[k].get(a, 0) + b
    return tot
groups = json.loads(sys.argv[2])
for g, names in groups.items():
    t = merge(names)
    print(f"\n### {g}: 주문 {t['orders']}, 시뮬 오류 {t['errors']}")
    print("  결과:", t["results"]); print("  실물 사고:", t["incidents"]); print("  fault:", t["faults"])
for n in sys.argv[3:]:
    r = load(n)
    print(f"\n{n}: 단계 {r['stage_sec(min/avg/max)']}\n   팔 {r['arm_sec(min/avg/max)']} 놓기→벨트출발 {r['tray_release_to_belt_start_sec(min/avg/max)']} 가상 {r['virtual_minutes']}분")
    if r["bad_orders"]: print("   나쁜 주문 예:", r["bad_orders"][:4])
