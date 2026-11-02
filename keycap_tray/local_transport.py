"""2026-11-02 local LAN adapter; no cloud API-key headers and no Redis writes."""
import json
import os
import uuid
from urllib.parse import quote
import requests

from runtime_config import PROFILE

REPORT_ROUTES = {
    'cnt_robot_arm/cnt_tray_robot_arm': '/report/tray-robot-arm',
    'cnt_robot_arm/cnt_keycap_robot_arm': '/report/keycap-robot-arm',
    'cnt_conveyor/cnt_keycap_conveyor': '/report/conveyor/keycap',
    'cnt_conveyor/cnt_tray_conveyor': '/report/conveyor/tray',
    'cnt_robot_arm/cnt_board_robot_arm': '/report/board-robot-arm',
    'cnt_warehouse/cnt_board_dispense': '/report/board-dispense',
}


def settings():
    if PROFILE != 'local':
        raise RuntimeError('로컬 전송은 WAREHOUSE_PROFILE=local에서만 사용합니다.')
    required = ('MOBIUS_URL', 'MOBIUS_CB', 'MOBIUS_AE_RN', 'MOBIUS_AEI', 'BOARD_API_URL')
    missing = [k for k in required if not os.environ.get(k, '').strip()]
    if missing:
        raise RuntimeError('로컬 설정 필요: ' + ', '.join(missing))
    return {k: os.environ[k].strip().rstrip('/') for k in required}


def report_payload(path, con):
    """CIN fields -> Board Flask request fields (reference app.py contract)."""
    if path not in REPORT_ROUTES:
        raise ValueError(f'로컬 API에 대응하지 않는 보고 경로: {path}')
    payload = dict(con)
    if path == 'cnt_robot_arm/cnt_keycap_robot_arm':
        fields = [f'keycap{i}' for i in range(1, 5) if f'keycap{i}' in con]
        if len(fields) != 1:
            raise ValueError('키캡 로봇 보고는 한 건에 한 개여야 합니다.')
        key = fields[0]
        payload = {'order_id': con['order_id'], 'keycap_index': int(key[-1]), 'status': con[key]}
    elif path == 'cnt_conveyor/cnt_tray_conveyor':
        fields = [f'section{i}' for i in range(3) if f'section{i}' in con]
        if len(fields) != 1:
            raise ValueError('트레이 벨트 보고는 한 건에 한 구간이어야 합니다.')
        key = fields[0]
        payload = {'order_id': con['order_id'], 'section': key[-1], 'status': con[key]}
    return REPORT_ROUTES[path], payload


def create_cin(path, con):
    cfg = settings()
    route, payload = report_payload(path, con)
    response = requests.post(cfg['BOARD_API_URL'] + route, json=payload, timeout=(3, 5))
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f'보드 API 보고 실패: {route}, HTTP {response.status_code}')
    # HTTP 200이어도 서버가 실패를 명시하면 전송 완료로 저장하지 않음.
    body = response.json()
    if not isinstance(body, dict) or body.get('error') or body.get('result') != 'ok':
        raise RuntimeError(f'보드 API 성공 응답 확인 필요: {route}')


def get_latest(path):
    cfg = settings()
    base = '/'.join([cfg['MOBIUS_URL'], quote(cfg['MOBIUS_CB'], safe=''), quote(cfg['MOBIUS_AE_RN'], safe='')])
    headers = {'X-M2M-RI': str(uuid.uuid4()), 'X-M2M-Origin': cfg['MOBIUS_AEI'], 'Accept': 'application/json'}
    response = requests.get(f"{base}/{path.strip('/')}/la", headers=headers, timeout=(3, 5))
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise RuntimeError(f'로컬 Mobius 읽기 실패: HTTP {response.status_code}')
    value = response.json()['m2m:cin']['con']
    return json.loads(value) if isinstance(value, str) else value
