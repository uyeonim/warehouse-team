# -*- coding: utf-8 -*-
"""
화면 출력(stdout/stderr)은 그대로 두고 logs/{name}_{날짜}.log 에도 같이 기록.
local_gateway.py가 실행 시작할 때 start_file_log()를 한 번 호출함 (board/log_tee.py와 같은 파일).
같은 날 다시 실행하면 같은 파일에 이어서 씀 (실행마다 "===== 시작 =====" 구분선).
"""
import logging
import os
import sys
import time


class _Tee:
    def __init__(self, stream, f):
        self.stream, self.f = stream, f

    def write(self, s):
        self.stream.write(s)
        self.f.write(s)
        return len(s)

    def flush(self):
        self.stream.flush()
        self.f.flush()

    def __getattr__(self, name):   # encoding/isatty 등은 원래 스트림 것 그대로
        return getattr(self.stream, name)


def start_file_log(name):
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"{name}_{time.strftime('%Y-%m-%d')}.log")
    f = open(path, "a", encoding="utf-8", buffering=1)   # 줄 단위로 바로 기록 - 강제 종료돼도 남게
    f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 시작 =====\n")

    orig_out, orig_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _Tee(orig_out, f), _Tee(orig_err, f)

    # import 시점에 이미 만들어진 logging 핸들러(DobotRPC 등)는 예전 스트림을 쥐고 있어서 같이 바꿔줌
    loggers = [logging.getLogger()] + [lg for lg in logging.Logger.manager.loggerDict.values()
                                       if isinstance(lg, logging.Logger)]
    for lg in loggers:
        for h in lg.handlers:
            if isinstance(h, logging.StreamHandler) and h.stream is orig_out:
                h.setStream(sys.stdout)
            elif isinstance(h, logging.StreamHandler) and h.stream is orig_err:
                h.setStream(sys.stderr)
    return path
