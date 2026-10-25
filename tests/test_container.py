"""Check socket integration and the disposable Docker fixture schema."""
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

from docker_smoke import fixtures
from zivpn_native_accounting import apply_snapshot


class ContainerIntegrationTests(unittest.TestCase):
    def test_collector_uses_configured_private_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'native.sock')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(path)
                server.listen(1)
                server.settimeout(10)
                requests = []

                def reply():
                    with server.accept()[0] as client:
                        requests.append(client.recv(4096))
                        body = b'{"ok":true}'
                        client.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n' + body)

                worker = threading.Thread(target=reply, daemon=True)
                worker.start()
                result = subprocess.run(
                    [sys.executable, '-c',
                     "from zivpn_native_accounting import request; "
                     "import json; print(json.dumps(request('/health')))"],
                    env=dict(os.environ, ZIVPN_NATIVE_SOCKET=path),
                    text=True, capture_output=True, timeout=10, check=True,
                )
                worker.join(timeout=10)
                self.assertEqual(json.loads(result.stdout), {'ok': True})
                self.assertTrue(requests[0].startswith(b'GET /health HTTP/1.1'))

    def test_smoke_database_supports_final_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            _, database = fixtures(Path(directory))
            with sqlite3.connect(database / 'x-ui.db') as db:
                sample = {'epoch': 'a' * 32, 'accounts': {'smoke-user': {'up': 12, 'down': 34}},
                          'servers': {'5667': {'up': 12, 'down': 34}}, 'sessions': {}}
                apply_snapshot(db, sample, 1800000000000)
                self.assertEqual(db.execute('SELECT up,down FROM client_traffics').fetchone(), (12, 34))
                self.assertEqual(db.execute('SELECT up,down FROM inbounds').fetchone(), (12, 34))
