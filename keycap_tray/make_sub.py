"""
로컬 Mobius cnt_process 구독(sub_warehouse_keycap_robotarm) 등록 - 한 번만 실행하는 설정용.
실행: python make_sub.py [--env .env.local]   (확인 질문에 y 입력해야 실제로 삭제/재생성)
- 알림 주소(nu) = .env.local 의 NOTIFY_BASE_URL + /notify/process (게이트웨이 수신부)
- 같은 이름이 있으면 지우고 다시 만듦. 이 이름만 지움 - 보드(sub_warehouse_robotarm)·
  키캡 카트리지(sub_warehouse_keycap_cartridge) 구독은 안 건드림
- local_gateway.py 가 켜진 상태에서 실행할 것 (등록 시 Mobius가 확인 요청을 보낼 수 있음)
"""
import argparse
import os
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv

SUB_RN = 'sub_warehouse_keycap_robotarm'
CNT = 'cnt_process'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', default=str(Path(__file__).with_name('.env.local')))
    args = parser.parse_args()
    load_dotenv(args.env, override=True)

    base = '/'.join([os.environ['MOBIUS_URL'].rstrip('/'), os.environ['MOBIUS_CB'], os.environ['MOBIUS_AE_RN']])
    notify = os.environ.get('NOTIFY_BASE_URL', '').strip().rstrip('/')
    if not notify:
        raise SystemExit('.env.local 에 NOTIFY_BASE_URL 이 비어있음 - 중단 (빈 주소로 구독 만들면 알림이 안 옴)')
    nu = f'{notify}/notify/process'

    def headers(create=False):
        h = {'X-M2M-Origin': os.environ['MOBIUS_AEI'], 'X-M2M-RI': f'sub-{uuid.uuid4().hex}',
             'X-M2M-RVI': '3', 'Accept': 'application/json'}
        if create:
            h['Content-Type'] = 'application/vnd.onem2m-res+json; ty=23'
        return h

    print(f'구독 rn  : {SUB_RN}')
    print(f'대상 cnt : {os.environ["MOBIUS_AE_RN"]}/{CNT}')
    print(f'알림 주소: {nu}')
    if input('이 구독을 삭제 후 재생성합니다. 계속할까요? (y/n): ').strip().lower() != 'y':
        print('취소 - 아무것도 안 바꿈')
        return

    d = requests.delete(f'{base}/{CNT}/{SUB_RN}', headers=headers(), timeout=15)
    print(f'삭제 시도: {d.status_code}' + (' (없음 - 정상)' if d.status_code == 404 else ''))
    body = {'m2m:sub': {'rn': SUB_RN, 'nu': [nu], 'enc': {'net': [3]}, 'nct': 1}}
    r = requests.post(f'{base}/{CNT}', headers=headers(create=True), json=body, timeout=15)
    print(f'생성: {r.status_code} -> {nu}', '' if r.status_code == 201 else r.text)


if __name__ == '__main__':
    main()
