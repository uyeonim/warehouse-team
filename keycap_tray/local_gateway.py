"""R1/R2/R4 worker beside existing servers; local Mobius notifications + HTTP catch-up."""
import argparse
import os
import time
import queue
import math
from pathlib import Path


def validate_local_options(environ):
    """설정 검사와 실전 시작에 공통 적용. 장비/네트워크 접근 없음."""
    def flag(name):
        value = environ.get(name, '0').strip()
        if value not in ('0', '1'):
            raise RuntimeError(f'{name}은 0 또는 1이어야 합니다.')
        return value == '1'

    if environ.get('REPORT_ROOT', '').strip('/'):
        raise RuntimeError('로컬 Flask 보고 모드의 REPORT_ROOT는 비워야 합니다.')
    # 담당 외 단계에 가짜 완료를 올려 다른 팔/벨트를 진행시키지 않는다.
    fake_flags = ('FAKE_UNBUILT', 'FAKE_TRAY_CONVEYOR', 'FAKE_TRAY_SECTION0',
                  'FAKE_TRAY_SECTION2', 'FAKE_BOARD_ARM', 'FAKE_BOARD_DISPENSE')
    if any(flag(k) for k in fake_flags):
        raise RuntimeError('로컬 실전 작업자에서는 FAKE_* 보고를 끄세요. 보드/트레이 벨트는 기존 담당 코드가 처리합니다.')
    if flag('CHECK_GRIP_IR') or flag('CHECK_BOARD_COLOR'):
        raise RuntimeError('이 폴더의 집기/색 센서 함수는 아직 미구현입니다.')
    if flag('DRY_RUN') or environ.get('FORCE_FAIL', '').strip():
        raise RuntimeError('로컬 실전 작업자는 모의 보고/강제 실패 설정을 허용하지 않습니다.')
    hardware = environ.get('HARDWARE', '').strip().lower()
    rehearsal = environ.get('WAREHOUSE_REHEARSAL') == '1'
    if hardware != 'dobot' and not (rehearsal and hardware == 'fake'):
        raise RuntimeError('로컬 실전 작업자는 HARDWARE=dobot을 명시해야 합니다 (리허설만 fake).')
    if environ.get('ARMS_TAKE_TURNS', '1').strip() != '1':
        raise RuntimeError('실전 모드는 두 팔이 트레이 자리를 번갈아 써야 합니다 (ARMS_TAKE_TURNS=1).')
    if not flag('REACT_ASSIGNED'):
        raise RuntimeError('이 작업자는 R4 투입을 담당하므로 REACT_ASSIGNED=1이 필요합니다.')
    # 팔마다 USB 시리얼(있으면) 또는 COM 포트 - 세 팔이 서로 달라야 함
    ports = [(environ.get(f'{name}_SERIAL', '').strip() or environ.get(f'{name}_PORT', default).strip()).upper()
             for name, default in (('SET1', 'COM6'), ('SET2', 'COM7'), ('TRAY', 'COM8'))]
    if any(not p for p in ports) or len(set(ports)) != len(ports):
        raise RuntimeError('SET1 / SET2 / TRAY 의 _SERIAL(또는 _PORT)은 서로 달라야 합니다.')
    period = float(environ.get('PROCESS_POLL_SEC', '5'))
    if not math.isfinite(period) or period <= 0:
        raise RuntimeError('PROCESS_POLL_SEC는 유한한 양수여야 합니다.')
    notify_port = int(environ.get('NOTIFY_PORT', '5003'))
    if not 1 <= notify_port <= 65535:
        raise RuntimeError('NOTIFY_PORT 범위를 확인하세요.')
    if notify_port in (5000, 5001, 5002):
        raise RuntimeError('NOTIFY_PORT는 보드 Flask(5000)·보드 main(5001)·키캡 Flask(5002)와 겹치면 안 됩니다.')
    return period, notify_port


def run():
    # 프로필 선택 전에 warehouse_server / DobotRPC를 import하지 않는다.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', default=str(Path(__file__).with_name('.env.local')))
    parser.add_argument('--check', action='store_true', help='설정만 검증. 네트워크/로봇 연결 없음')
    args = parser.parse_args()
    os.environ['WAREHOUSE_ENV_FILE'] = str(Path(args.env).resolve())
    from runtime_config import PROFILE
    if PROFILE != 'local':
        raise RuntimeError('로컬 실행 설정에 WAREHOUSE_PROFILE=local이 필요합니다.')
    if os.environ.get('WAREHOUSE_REHEARSAL') == '1':
        # 리허설 (start_rehearsal.bat): .env.local 의 HARDWARE=dobot 보다 우선. warehouse_server 의 가짜 장비로
        # "움직인 척"만 하고 보고는 실제로 보냄. warehouse_server import 전에 바꿔야 적용됨
        os.environ['HARDWARE'] = 'fake'
        print('!! 리허설 모드 - 키캡 팔·트레이 팔·키캡 벨트는 움직인 척만 함. 보고는 실제로 보냄')
    import local_transport
    cfg = local_transport.settings()
    period, notify_port = validate_local_options(os.environ)
    if args.check:
        print('로컬 설정 형식 확인 완료. 서버 API 호환성과 실물 좌표는 별도 검증이 필요합니다.')
        return
    from log_tee import start_file_log
    print(f'로그 파일: {start_file_log("gateway")}')   # 창을 닫거나 재부팅돼도 기록이 남게
    from worker_lock import WorkerLock
    with WorkerLock():
        import warehouse_server as ws
        from local_feed import SnapshotInbox, notify_server
        from heartbeat import Heartbeat
        inbox = SnapshotInbox()
        # 상태 기록 (Redis hb:keycap_tray, check_all.py가 읽음). Redis가 꺼져 있어도 동작엔 영향 없음
        import tray_stock  # 빈 트레이 남은 개수도 같이 기록 (hb:keycap_tray 의 tray_left / tray_total)
        hb = Heartbeat('keycap_tray', os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0'),
                       'rehearsal' if os.environ.get('WAREHOUSE_REHEARSAL') == '1' else 'real',
                       extra=lambda: {'tray_left': tray_stock.get(), 'tray_total': tray_stock.TOTAL}
                       if tray_stock.enabled() else {}).start()
        if tray_stock.enabled():
            print(tray_stock.describe())
        inbox.on_accept = hb.snapshot_received

        def process(snap):
            """스냅샷 처리 동안 busy + 주문 상태를 기록 - 한 단계에서 오래 멈추면 점검 화면에 보이게"""
            job = ', '.join(f"{oid} {o.get('status')}" for oid, o in snap.get('orders', {}).items())[:120]
            hb.set_state('busy', job or None)
            try:
                return ws.process_snapshot(snap)
            finally:
                hb.set_state('idle')

        process_path = os.environ.get('PROCESS_PATH', 'cnt_process').strip('/')
        print(f"로컬 R1/R2/R4 작업자: {cfg['MOBIUS_URL']} 읽기 → {cfg['BOARD_API_URL']} 보고")
        print('5000/5001/5002 서버 및 Redis는 기존 담당 프로그램이 운영합니다.')
        print('좌표: 키캡 팔 = 실물에서 찍은 값(pose_set1/2.json), 트레이 팔 = pose_tray.json 이 있으면 실물 값, '
              '없으면 도면 추정값(LAYOUT=design). 참고사항 3장')
        next_read = 0.0
        pending = None
        server = None
        try:
            # 먼저 현재 상태를 읽어 시작 기준으로 삼는다. 읽힐 때까지 로봇 연결도 하지 않는다.
            # 실패해도 끝내지 않고 PROCESS_POLL_SEC마다 다시 시도 - 플랫폼 서버보다 먼저 켜지거나
            # 네트워크가 늦게 붙어도 사람이 다시 켜지 않아도 되게 (노트북을 테이블 밑에 두는 운영)
            waited = False
            while True:
                try:
                    initial = local_transport.get_latest(process_path)
                    break
                except Exception as error:
                    if not waited:
                        print(f'[시작 대기] Mobius 읽기 실패 - {period:g}초마다 다시 시도: {error}')
                        waited = True
                        hb.set_state('waiting_mobius')
                    time.sleep(period)
            if waited:
                print('[시작 대기] Mobius 연결됨 - 시작')
            if initial is not None:
                def reported(path, order_id):
                    """Mobius 에 이 주문의 우리 보고(그 단계 컨테이너 최신 CIN)가 있는지. 못 읽으면 None"""
                    try:
                        con = local_transport.get_latest(path)
                    except Exception as error:
                        print(f'[재시작] {path} 읽기 실패 - 이미 한 것으로 봄: {error}')
                        return None
                    return isinstance(con, dict) and con.get('order_id') == order_id

                inbox.submit(initial, from_poll=True)
                # 켤 때 이미 진행 중이던 주문은 기준으로만 (새 status 부터 반응). 단 우리 단계인데 보고가 없으면
                # 꺼지기 전에 못 끝낸 것 -> 이어서 실행 (11/05)
                ws.mark_as_handled(initial, reported)
            # 시작 기준을 잡은 뒤에 수신 시작 (Mobius 구독 nu = NOTIFY_BASE_URL/notify/process)
            server = notify_server(inbox, notify_port)
            if ws.hw:
                missing = ws.hw.connect()   # 안 보이는 팔이 있어도 끝내지 않음 - 아래 연결 감시가 다시 찾음
                if missing:
                    print(f'!! 연결 못 한 팔: {", ".join(missing)} - USB/전원 확인. 5초마다 다시 시도')
            hb.set_state('idle')
            while True:
                now = time.monotonic()
                if now >= next_read:
                    next_read = now + period
                    try:
                        snapshot = local_transport.get_latest(process_path)
                    except Exception as error:
                        print(f'[읽기 실패] {error}')
                        hb.update(poll_ok=False)
                    else:
                        hb.update(poll_ok=True)
                        if (isinstance(snapshot, dict) and isinstance(snapshot.get('orders', {}), dict)
                                and all(isinstance(o, dict) for o in snapshot.get('orders', {}).values())):
                            inbox.submit(snapshot, from_poll=True)
                    if pending is not None:
                        # 새 상태가 있으면 아래 큐에서 먼저 처리한다.
                        if inbox.queue.empty():
                            pending = pending if process(pending) == 'retry' else None
                try:
                    snapshot = inbox.queue.get_nowait()
                except queue.Empty:
                    pass
                else:
                    pending = snapshot if process(snapshot) == 'retry' else None
                if ws.hw:
                    ws.hw.keepalive()
                    ws.hw.link_watchdog()   # 작업 사이에만: 연결 확인 / 끊긴 팔 재연결 (11/05)
                    problem = ws.hw.link_problem()
                    if problem:
                        hb.set_state('link_lost', problem)   # 상태판·점검에 "연결 끊김"
                    elif hb.data.get('state') == 'link_lost':
                        hb.set_state('idle')
                time.sleep(0.1)
        except KeyboardInterrupt:
            print('로컬 작업자를 종료합니다.')
        finally:
            try:
                if ws.hw:
                    ws.hw.disconnect()
            finally:
                hb.stop()
                if server is not None:
                    server.shutdown()
                    server.server_close()


if __name__ == '__main__':
    run()
