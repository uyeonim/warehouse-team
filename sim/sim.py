# -*- coding: utf-8 -*-
"""
창고 전체 흐름 가상 시뮬레이션 (로봇·네트워크·실제 시간 없이)

진짜 코드 그대로 실행:  keycap_tray/warehouse_server.py + dobot_control.py + tray_stock.py + local_transport.py + local_feed.py
                        board/main_board_robot_arm.py(리허설 장비) + board/app.py(Flask, 테스트 클라이언트)
가짜로 만든 것:         플랫폼(cnt_process 전이 규칙), Mobius, 키캡 배출(아두이노), 키캡 Flask의 트레이 재고,
                        게이트웨이 쪽 로봇팔(DobotLink), Redis, 벨트 위 실물(트레이·키캡·보드), 운영자

플랫폼 규칙 근거: COSS 로직 명세의 콜백 표(보고 -> 전이·fault)와 단계 제한 60초,
                 2026-10-08 실제 cnt_process 기록 (status=keycap_dispensed 에 fault=tray_load_failed 가 붙어 있었음)
가정(실물과 다를 수 있음): section0 보고는 플랫폼이 쓰지 않음 / fault 중 로봇팔·벨트 보고는 버림, 배출 보고는 받음 /
                 stage 타이머는 fault 동안 멈추고 해제 때 다시 시작 /
                 로봇 이동 시간 = 0.5 + 수직거리/92 + 2*sqrt(회전각/300) 초
                 (11/08 실측에 맞춤: 트레이 팔 14.4~18.4초, 키캡 4개 32.9초)

실행 (저장소 맨 위 폴더에서):
  venv\\Scripts\\python.exe sim\\sim.py --orders 100 --seed 1
  방해 넣기: --noise 0.02 (이동 명령이 무시될 확률) --hardfail 0.1 (팔이 끝내 못 가는 주문 비율)
             --forget 0.5 (운영자가 트레이 보충을 잊을 확률)
  그 밖:     --dispense 7,13 (키캡 배출 시간 범위) --gap 10 (주문 사이 쉬는 시간) --empty_reset 1
             --log 파일 (프로그램 로그) --out 파일 (결과 JSON)
진짜 .env.local, 트레이 개수 파일, 네트워크, 로봇은 쓰지 않음 (requests 를 통째로 가짜로 바꾸고 설정은 sim/sim.env)
"""
import argparse, asyncio, copy, json, math, os, queue, random, selectors, sys, threading, time as _rt, traceback

ap = argparse.ArgumentParser()
ap.add_argument("--code", default=None, help="keycap_tray·board 폴더가 있는 곳 (기본: 이 저장소)")
ap.add_argument("--orders", type=int, default=100)
ap.add_argument("--seed", type=int, default=1)
ap.add_argument("--noise", type=float, default=0.0, help="이동 명령이 무시될 확률(알람 0x12)")
ap.add_argument("--hardfail", type=float, default=0.0, help="팔이 끝내 못 가는 확률(주문당)")
ap.add_argument("--forget", type=float, default=0.0, help="운영자가 트레이를 미리 안 채울 확률")
ap.add_argument("--empty_reset", type=int, default=0, help="1: 주문 목록이 빈 스냅샷을 키캡 Flask가 하드리셋으로 보고 트레이 개수를 15로 (main 코드의 규칙)")
ap.add_argument("--dispense", default="7,13", help="키캡 4개 배출이 끝나는 시간 범위(초)")
ap.add_argument("--gap", type=float, default=0.0, help=">0: 앞 주문이 완전히 끝나고 이만큼 쉰 뒤 다음 주문 (주문 목록이 비는 순간이 생김)")
ap.add_argument("--log", default=None)
ap.add_argument("--out", default=None)
args = ap.parse_args()

REAL_OUT = sys.stdout
C0, VZ, ACC = float(os.environ.get("SIM_C0", 0.5)), float(os.environ.get("SIM_VZ", 92)), float(os.environ.get("SIM_ACC", 300))   # 이동 시간 = C0 + 수직거리/VZ + 2*sqrt(회전각/ACC)
STAGE_LIMIT = 60.0
DISPENSE = tuple(float(v) for v in args.dispense.split(","))
BASE_EPOCH = 1_800_000_000.0


# ============================== 가상 시계 ==============================
class Clock:
    def __init__(self):
        self.now, self.cv, self.sleepers, self.n = 0.0, threading.Condition(), {}, 0

    def register(self):
        with self.cv:
            self.n += 1

    def unregister(self):
        with self.cv:
            self.n -= 1
            self._advance()

    def _advance(self):
        if self.sleepers and len(self.sleepers) >= self.n:
            self.now = max(self.now, min(self.sleepers.values()))
            self.cv.notify_all()

    def sleep(self, dt):
        tid = threading.get_ident()
        with self.cv:
            wake = self.now + max(0.0, dt)
            self.sleepers[tid] = wake
            self._advance()
            while self.now < wake:
                if not self.cv.wait(timeout=60):
                    raise RuntimeError("시뮬레이션 교착 (어떤 스레드가 실제 시간으로 멈춰 있음)")
            del self.sleepers[tid]
            self._advance()


CLOCK = Clock()


class VTime:
    def time(self): return BASE_EPOCH + CLOCK.now
    def monotonic(self): return CLOCK.now
    def sleep(self, dt): CLOCK.sleep(dt)
    def strftime(self, fmt, *a): return _rt.strftime(fmt, _rt.localtime(BASE_EPOCH + CLOCK.now))
    def __getattr__(self, name): return getattr(_rt, name)


class VSel:
    def __init__(self): self._s = selectors.DefaultSelector()
    def select(self, timeout=None):
        if timeout is None:
            timeout = 0.5
        if timeout > 0:
            CLOCK.sleep(timeout)
        return self._s.select(0)
    def __getattr__(self, name): return getattr(self._s, name)


class VLoop(asyncio.SelectorEventLoop):
    def __init__(self): super().__init__(VSel())
    def time(self): return CLOCK.now


class VPolicy(asyncio.WindowsSelectorEventLoopPolicy):
    _loop_factory = VLoop


asyncio.set_event_loop_policy(VPolicy())


# ============================== 벨트 위 실물 ==============================
ST_LOAD, ST_KEYCAP, ST_BOARD, ST_PICKUP = 0.0, 215.0, 870.0, 1125.0   # 50mm/s × 4.3 / 13.1 / 5.1초
TOL = 60.0


class World:
    def __init__(self):
        self.lock = threading.RLock()
        self.stacks = [5, 5, 5]
        self.trays = []            # {"x", "order", "keycaps", "board", "placed"}
        self.incidents = []        # (t, order, kind)
        self.belt_on = None
        self.tray_hold = False
        self.zone = (None, None)   # 트레이 팔이 벨트 투입 자리에 있는 구간
        self.kc_hold = {}
        self.kc_ready = set()      # 키캡 벨트가 돈 주문
        self.order = None
        self.marks = {}            # order -> {"tray_released": t, "belt0_start": t, ...}
        self.roles = {}

    def inc(self, kind):
        self.incidents.append((round(CLOCK.now, 1), self.order, kind))

    def mark(self, key):
        self.marks.setdefault(self.order, {}).setdefault(key, round(CLOCK.now, 2))

    def tray_at(self, x):
        return next((t for t in self.trays if abs(t["x"] - x) <= TOL), None)

    # ---- 팔 ----
    def arm_move(self, port, p0, p1, t0, t1):
        with self.lock:
            if self.roles.get(port) != "tray":
                return
            place = TRAY["place_slots"][1]
            if math.hypot(p1[0] - place[0], p1[1] - place[1]) < 25 and abs(p1[2] - place[2]) < 10:
                self.zone = (t1 - 1.2, None)
            elif math.hypot(p0[0] - place[0], p0[1] - place[1]) < 25 and self.zone[0] is not None and self.zone[1] is None:
                self.zone = (self.zone[0], t0 + 1.2)

    def gripper(self, port, closing, pose):
        with self.lock:
            role = self.roles.get(port)
            if role == "tray":
                self._tray_gripper(closing, pose)
            elif role in ("set1", "set2"):
                self._keycap_gripper(role, port, closing, pose)

    def _tray_gripper(self, closing, pose):
        place = TRAY["place_slots"][1]
        if closing:
            for i, stack in enumerate(STACKS):
                if math.hypot(pose[0] - stack[0][0], pose[1] - stack[0][1]) < 25:
                    floor = next((len(stack) - k for k, p in enumerate(stack) if abs(pose[2] - p[2]) < 8), None)
                    if floor is None:
                        return
                    top = self.stacks[i]
                    if floor == top:
                        self.stacks[i] -= 1
                        self.tray_hold = True
                    elif floor < top:
                        self.inc("트레이 더미에 박음(개수 불일치)")
                    else:
                        self.inc("빈 곳을 집음(트레이 없음)")
                    return
        else:
            if not self.tray_hold:
                return
            self.tray_hold = False
            if math.hypot(pose[0] - place[0], pose[1] - place[1]) < 25 and abs(pose[2] - place[2]) < 10:
                if self.belt_on is not None:
                    self.inc("벨트가 도는 중에 트레이를 놓음")
                if self.tray_at(ST_LOAD):
                    self.inc("투입 자리에 트레이가 두 장")
                self.trays.append({"x": ST_LOAD, "order": self.order, "keycaps": 0, "board": False, "placed": CLOCK.now})
                self.mark("tray_released")
            else:
                for i, stack in enumerate(STACKS):   # 되돌려 놓음
                    if math.hypot(pose[0] - stack[0][0], pose[1] - stack[0][1]) < 25:
                        self.stacks[i] += 1

    def _keycap_gripper(self, role, port, closing, pose):
        cfg = SETS[role]
        if closing:
            if any(math.hypot(pose[0] - p[0], pose[1] - p[1]) < 15 for p in cfg["pickup_slots"].values()):
                self.kc_hold[port] = self.order in self.kc_ready
                if not self.kc_hold[port]:
                    self.inc("키캡 없는 자리를 집음")
        elif self.kc_hold.pop(port, False):
            if any(math.hypot(pose[0] - p[0], pose[1] - p[1]) < 15 for p in cfg["place_slots"].values()):
                tray = self.tray_at(ST_KEYCAP)
                if tray:
                    tray["keycaps"] += 1
                else:
                    self.inc("트레이 없는 자리에 키캡을 놓음")

    def keycap_belt(self, port, enable):
        with self.lock:
            if not enable:
                self.kc_ready.add(self.order)

    # ---- 트레이 벨트 (보드 main) ----
    def tray_belt(self, enable):
        with self.lock:
            now = CLOCK.now
            if enable and self.belt_on is None:
                self.belt_on = now
                self.mark("belt0_start")
                z0, z1 = self.zone
                if z0 is not None and z0 <= now and (z1 is None or now <= z1):
                    self.inc("트레이 팔이 놓는 중에 벨트 출발")
                if not self.trays:
                    self.inc("트레이 없이 벨트 구동")
            elif not enable and self.belt_on is not None:
                for t in self.trays:
                    t["x"] += 50.0 * (now - max(self.belt_on, t["placed"]))
                self.belt_on = None

    def board_placed(self):
        with self.lock:
            tray = self.tray_at(ST_BOARD)
            if tray:
                tray["board"] = True
            else:
                self.inc("트레이 없는 자리에 보드를 놓음")

    def pickup(self):
        with self.lock:
            tray = self.tray_at(ST_PICKUP)
            if tray is None:
                return "픽업대에 트레이 없음"
            self.trays.remove(tray)
            if tray["keycaps"] == 4 and tray["board"]:
                return "정상"
            return f"불완전 (키캡 {tray['keycaps']}/4, 보드 {'있음' if tray['board'] else '없음'})"

    def clear_line(self):
        with self.lock:
            self.trays.clear()
            self.kc_hold.clear()


WORLD = World()


# ============================== 가짜 Redis / HTTP ==============================
class FakeRedis:
    def __init__(self): self.d, self.lock = {}, threading.RLock()
    def get(self, k):
        with self.lock: return self.d.get(k)
    def set(self, k, v, ex=None):
        with self.lock: self.d[k] = str(v); return True
    def mget(self, *keys):
        with self.lock: return [self.d.get(k) for k in keys]
    def decrby(self, k, n):
        with self.lock: self.d[k] = str(int(self.d.get(k, 0)) - n); return int(self.d[k])
    def delete(self, *keys):
        with self.lock:
            for k in keys: self.d.pop(k, None)
    def lrange(self, k, a, b):
        with self.lock: return list(self.d.get(k, []))
    def rpush(self, k, v):
        with self.lock: self.d.setdefault(k, []).append(v)
    def lrem(self, k, n, v):
        with self.lock:
            if v in self.d.get(k, []): self.d[k].remove(v)


class Resp:
    def __init__(self, status, data=None):
        self.status_code, self._d, self.text = status, data, json.dumps(data, ensure_ascii=False) if data is not None else ""
    def json(self): return self._d
    def raise_for_status(self):
        if not 200 <= self.status_code < 300: raise RuntimeError(f"HTTP {self.status_code}")


TRAY_STOCK = {"count": 15}
TRAY_TOTAL = 15


def http(method, url, json_body=None):
    CLOCK.sleep(0.02)
    if url.startswith("http://board.sim"):
        r = BOARD_APP.test_client().open(url[len("http://board.sim"):], method=method, json=json_body)
        return Resp(r.status_code, r.get_json(silent=True))
    if url.startswith("http://kflask.sim"):
        path = url[len("http://kflask.sim"):]
        if path == "/stock/tray":
            return Resp(200, {"count": TRAY_STOCK["count"], "total": TRAY_TOTAL})
        if path == "/report/tray-used":
            TRAY_STOCK["count"] = max(0, TRAY_STOCK["count"] - int(json_body["n"]))
            PLATFORM.store("cnt_warehouse/cnt_tray_status", {"order_id": json_body.get("order_id"), "count": TRAY_STOCK["count"]})
            return Resp(200, {"result": "ok", "count": TRAY_STOCK["count"]})
        if path == "/stock/set":
            TRAY_STOCK["count"] = int(json_body["count"])
            return Resp(200, {"result": "ok"})
        return Resp(404)
    if url.startswith("http://mobius.sim/Mobius/ae/"):
        path = url[len("http://mobius.sim/Mobius/ae/"):]
        path, _, query = path.partition("?")
        if method == "POST":
            con = json_body["m2m:cin"]["con"]
            PLATFORM.create_cin(path.strip("/"), json.loads(con) if isinstance(con, str) else con)
            return Resp(201, {})
        if path.endswith("/la"):
            con = PLATFORM.latest(path[:-3].strip("/"))
            return Resp(200, {"m2m:cin": {"con": con}}) if con is not None else Resp(404)
        return Resp(200, {"m2m:rsp": {"m2m:cin": [{"con": c} for c in PLATFORM.all(path.strip("/"))]}})
    return Resp(404)


# ============================== 가짜 플랫폼 ==============================
NEXT = {"assigned": "keycap_dispensed", "keycap_dispensed": "keycap_reached", "keycap_reached": "keycap_packed",
        "keycap_packed": "tray_reached", "tray_reached": "board_packed", "board_packed": "pickup_reached"}
WAREHOUSE_STAGES = set(NEXT) | {"board_mismatched"}
COLOR_CODE = {"red": "r", "yellow": "y", "blue": "b", "green": "g"}


class Platform:
    def __init__(self):
        self.lock = threading.RLock()
        self.seq, self.orders, self.cins, self.subs = 0, {}, {}, []
        self.acc = {}       # order -> 누적 보고
        self.stage_t = {}   # order -> 단계 시작 시각 (fault 동안 None)
        self.hist = {}      # order -> 기록
        self.reason = {}

    def store(self, path, con):
        with self.lock:
            self.cins.setdefault(path, []).append(con)

    def latest(self, path):
        with self.lock:
            if path == "cnt_process":
                return self._snap()
            items = self.cins.get(path)
            return items[-1] if items else None

    def all(self, path):
        with self.lock:
            return list(self.cins.get(path, []))

    def _snap(self):
        return {"seq": self.seq, "updated_at": "sim", "orders": copy.deepcopy(self.orders)}

    def _push(self):
        self.seq += 1
        if args.empty_reset and not self.orders:
            TRAY_STOCK["count"] = 15    # keycap_flask/app.py _is_platform_reset: orders == {} 이고 restock 목록이 없으면 하드리셋
        snap = self._snap()
        for deliver in self.subs:
            deliver(copy.deepcopy(snap))

    def _h(self, oid):
        return self.hist.setdefault(oid, {"faults": [], "stages": {}, "result": None})

    def new_order(self, oid, keycap, colors, board):
        with self.lock:
            self.orders[oid] = {"station_id": "1", "order_seq": len(self.hist) + 1, "status": "assigned", "board": board,
                                "keycap": keycap, "colors": colors, "fault": None, "fault_section": None}
            self.acc[oid] = {"dispense": set(), "arm": set()}
            self.stage_t[oid] = CLOCK.now
            self._h(oid)["stages"]["assigned"] = [round(CLOCK.now, 1), None]
            WORLD.order = oid
            self._push()

    def _to(self, oid, status):
        o = self.orders[oid]
        h = self._h(oid)
        if o["status"] in h["stages"]:
            h["stages"][o["status"]][1] = round(CLOCK.now, 1)
        o["status"] = status
        h["stages"][status] = [round(CLOCK.now, 1), None]
        self.stage_t[oid] = CLOCK.now
        self.acc[oid]["arm"] = set()
        self._push()

    def _fault(self, oid, fault, section):
        o = self.orders[oid]
        o["fault"], o["fault_section"] = fault, section
        self.stage_t[oid] = None
        self._h(oid)["faults"].append((round(CLOCK.now, 1), o["status"], fault))
        self.acc[oid]["arm"] = set()
        self._push()

    def clear_fault(self, oid):
        with self.lock:
            o = self.orders.get(oid)
            if o and o["fault"]:
                o["fault"], o["fault_section"] = None, None
                self.stage_t[oid] = CLOCK.now
                self._push()

    def discard(self, oid, why):
        with self.lock:
            if oid in self.orders:
                self._h(oid)["result"] = why
                del self.orders[oid]
                self._push()

    def complete(self, oid, result):
        with self.lock:
            self._h(oid)["result"] = result
            self.orders.pop(oid, None)
            self._push()

    def create_cin(self, path, con):
        with self.lock:
            self.store(path, con)
            oid = con.get("order_id")
            o = self.orders.get(oid)
            if o is None:
                return
            name, st = path.rsplit("/", 1)[-1], o["status"]
            if name == "cnt_keycap_dispense":      # 배출 보고는 fault 중에도 받음 (10/8 기록: fault 가 걸린 채 keycap_dispensed)
                self.acc[oid]["dispense"].add(con.get("keycap"))
                if st == "assigned" and len(self.acc[oid]["dispense"]) >= 4:
                    self._to(oid, "keycap_dispensed")
                return
            if name == "cnt_tray_robot_arm":
                if con.get("status") == "failed" and not o["fault"]:
                    self.reason[oid] = con.get("reason")
                    self._fault(oid, "tray_load_failed", "tray_robot_arm")
                return
            if o["fault"]:
                return                              # fault 중 로봇팔·벨트 보고는 버림
            if name == "cnt_keycap_conveyor" and st == "keycap_dispensed":
                vals = [con.get(f"keycap{i}") for i in range(1, 5)]
                if "failed" in vals:
                    self._fault(oid, f"keycap_{con.get('section', '01')}_belt_jammed", "keycap_conveyor")
                elif all(v == "done" for v in vals):
                    self._to(oid, "keycap_reached")
            elif name == "cnt_keycap_robot_arm" and st == "keycap_reached":
                for i in range(1, 5):
                    v = con.get(f"keycap{i}")
                    if v == "failed":
                        self._fault(oid, "keycap_pack_failed", "keycap_robot_arm")
                        return
                    if v == "done":
                        self.acc[oid]["arm"].add(i)
                if len(self.acc[oid]["arm"]) >= 4:
                    self._to(oid, "keycap_packed")
            elif name == "cnt_tray_conveyor":
                if "section1" in con and st == "keycap_packed":
                    self._to(oid, "tray_reached") if con["section1"] == "done" else self._fault(oid, "tray_01_belt_jammed", "tray_conveyor")
                elif "section2" in con and st == "board_packed":
                    self._to(oid, "pickup_reached") if con["section2"] == "done" else self._fault(oid, "tray_02_belt_jammed", "tray_conveyor")
                # section0 보고는 전이에 쓰지 않음 (명세 6-2 표에 없음, 10/8 기록에서도 fault 가 안 붙음)
            elif name == "cnt_board_robot_arm" and st in ("tray_reached", "board_mismatched"):
                if con.get("board") == "failed":
                    self._fault(oid, "board_pack_failed", "board_robot_arm")
                elif con.get("board") == "done":
                    WORLD.board_placed()
                    self.acc[oid]["board_done"] = True
                    self._board(oid)
            elif name == "cnt_board_dispense" and st in ("tray_reached", "board_mismatched"):
                self.acc[oid]["board_color"] = con.get("board")
                self._board(oid)

    def _board(self, oid):
        a, o = self.acc[oid], self.orders[oid]
        if a.get("board_done") and a.get("board_color"):
            self._to(oid, "board_packed" if a["board_color"] == o["board"] else "board_mismatched")


PLATFORM = Platform()


# ============================== 가짜 로봇팔 (게이트웨이 쪽) ==============================
class Arm:
    def __init__(self, pose):
        self.p0 = self.p1 = pose
        self.t0 = self.t1 = 0.0
        self.alarms, self.jump, self.stuck = set(), 20.0, 0


class FakeDobot:
    def __init__(self, rng):
        self.arms, self.rng = {}, rng

    def arm(self, port):
        return self.arms.setdefault(port, Arm((200.0, 0.0, 60.0, 0.0)))

    def pose(self, a):
        now = CLOCK.now
        if now >= a.t1 or a.t1 <= a.t0:
            return a.p1
        k = (now - a.t0) / (a.t1 - a.t0)
        return tuple(a.p0[i] + (a.p1[i] - a.p0[i]) * k for i in range(4))

    async def GetPose(self, portName=None, **kw):
        x, y, z, r = self.pose(self.arm(portName))
        return {"x": x, "y": y, "z": z, "r": r, "jointAngle": [0, 0, 0, 0]}

    async def SetPTPJumpParams(self, portName=None, jumpHeight=20.0, **kw):
        self.arm(portName).jump = float(jumpHeight)
        return True

    async def SetPTPCmd(self, portName=None, ptpMode=0, x=0, y=0, z=0, r=0, **kw):
        a = self.arm(portName)
        cur = self.pose(a)
        if a.alarms:
            return True                             # 알람이 남아 있으면 조용히 무시 (실물과 같음)
        if a.stuck > 0 or self.rng.random() < args.noise:
            a.stuck = max(0, a.stuck - 1)
            a.alarms.add(0x12)
            return True
        theta = abs(math.degrees(math.atan2(y, x) - math.atan2(cur[1], cur[0])))
        theta = min(theta, 360.0 - theta)
        radial = abs(math.hypot(x, y) - math.hypot(cur[0], cur[1]))
        if ptpMode == 0:
            peak = min(150.0, max(cur[2], z) + a.jump)
            vert = max(0.0, peak - cur[2]) + max(0.0, peak - z)
            dur = C0 + vert / VZ + 2.0 * math.sqrt(theta / ACC) + radial / 300.0
        else:
            dur = C0 / 2 + abs(z - cur[2]) / VZ + 2.0 * math.sqrt(theta / ACC) + radial / 300.0
        a.p0, a.p1, a.t0, a.t1 = cur, (float(x), float(y), float(z), float(r)), CLOCK.now, CLOCK.now + dur
        WORLD.arm_move(portName, a.p0, a.p1, a.t0, a.t1)
        return True

    async def SetEndEffectorGripper(self, portName=None, enable=True, on=False, **kw):
        if enable:
            WORLD.gripper(portName, bool(on), self.pose(self.arm(portName)))
        return True

    async def GetAlarmsState(self, portName=None, **kw):
        state = [0] * 16
        for c in self.arm(portName).alarms:
            state[c // 8] |= 1 << (c % 8)
        return {"state": state}

    async def ClearAllAlarmsState(self, portName=None, **kw):
        self.arm(portName).alarms.clear()
        return True

    async def SetEMotor(self, portName=None, enable=False, **kw):
        WORLD.keycap_belt(portName, bool(enable))
        return True

    async def ConnectDobot(self, portName=None, **kw):
        self.arm(portName)
        return {"firmwareName": "Sim", "firmwareVersion": "", "productName": ""}

    def __getattr__(self, name):
        async def ok(**kw):
            return True
        return ok


class BeltBox:
    """보드 main 의 MagicBox 자리 - 트레이 벨트 2대"""
    async def SetEMotor(self, portName=None, index=0, enable=False, **kw):
        WORLD.tray_belt(bool(enable))
        return True

    def __getattr__(self, name):
        async def ok(**kw):
            return True
        return ok


# ============================== 진짜 코드 불러오기 ==============================
HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.abspath(args.code or os.path.join(HERE, ".."))
logf = open(args.log, "w", encoding="utf-8") if args.log else open(os.devnull, "w", encoding="utf-8")
sys.stdout = logf

os.environ.pop("WAREHOUSE_REHEARSAL", None)
os.environ["WAREHOUSE_ENV_FILE"] = os.path.join(HERE, "sim.env")   # 진짜 .env.local 대신 가상 주소만 든 설정
os.environ["KEYCAP_FLASK_URL"] = "http://kflask.sim"   # tray_stock 이 import 때 읽음
sys.path.insert(0, os.path.join(CODE, "keycap_tray"))
os.chdir(os.path.join(CODE, "keycap_tray"))
import requests
requests.post = lambda url, json=None, **kw: http("POST", url, json)
requests.get = lambda url, **kw: http("GET", url)
import tray_stock
import tempfile
tray_stock.STOCK_FILE = os.path.join(tempfile.mkdtemp(prefix="warehouse_sim_"), "tray_stock.json")   # 진짜 개수 파일은 안 건드림
import dobot_control as dc
import warehouse_server as ws
import local_transport
from local_feed import SnapshotInbox
TRAY, SETS, STACKS = dc.TRAY, dc.SETS, tray_stock.STACKS
TIMES = {"tray": [], "arms": [], "belts": []}


def _timed(name, fn):
    def run(*a, **k):
        t = CLOCK.now
        try:
            return fn(*a, **k)
        finally:
            TIMES[name].append(round(CLOCK.now - t, 1))
    return run


dc.run_tray_arm = _timed("tray", dc.run_tray_arm)
dc.run_keycap_arms = _timed("arms", dc.run_keycap_arms)
dc.run_keycap_conveyor = _timed("belts", dc.run_keycap_conveyor)
vt = VTime()
for mod in (dc, ws, tray_stock):
    mod.time = vt

os.environ.update(WAREHOUSE_REHEARSAL="1", FLASK_BASE_URL="http://board.sim", COSS_BASE_URL="http://mobius.sim/Mobius",
                  AE_NAME="ae", ORIGIN="sim", REDIS_URL="redis://sim/0")
sys.path.insert(0, os.path.join(CODE, "board"))
os.chdir(os.path.join(CODE, "board"))
import app as board_app_module
import main_board_robot_arm as bm
os.environ.pop("WAREHOUSE_REHEARSAL", None)
BOARD_APP = board_app_module.app
REDIS = FakeRedis()
board_app_module.redis = REDIS
for code in "rybg":
    REDIS.set(f"stock:board_{code}", 20)
    REDIS.set(f"pool:board_{code}", 310)
bm.time = vt
HAS_TRAY_WAIT = hasattr(bm, "tray_arm_expected")
if HAS_TRAY_WAIT:
    bm.tray_arm_expected = lambda: True     # 게이트웨이가 켜져 있고 트레이 팔을 쓰는 상황

STOP = threading.Event()
ERRORS = []


def worker(fn):
    def run():
        CLOCK.register()
        try:
            fn()
        except Exception:
            ERRORS.append(traceback.format_exc())
            STOP.set()
        finally:
            CLOCK.unregister()
    return threading.Thread(target=run, daemon=True)


# ---- 게이트웨이 (local_gateway.run 의 주 반복과 같은 순서) ----
def gateway():
    rng = random.Random(args.seed * 7 + 1)
    fake = FakeDobot(rng)
    GW["fake"] = fake
    dc._loop = asyncio.new_event_loop()
    asyncio.set_event_loop(dc._loop)
    dc._arm_api = dc._box_api = fake
    ws.hw.connect()
    WORLD.roles = {TRAY["port"]: "tray", SETS["set1"]["port"]: "set1", SETS["set2"]["port"]: "set2"}
    fake.arm(TRAY["port"]).p0 = fake.arm(TRAY["port"]).p1 = (169.7, -169.7, 50.0, -45.0)
    for key in ("set1", "set2"):
        x, y, z, r = SETS[key]["pickup_slots"][1]
        fake.arm(SETS[key]["port"]).p0 = fake.arm(SETS[key]["port"]).p1 = (x, y, z + 50.0, r)
    inbox, pending, next_read = GW["inbox"], None, 0.0
    while not STOP.is_set():
        if CLOCK.now >= next_read:
            next_read = CLOCK.now + 5.0
            try:
                inbox.submit(local_transport.get_latest("cnt_process"), from_poll=True)
            except Exception as e:
                print(f"[읽기 실패] {e}")
            if pending is not None and inbox.queue.empty():
                pending = pending if ws.process_snapshot(pending) == "retry" else None
        try:
            snap = inbox.queue.get_nowait()
        except queue.Empty:
            pass
        else:
            pending = snap if ws.process_snapshot(snap) == "retry" else None
        ws.hw.keepalive()
        if hasattr(ws.hw, "link_watchdog"):
            ws.hw.link_watchdog()
        CLOCK.sleep(0.1)


GW = {"inbox": SnapshotInbox(), "fake": None}


# ---- 보드 main (main() 의 주 반복과 같은 순서) ----
def board():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    arm, box = bm.RehearsalDobot(), BeltBox()
    bm._arm_api = arm
    bm.local_stock = bm.fetch_initial_stock()
    bm.job_queue = bm.fetch_queue_from_server()
    gate = bm.fetch_gate_from_server()
    bm.gate_open, bm.gate_order_id = (False, gate.get("order_id", "")) if gate.get("locked") else (True, "")
    first = False
    while not STOP.is_set():
        bm.flush_pending_reports()
        try:
            data = bm.snapshot_queue.get_nowait()
        except queue.Empty:
            data = None
        if data is not None:
            try:
                bm.handle_snapshot(loop, arm, box, data)
            except Exception:
                print("!! 보드 main 스냅샷 처리 예외\n" + traceback.format_exc())
                WORLD.inc("보드 main 예외")
            first = True
        if first and bm.gate_open and bm.job_queue:
            job = bm.job_queue.pop(0)
            try:
                bm.process_board_job(loop, arm, box, job["order_id"], job["color"])
            except Exception:
                print("!! 보드 main 작업 예외\n" + traceback.format_exc())
                WORLD.inc("보드 main 예외")
        for color, count in list(bm.local_stock.items()):      # 운영자: 보드가 떨어지기 전에 채움
            if count <= 1 and not bm.job_queue:
                bm.local_stock[color] = 20
                REDIS.set(f"stock:board_{COLOR_CODE[color]}", 20)
        CLOCK.sleep(0.2)


# ---- 키캡 배출 (아두이노 4대) ----
def arduino():
    rng = random.Random(args.seed * 7 + 2)
    done = set()
    while not STOP.is_set():
        with PLATFORM.lock:
            todo = [(oid, o) for oid, o in PLATFORM.orders.items() if o["status"] == "assigned" and oid not in done]
        for oid, o in todo:
            done.add(oid)
            total = rng.uniform(*DISPENSE)
            times = sorted(rng.uniform(0.45, 1.0) * total for _ in range(4))
            times[-1] = total
            start = CLOCK.now
            for (letter, color), t in zip(zip(o["keycap"], o["colors"]), times):
                CLOCK.sleep(max(0.0, start + t - CLOCK.now))
                PLATFORM.create_cin("cnt_warehouse/cnt_keycap_dispense", {"order_id": oid, "keycap": f"{letter}_{color}"})
        CLOCK.sleep(0.1)


# ---- 주문 넣기 · 단계 시간 초과 · 운영자 · AGV ----
def director():
    rng = random.Random(args.seed * 7 + 3)
    issued, fix_at, pickup_at, next_order_at = 0, {}, {}, 1.0
    while not STOP.is_set():
        now = CLOCK.now
        with PLATFORM.lock:
            orders = copy.deepcopy(PLATFORM.orders)
            stage_t = dict(PLATFORM.stage_t)
        busy = any(o["status"] in WAREHOUSE_STAGES for o in orders.values()) or (args.gap > 0 and bool(orders))
        # 운영자: 주문 사이에 빈 트레이가 2개 이하면 15개로 채우고 상태판에 15 입력 (가끔 잊음)
        if not busy and TRAY_STOCK["count"] <= 2 and "refill_decided" not in fix_at:
            fix_at["refill_decided"] = True
            if rng.random() >= args.forget:
                WORLD.stacks = [5, 5, 5]
                TRAY_STOCK["count"] = 15
        if TRAY_STOCK["count"] > 2:
            fix_at.pop("refill_decided", None)
        if not busy and issued < args.orders and now >= next_order_at:
            issued += 1
            keycap = "".join(rng.choice(p) for p in ("EI", "NS", "TF", "JP"))
            PLATFORM.new_order(f"ord_{issued:04d}", keycap, [rng.choice("rybg") for _ in range(4)],
                               ["red", "yellow", "blue", "green"][issued % 4])
            if rng.random() < args.hardfail:
                GW["fail_order"] = f"ord_{issued:04d}"
        for oid, o in orders.items():
            if o["status"] == "pickup_reached":
                if oid not in pickup_at:
                    pickup_at[oid] = now + 5.0
                elif now >= pickup_at[oid]:
                    PLATFORM.complete(oid, WORLD.pickup())
                    next_order_at = now + max(2.0, args.gap)
                continue
            if o["status"] not in WAREHOUSE_STAGES:
                continue
            if o["fault"]:
                if oid not in fix_at:
                    fix_at[oid] = now + rng.uniform(15.0, 40.0)
                elif now >= fix_at[oid]:
                    del fix_at[oid]
                    if o["fault"] == "tray_load_failed":
                        with WORLD.lock:
                            WORLD.zone = (None, None)           # 가이드대로: 팔이 벨트 위에 멈춰 있으면 손으로 비킴
                        if TRAY_STOCK["count"] <= 0:
                            WORLD.stacks = [5, 5, 5]            # 15개 다 쌓고 상태판에 15 입력
                            TRAY_STOCK["count"] = 15
                    if o["fault"] == "keycap_pack_failed":
                        with WORLD.lock:                    # 가이드대로: 키캡 4개를 손으로 트레이에 넣음
                            tray = WORLD.tray_at(ST_KEYCAP)
                            if tray:
                                tray["keycaps"] = 4
                    PLATFORM.clear_fault(oid)
            elif stage_t.get(oid) is not None and now - stage_t[oid] > STAGE_LIMIT:
                PLATFORM.discard(oid, f"시간 초과({o['status']})")
                WORLD.clear_line()                          # 운영자가 라인을 비움
                next_order_at = now + 20.0
        # 주문당 한 번, 지금 움직이는 팔 하나를 "끝내 못 감" 상태로 (재전송 2번까지 무시)
        fo = GW.get("fail_order")
        if fo and fo in orders and GW["fake"] is not None and orders[fo]["status"] in ("assigned", "keycap_reached"):
            if rng.random() < 0.05:
                port = TRAY["port"] if orders[fo]["status"] == "assigned" else SETS[rng.choice(["set1", "set2"])]["port"]
                GW["fake"].arm(port).stuck = 3
                GW["fail_order"] = None
        with PLATFORM.lock:
            left = bool(PLATFORM.orders)
        if issued >= args.orders and not left:
            STOP.set()
        CLOCK.sleep(0.25)


PLATFORM.subs = [lambda snap: GW["inbox"].submit(snap), lambda snap: bm.accept_snapshot(snap, "알림")]

threads = [worker(f) for f in (gateway, board, arduino, director)]
for t in threads:
    t.start()
for t in threads:
    t.join()
sys.stdout = REAL_OUT
logf.close()

# ============================== 결과 ==============================
hist = PLATFORM.hist
results = {}
for oid, h in hist.items():
    results[h["result"] or "미완료"] = results.get(h["result"] or "미완료", 0) + 1
inc = {}
for _, oid, kind in WORLD.incidents:
    inc[kind] = inc.get(kind, 0) + 1
faults = {}
for h in hist.values():
    for _, st, f in h["faults"]:
        faults[f"{f}@{st}"] = faults.get(f"{f}@{st}", 0) + 1
dur = {}
for h in hist.values():
    for st, (a, b) in h["stages"].items():
        if b is not None and st in NEXT and not any(f[1] == st for f in h["faults"]):
            dur.setdefault(st, []).append(round(b - a, 1))
margins = [round(m["belt0_start"] - m["tray_released"], 1) for m in WORLD.marks.values()
           if "belt0_start" in m and "tray_released" in m]
summary = {
    "code": CODE, "orders": len(hist), "seed": args.seed, "noise": args.noise,
    "hardfail": args.hardfail, "forget": args.forget, "empty_reset": args.empty_reset, "dispense": args.dispense, "tray_wait_code": HAS_TRAY_WAIT,
    "virtual_minutes": round(CLOCK.now / 60, 1), "results": results, "incidents": inc, "faults": faults,
    "stage_sec(min/avg/max)": {st: [min(v), round(sum(v) / len(v), 1), max(v)] for st, v in dur.items()},
    "arm_sec(min/avg/max)": {k: [min(v), round(sum(v) / len(v), 1), max(v)] for k, v in TIMES.items() if v},
    "tray_release_to_belt_start_sec(min/avg/max)": [min(margins), round(sum(margins) / len(margins), 1), max(margins)] if margins else None,
    "errors": ERRORS[:2],
    "bad_orders": [(oid, h["result"], h["faults"]) for oid, h in hist.items() if h["result"] != "정상"][:12],
    "incident_list": WORLD.incidents[:12],
}
if args.out:
    json.dump(summary, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(json.dumps(summary, ensure_ascii=False, indent=1))
