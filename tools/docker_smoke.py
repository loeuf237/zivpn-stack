#!/usr/bin/env python3
"""Exercise the built image with temporary, fictitious account data only."""
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time


def docker(*args, input=None, check=True):
    return subprocess.run(
        ['docker', *args], input=input, text=True, capture_output=True,
        check=check, timeout=90,
    )


def fixtures(root):
    config = root / 'config'
    database = root / 'db'
    config.mkdir()
    database.mkdir()
    with sqlite3.connect(database / 'x-ui.db') as db:
        db.executescript("""
            CREATE TABLE clients (id INTEGER PRIMARY KEY, email TEXT,
                password TEXT, enable INTEGER, total_gb INTEGER,
                expiry_time INTEGER, updated_at INTEGER);
            CREATE TABLE client_traffics (email TEXT, inbound_id INTEGER,
                enable INTEGER, total INTEGER, expiry_time INTEGER,
                up INTEGER, down INTEGER, last_online INTEGER);
            CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT,
                enable INTEGER, expiry_time INTEGER, up INTEGER, down INTEGER);
            INSERT INTO inbounds VALUES (1, 'inbound-zivpn', 1, 0, 0, 0);
            INSERT INTO clients VALUES (1, 'smoke-user', 'fixture-password', 1, 0, 0, 0);
            INSERT INTO client_traffics VALUES ('smoke-user', 1, 1, 0, 0, 0, 0, 0);
        """)
    subprocess.run([
        'openssl', 'req', '-x509', '-nodes', '-newkey', 'rsa:2048',
        '-keyout', str(config / 'zivpn.key'), '-out', str(config / 'zivpn.crt'),
        '-days', '1', '-subj', '/CN=localhost',
    ], check=True, capture_output=True, timeout=30)
    (config / 'config.json').write_text(json.dumps({
        'listen': ':5667', 'cert': '/etc/zivpn/zivpn.crt',
        'key': '/etc/zivpn/zivpn.key',
    }))
    for path in config.iterdir():
        path.chmod(0o600)
    (database / 'x-ui.db').chmod(0o600)
    return config, database


def main(image):
    name = 'zivpn-smoke-' + secrets.token_hex(6)
    with tempfile.TemporaryDirectory(prefix='zivpn-smoke-') as directory:
        config, database = fixtures(Path(directory))
        try:
            # Fixtures belong to the host CI user. Make only these disposable
            # files root-owned before testing a runtime with all caps dropped.
            docker('run', '--rm', '--network', 'none', '--entrypoint', 'python3',
                   '--mount', f'type=bind,src={directory},dst=/fixture', image,
                   '-c', "import os,pathlib; "
                   "[os.chown(p,0,0) for p in pathlib.Path('/fixture').rglob('*') if p.is_file()]")
            docker('run', '-d', '--name', name, '--network', 'none',
                   '--read-only', '--cap-drop', 'ALL',
                   '--security-opt', 'no-new-privileges:true',
                   '--tmpfs', '/run', '--tmpfs', '/tmp',
                   '--mount', f'type=bind,src={config},dst=/etc/zivpn,readonly',
                   '--mount', f'type=bind,src={database},dst=/etc/x-ui', image)
            deadline = time.monotonic() + 40
            while True:
                probe = docker('exec', name, 'python3',
                               '/usr/local/lib/zivpn_container_healthcheck.py', check=False)
                if probe.returncode == 0:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('Container failed its startup health probe')
                state = docker('inspect', '--format', '{{.State.Running}}', name)
                if state.stdout.strip() != 'true':
                    raise RuntimeError('Native server exited during startup')
                time.sleep(1)
            for password, expected in [('fixture-password', True), ('incorrect', False)]:
                result = docker('exec', '-i', name, '/usr/local/sbin/zivpn-native-auth',
                                input=json.dumps({'addr': '203.0.113.10:12345',
                                                  'auth': password, 'server_port': 5667}))
                data = json.loads(result.stdout)
                if data.get('ok') is not expected:
                    raise RuntimeError('Unexpected authentication decision')
                if expected and (data.get('id') != 'S:smoke-user' or data.get('rate') != 1000000):
                    raise RuntimeError('Standard identity or bandwidth policy changed')
            docker('stop', '--time', '45', name)
            status = docker('inspect', '--format', '{{.State.ExitCode}}', name)
            if status.stdout.strip() != '0':
                raise RuntimeError('Native server did not stop cleanly')
            docker('run', '--rm', '--network', 'none', '--cap-drop', 'ALL',
                   '--entrypoint', 'python3',
                   '--mount', f'type=bind,src={database},dst=/db,readonly', image,
                   '-c', "import sqlite3; "
                   "db=sqlite3.connect('file:/db/x-ui.db?mode=ro',uri=True); "
                   "meta=dict(db.execute('SELECT key,value FROM zivpn_accounting_meta')); "
                   "assert meta.get('measurement_mode')=='authenticated_payload'; "
                   "assert int(meta.get('last_sample_ms',0))>0")
            print('Docker smoke passed: startup, policy allow/deny, health, shutdown accounting.')
        except Exception:
            logs = docker('logs', name, check=False)
            print(logs.stdout + logs.stderr, file=sys.stderr)
            raise
        finally:
            docker('rm', '-f', name, check=False)
            # Restore ownership so the host test driver can inspect/clean files.
            docker('run', '--rm', '--network', 'none', '--entrypoint', 'python3',
                   '--mount', f'type=bind,src={directory},dst=/fixture', image,
                   '-c', "import os,pathlib; "
                   "root=pathlib.Path('/fixture'); owner=root.stat(); "
                   "[os.chown(p,owner.st_uid,owner.st_gid) for p in root.rglob('*')]",
                   check=False)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python3 tools/docker_smoke.py IMAGE')
    main(sys.argv[1])
