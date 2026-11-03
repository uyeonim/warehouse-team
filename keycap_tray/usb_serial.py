# -*- coding: utf-8 -*-
"""
로봇 USB 시리얼 번호 -> 지금 COM 번호 찾기 (Windows 레지스트리, 0.1초 안쪽).
COM 번호는 노트북·USB 구멍·드라이버 재설치에 따라 바뀔 수 있어서(2026-11-03 키캡 팔 두 대가 서로 바뀐 채로
움직인 사례), 설정에는 장치마다 고유한 USB 시리얼을 적고 켤 때 COM을 찾음.
시리얼이 설정돼 있는데 못 찾으면 None - COM 예비값으로 넘어가지 않음(다른 팔이 그 번호를 받았을 수 있어서 위험).
board/usb_serial.py 와 keycap_tray/usb_serial.py 는 같은 파일.

Dobot(Magician Lite)은 USB\\VID_F055&PID_9800\\<시리얼> 장치 아래 인터페이스 MI_01 이 COM 포트.
  - 시리얼 장치 키의 ParentIdPrefix 값 = MI_01 인스턴스 ID의 앞부분 -> 둘을 이어서 시리얼 <-> COM
  - 레지스트리엔 예전에 꽂았던 장치도 남아 있어서, HARDWARE\\DEVICEMAP\\SERIALCOMM(지금 있는 COM)과 대조
"""
import winreg

DOBOT_USB = r"SYSTEM\CurrentControlSet\Enum\USB\VID_F055&PID_9800"


def _subkeys(path):
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            i = 0
            while True:
                try:
                    yield winreg.EnumKey(k, i)
                except OSError:
                    return
                i += 1
    except OSError:
        return


def _value(path, name):
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def _present_coms():
    coms = set()
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as k:
            i = 0
            while True:
                try:
                    coms.add(winreg.EnumValue(k, i)[1].upper())
                except OSError:
                    break
                i += 1
    except OSError:
        pass
    return coms


def list_ports():
    """{시리얼: COM} - 지금 꽂혀 있는 Dobot 전부"""
    prefix_to_serial = {}
    for serial in _subkeys(DOBOT_USB):
        prefix = _value(rf"{DOBOT_USB}\{serial}", "ParentIdPrefix")
        if prefix:
            prefix_to_serial[prefix.lower()] = serial.upper()
    present = _present_coms()
    result = {}
    for inst in _subkeys(DOBOT_USB + "&MI_01"):
        com = _value(rf"{DOBOT_USB}&MI_01\{inst}\Device Parameters", "PortName")
        serial = prefix_to_serial.get(inst.rsplit("&", 1)[0].lower())
        if com and serial and com.upper() in present:
            result[serial] = com.upper()
    return result


def resolve_port(serial, fallback, ports=None):
    """시리얼이 있으면 그 장치의 COM (못 찾으면 None), 시리얼을 비워 두면 fallback(COM 번호) 그대로"""
    serial = (serial or "").strip().upper()
    if not serial:
        return fallback
    if ports is None:
        ports = list_ports()
    return ports.get(serial)
