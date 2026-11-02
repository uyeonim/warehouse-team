# -*- coding: utf-8 -*-
"""
로봇 프로그램 상태(heartbeat)를 INTERVAL_SEC마다 Redis hb:<name> 에 기록 (TTL_SEC 지나면 자동 삭제).
check_all.py(나중에 상태판)이 읽어서 "포트는 열려 있는데 멈춘" 상태까지 잡아냄.
- 기록은 별도 스레드 - 픽업·컨베이어처럼 오래 걸리는 작업 중에도 끊기지 않음
- 메인 코드는 상태가 바뀔 때 set_state()/update()만 부름
- Redis가 꺼져 있어도 프로그램은 그대로 돎 (기록만 못 함, 로그는 처음 한 번)
board/heartbeat.py 와 keycap_tray/heartbeat.py 는 같은 파일.
"""
import json
import os
import threading
import time

INTERVAL_SEC = 5
TTL_SEC = 30


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Heartbeat:
    def __init__(self, name, redis_url, mode, log=print, extra=None):
        """name: hb:<name> 키 / mode: real 또는 rehearsal / extra: 기록할 때마다 불러서 합칠 dict를 돌려주는 함수"""
        self.key = f"hb:{name}"
        self.redis_url = redis_url
        self.log = log
        self.extra = extra
        self.lock = threading.Lock()
        self.data = {"mode": mode, "state": "starting", "since": _now(), "job": None,
                     "last_snapshot": None, "poll_ok": None, "pid": os.getpid()}
        self._client = None
        self._failing = False
        self._stopped = False

    def set_state(self, state, job=None):
        """state: starting / waiting_mobius / idle / busy / link_lost. 상태나 작업이 바뀌면 since 갱신"""
        with self.lock:
            if state != self.data["state"] or job != self.data["job"]:
                self.data["since"] = _now()
            self.data["state"] = state
            self.data["job"] = job

    def update(self, **fields):
        with self.lock:
            self.data.update(fields)

    def snapshot_received(self, seq, via):
        self.update(last_snapshot={"seq": seq, "time": _now(), "via": via})

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def stop(self):
        """정상 종료 - 키를 바로 지워서 점검 화면에 '꺼짐'으로 보이게"""
        self._stopped = True
        try:
            if self._client is not None:
                self._client.delete(self.key)
        except Exception:
            pass

    def _run(self):
        from redis import Redis
        self._client = Redis.from_url(self.redis_url, decode_responses=True,
                                      socket_timeout=2, socket_connect_timeout=2)
        while not self._stopped:
            with self.lock:
                payload = dict(self.data, time=_now())
            if self.extra:
                try:
                    payload.update(self.extra())
                except Exception:
                    pass
            try:
                self._client.set(self.key, json.dumps(payload, ensure_ascii=False), ex=TTL_SEC)
                if self._failing:
                    self.log("heartbeat 기록 복구됨")
                    self._failing = False
            except Exception as e:
                if not self._failing:
                    self.log(f"heartbeat 기록 실패 (Redis): {e} - 로봇 동작과는 무관, 점검 화면에만 안 보임")
                    self._failing = True
            time.sleep(INTERVAL_SEC)
