import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import threading
import time

import pymysql

from .config import read_config
from .store import Store


@contextmanager
def connection(config):
    db = pymysql.connect(**config['database'], charset='utf8mb4', autocommit=False,
                         cursorclass=pymysql.cursors.DictCursor, connect_timeout=5,
                         read_timeout=config.get('readTimeout', 30), write_timeout=30,
                         init_command="SET time_zone = '+00:00', max_statement_time=10")
    try:
        with db.cursor() as cursor:
            cursor.execute('SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED')
        yield db
        db.commit()
    except Exception:
        try: db.rollback()
        except pymysql.Error: logging.warning('Database connection ended before rollback could be confirmed')
        raise
    finally:
        db.close()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, config):
        self.config = config
        self.slots = threading.BoundedSemaphore(64)
        super().__init__((config.get('bind', '127.0.0.1'), config.get('port', 8769)), Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        super().process_request(request, address)

    def process_request_thread(self, request, address):
        try: super().process_request_thread(request, address)
        finally: self.slots.release()

    def rpc(self, token, action, params):
        with connection(self.config) as db:
            store = Store(db, self.config['adminTokenHash'])
            auth = store.authenticate(token)
            return store.dispatch(auth, action, params)


class Handler(BaseHTTPRequestHandler):
    server_version = 'AgentMessaging/0.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(65)

    def respond(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try: self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError): pass

    def do_GET(self):
        if self.path != '/health':
            self.respond(404, {'error': 'Not found'}); return
        try:
            with connection(self.server.config) as db:
                with db.cursor() as cursor: cursor.execute('SELECT 1')
            self.respond(200, {'status': 'good'})
        except Exception:
            self.respond(503, {'status': 'unavailable'})

    def do_POST(self):
        if self.path != '/v1/rpc':
            self.respond(404, {'error': 'Not found'}); return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2 * 1024 * 1024: raise ValueError('Request size must be 1–2097152 bytes')
            authorization = self.headers.get('Authorization', '')
            if not authorization.startswith('Bearer '): raise PermissionError('Authentication required')
            token = authorization[7:]
            data = json.loads(self.rfile.read(length))
            action, params = data['action'], data.get('params', {})
            if not isinstance(params, dict): raise ValueError('Parameters must be an object')
            if action == 'watch':
                duration = params.pop('timeout', 55)
                if not isinstance(duration, (int, float)) or not 0 <= duration <= 55: raise ValueError('Watch timeout must be 0–55 seconds')
                end = time.monotonic() + duration
                while True:
                    result = self.server.rpc(token, 'inbox', params)
                    if result['messages'] or time.monotonic() >= end: break
                    time.sleep(min(1, max(0, end - time.monotonic())))
            else:
                result = self.server.rpc(token, action, params)
            self.respond(200, result)
        except PermissionError as error:
            self.respond(403, {'error': str(error)})
        except (ValueError, KeyError, TypeError) as error:
            self.respond(400, {'error': str(error)})
        except pymysql.IntegrityError:
            self.respond(409, {'error': 'Conflicting identity, recipient project, or message reference'})
        except Exception:
            logging.exception('Messaging request failed')
            self.respond(503, {'error': 'Service could not complete the request'})

    def log_message(self, *args):
        # Message content and authentication headers stay out of access logs.
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--initialize', action='store_true')
    args = parser.parse_args()
    config = read_config(args.config)
    if args.initialize:
        with connection(config) as db:
            with db.cursor() as cursor:
                for statement in Path(__file__).with_name('schema.sql').read_text().split(';'):
                    if statement.strip(): cursor.execute(statement)
        return
    with connection(config) as db: Store(db, config['adminTokenHash']).recover()
    Server(config).serve_forever(poll_interval=0.5)


if __name__ == '__main__':
    main()
