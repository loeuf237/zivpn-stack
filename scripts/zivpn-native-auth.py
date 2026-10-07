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
    result = authenticate(request['addr'], request['auth'],
                          required_tag='inbound-zivpn-limited' if port == 5668 else None,
                          db_path=db_path, manage_ipset=False, diagnostic=True)
    if not result['ok']:
        print(json.dumps(result))
        return 0
    with sqlite3.connect('file:' + db_path + '?mode=ro', uri=True, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(ACCOUNT_QUERY + ' WHERE c.email=?', (result['email'],)).fetchall()
        denial = access_denial(rows[0], int(time.time()*1000)) if len(rows) == 1 else 'invalid_credentials'
        if denial:
            print(json.dumps({'ok': False, 'reason': denial}))
        else:
            print(json.dumps({'ok': True, 'reason': 'accepted', 'id': account_id(rows[0])}))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except sqlite3.Error:
        print(json.dumps({"ok": False, "reason": "database_error"}))
        sys.exit(0)
    except (KeyError, ValueError, TypeError, OSError):
        print(json.dumps({"ok": False, "reason": "internal_error"}))
        sys.exit(0)
