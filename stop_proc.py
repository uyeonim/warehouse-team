# -*- coding: utf-8 -*-
"""다른 콘솔 창에서 돌고 있는 프로그램에 Ctrl+C 를 보냄 (stop_all.ps1 이 부름).
Ctrl+C 로 꺼야 로봇 연결 해제·컨베이어 정지 같은 마무리 코드가 돎 (강제 종료는 그걸 건너뜀).
사용: python stop_proc.py <pid>   종료코드 0 = 보냄 / 1 = 못 보냄"""
import ctypes
import sys

k = ctypes.windll.kernel32
pid = int(sys.argv[1])
k.FreeConsole()
if not k.AttachConsole(pid):
    sys.exit(1)
k.SetConsoleCtrlHandler(None, True)   # 이 프로그램은 Ctrl+C 에 안 죽게
ok = k.GenerateConsoleCtrlEvent(0, 0)   # CTRL_C_EVENT -> 붙은 콘솔의 모든 프로세스
k.FreeConsole()
sys.exit(0 if ok else 1)
