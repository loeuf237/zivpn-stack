#!/usr/bin/env python3
"""Root-only stdin/stdout identity bridge; credentials never enter argv."""
import json
import os
import sqlite3
import sys
sys.path.insert(0, os.environ.get('ZIVPN_NATIVE_LIB', '/usr/local/lib'))
from zivpn_policy import ACCOUNT_QUERY, access_denial, authenticate
import time


def account_id(row):
    return ('P:' if row['inbound_tag'] == 'inbound-zivpn-limited' else 'S:') + row['email']


def main():
    request = json.load(sys.stdin)
    db_path = os.environ.get('ZIVPN_NATIVE_DB', '/etc/x-ui/x-ui.db')
    if request.get('check'):
        now_ms = int(time.time() * 1000)
        with sqlite3.connect('file:' + db_path + '?mode=ro', uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            allowed = {}
            for identity in set(request['ids']):
                prefix, email = identity.split(':', 1)
                rows = conn.execute(ACCOUNT_QUERY + ' WHERE c.email=?', (email,)).fetchall()
                allowed[identity] = (len(rows) == 1 and account_id(rows[0]) == identity
                                     and access_denial(rows[0], now_ms) is None)
        print(json.dumps({'allowed': allowed}))
        return 0
    port = int(request['server_port'])
    identity = authenticate(request['addr'], request['auth'],
                            required_tag='inbound-zivpn-limited' if port == 5668 else None,
                            db_path=db_path, manage_ipset=False)
    if identity is None:
        return 1
    with sqlite3.connect('file:' + db_path + '?mode=ro', uri=True, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(ACCOUNT_QUERY + ' WHERE c.email=?', (identity,)).fetchall()
        if len(rows) != 1 or access_denial(rows[0], int(time.time()*1000)):
            return 1
        print(json.dumps({'id': account_id(rows[0])}))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (KeyError, ValueError, TypeError, OSError, sqlite3.Error):
        sys.exit(1)
