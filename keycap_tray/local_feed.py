"""Local Mobius notify receiver + polling inbox. Receiver threads never call robot APIs."""
import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

NOTIFY_PATH = '/notify/process'


class SnapshotInbox:
    def __init__(self):
        self.queue = queue.Queue()
        self.lock = threading.Lock()
        self.last_seq = None
        self.latest_orders = {}
        self.fault_latches = set()
        self.received_snapshot = False
        self.on_accept = None   # (seq, via) -> None. heartbeat 기록용 (local_gateway 가 연결)

    def submit(self, value, from_poll=False):
        """스냅샷을 큐에 넣음. 넣었으면 True.
        - 알림: seq가 직전과 같으면(중복 알림) 버림. 플랫폼 서버가 재시작해서 seq가 1부터
          다시 시작하거나, 다른 곳(테스트 등)이 큰 seq를 올린 뒤에도 따라가야 해서 "다르면 받음"
        - 조회(from_poll): seq가 직전보다 클 때만 받음. 조회 응답이 오는 사이 더 새 알림이 먼저
          들어왔을 수 있어서, 옛 스냅샷으로 상태가 뒤로 돌아가는 걸 막음"""
        if isinstance(value, (str, bytes)):
            value = json.loads(value)
        if not isinstance(value, dict) or not isinstance(value.get('orders'), dict):
            raise ValueError('주문은 orders 객체가 있는 스냅샷이어야 합니다.')
        if not all(isinstance(o, dict) for o in value['orders'].values()):
            raise ValueError('orders의 각 주문은 객체여야 합니다.')
        seq = value.get('seq')
        with self.lock:
            if seq is not None and seq == self.last_seq:
                return False
            if (from_poll and isinstance(seq, (int, float)) and isinstance(self.last_seq, (int, float))
                    and seq < self.last_seq):
                return False
            self.last_seq = seq
            self.latest_orders = value['orders']
            self.received_snapshot = True
            self.fault_latches.update(oid for oid, order in value['orders'].items() if order.get('fault'))
            self.queue.put(value)
        if self.on_accept:
            self.on_accept(seq, '조회' if from_poll else '알림')
        return True

    def begin_order(self, order_id):
        with self.lock:
            # 이미 큐에 쌓인 과거 동작을 뒤늦게 수행하지 않도록 최신 fault를 유지.
            if not self.latest_orders.get(order_id, {}).get('fault'):
                self.fault_latches.discard(order_id)

    def stopped(self, order_id):
        with self.lock:
            return (order_id in self.fault_latches
                    or (self.received_snapshot and order_id not in self.latest_orders))


def _parse_con(con):
    """CIN의 con -> dict. 문자열이면 JSON 파싱(이중 인코딩 대비 최대 2회), dict가 아니면 None."""
    for _ in range(2):
        if isinstance(con, str):
            try:
                con = json.loads(con)
            except ValueError:
                return None
    return con if isinstance(con, dict) else None


def notify_server(inbox, port):
    """Mobius cnt_process 구독 알림을 직접 받음 (구독 nu = http://<이 PC IP>:port/notify/process).
    단일 스레드 HTTPServer - 알림을 도착 순서대로 하나씩 처리해서 스냅샷 순서가 안 뒤집힘.
    받은 스냅샷은 inbox 큐에 넣기만 하고, 로봇은 메인 루프에서만 움직임."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != NOTIFY_PATH:
                self.send_response(404)
                self.end_headers()
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length) or b'{}')
            except ValueError:
                print('[알림 수신] JSON 파싱 실패 - 무시')
                data = {}
            # 응답 먼저 - 실제 처리는 메인 루프가 하니까 Mobius를 기다리게 할 이유 없음
            self.send_response(200)
            self.end_headers()

            sgn = data.get('m2m:sgn', {}) if isinstance(data, dict) else {}
            if sgn.get('vrq'):
                print('[알림 수신] Mobius 구독 확인 요청(vrq) - 응답 완료')
                return
            cin = sgn.get('nev', {}).get('rep', {}).get('m2m:cin')
            if not cin:
                return
            con = _parse_con(cin.get('con'))
            if con is None:
                print('[알림 수신] con이 JSON 객체가 아님 - 무시')
                return
            try:
                inbox.submit(con)
            except (ValueError, TypeError):
                print('[알림 수신] 지원하지 않는 주문 형식 - 무시')

        def log_message(self, fmt, *args):
            pass  # 요청마다 찍히는 기본 접속 로그 끔

    server = HTTPServer(('0.0.0.0', port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f'[알림 수신] Mobius 알림 대기: 0.0.0.0:{port}{NOTIFY_PATH}')
    return server
