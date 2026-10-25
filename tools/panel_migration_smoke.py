#!/usr/bin/env python3
"""Check migration against the real checksum-pinned panel in a temporary folder.

Runs the vendor's offline settings command only; never installs or starts services.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile

from download import archive
from install import ROOT, seed_database
from lifecycle import import_legacy_accounts, validate_database


def main():
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}[platform.machine()]
    lock = json.loads((ROOT / 'config/artifacts.json').read_text())
    with tempfile.TemporaryDirectory(prefix='zivpn-panel-migration-') as directory:
        root = Path(directory)
        archive(f"https://github.com/MHSanaei/3x-ui/releases/download/v{lock['xui_version']}/x-ui-linux-{arch}.tar.gz",
                lock['xui'][arch], root / 'package')
        database = root / 'db'
        database.mkdir(mode=0o700)
        subprocess.run([str(root / 'package/x-ui/x-ui'), 'setting', '-port', '2053',
                        '-listenIP', '127.0.0.1', '-webBasePath', '/fixture-path/'],
                       env=dict(os.environ, XUI_DB_FOLDER=str(database)),
                       check=True, capture_output=True, timeout=60)
        path = database / 'x-ui.db'
        seed_database(path, {'primary_admin_id': 123},
                      {'panel_username': 'fixture-admin', 'panel_password': 'fixture-password-for-panel-only'})
        import_legacy_accounts(path, ['fixture-legacy-one', 'fixture-legacy-two', 'fixture-legacy-one'], 'standard')
        validate_database(path)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE clients SET total_gb=10000,expiry_time=2000000000000 WHERE email='legacy-0001'")
            db.execute("UPDATE client_traffics SET up=17,down=23,total=10000 WHERE email='legacy-0001'")
            before = db.execute('SELECT * FROM clients ORDER BY id').fetchall()
            traffic = db.execute('SELECT * FROM client_traffics ORDER BY email').fetchall()
        import_legacy_accounts(path, ['fixture-legacy-one', 'fixture-legacy-two'], 'premium')
        with closing(sqlite3.connect(path)) as db:
            assert db.execute('SELECT * FROM clients ORDER BY id').fetchall() == before
            assert db.execute('SELECT * FROM client_traffics ORDER BY email').fetchall() == traffic
            assert len(before) == 2
        for password, expected in [('fixture-legacy-one', True), ('fixture-incorrect', False)]:
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/zivpn-native-auth.py')],
                                    input=json.dumps({'addr': '203.0.113.1:12345', 'auth': password, 'server_port': 5667}),
                                    env=dict(os.environ, ZIVPN_NATIVE_DB=str(path), ZIVPN_NATIVE_LIB=str(ROOT / 'src')),
                                    text=True, capture_output=True, check=True, timeout=15)
            assert json.loads(result.stdout).get('ok') is expected
        print('Pinned panel migration passed: schema, deduplicated import, credentials, quota/counter preservation and authentication.')


if __name__ == '__main__':
    main()
