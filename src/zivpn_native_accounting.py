"""Authenticated, migration-safe telemetry from the root-only native socket."""
import fcntl
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import time

SOCKET_PATH = '/run/zivpn-native.sock'


class UnixHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCKET_PATH)


def request(path='/snapshot', payload=None):
    conn = UnixHTTPConnection('localhost', timeout=5)
    try:
        conn.request('GET' if payload is None else 'POST', path,
                     body=None if payload is None else json.dumps(payload),
                     headers={'Content-Type': 'application/json'})
        response = conn.getresponse()
        data = response.read(8*1024*1024+1)
        if response.status != 200 or len(data) > 8*1024*1024:
            raise ValueError('Invalid native API response')
        return json.loads(data)
    finally:
        conn.close()


def counter(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid counter')
    result = []
    for key in ('up', 'down'):
        number = value[key]
        if type(number) is not int or not 0 <= number <= 2**63-1:
            raise ValueError('Invalid byte count')
        result.append(number)
    return tuple(result)


def snapshot():
    data = request()
    if not re.fullmatch('[a-f0-9]{32}', data['epoch']):
        raise ValueError('Invalid epoch')
    account_totals = [0, 0]
    server_totals = [0, 0]
    for email, value in data['accounts'].items():
        if not isinstance(email, str) or not email:
            raise ValueError('Invalid identity')
        for i, number in enumerate(counter(value)):
            account_totals[i] += number
    for port, value in data['servers'].items():
        if port not in ('5667', '5668'):
            raise ValueError('Invalid listener')
        for i, number in enumerate(counter(value)):
            server_totals[i] += number
    if account_totals != server_totals:
        raise ValueError('Inconsistent native totals')
    for sid, value in data['sessions'].items():
        ipaddress.ip_address(value['ip'])
        counter(value)
        if (not re.fullmatch('[a-f0-9]{32}', sid) or not value['email']
                or value['port'] not in (5667, 5668)
                or type(value['remote_port']) is not int
                or not 0 <= value['remote_port'] <= 65535
                or type(value['vip']) is not bool):
            raise ValueError('Invalid authenticated session')
    return data


def live_connections():
    data = snapshot()
    return {sid: {'email': row['email'], 'ip': row['ip'],
                  'remote_port': row['remote_port'], 'port': row['port'],
                  'vip': row['vip'], 'bytes': row['up']+row['down'],
                  'packets': 0, 'label': str(row['port'])}
            for sid, row in data['sessions'].items()}


def apply_snapshot(conn, data, now_ms):
    from zivpn_accounting import ensure_schema, PORT_TAGS
    ensure_schema(conn)
    conn.execute('''CREATE TABLE IF NOT EXISTS zivpn_native_checkpoints (
        epoch TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
        up INTEGER NOT NULL, down INTEGER NOT NULL,
        PRIMARY KEY(epoch,kind,name))''')
    conn.execute('BEGIN IMMEDIATE')
    meta = dict(conn.execute('SELECT key,value FROM zivpn_accounting_meta'))
    epoch = data['epoch']
    attributed = [0, 0]
    unattributed = [0, 0]
    totals = [0, 0]
    for kind, items in (('account', data['accounts']), ('server', data['servers'])):
        for name, current in items.items():
            up, down = counter(current)
            old = conn.execute('SELECT up,down FROM zivpn_native_checkpoints '
                               'WHERE epoch=? AND kind=? AND name=?',
                               (epoch, kind, name)).fetchone()
            previous = old or (0, 0)
            delta = (up-previous[0], down-previous[1])
            if min(delta) < 0:
                raise ValueError('Native counters regressed within one epoch')
            if kind == 'account':
                affected = conn.execute('UPDATE client_traffics SET up=COALESCE(up,0)+?, '
                                        'down=COALESCE(down,0)+? WHERE email=?', (*delta, name)).rowcount
                sums = attributed if affected else unattributed
                if affected and sum(delta):
                    # Short sessions may close before the next sample.
                    conn.execute('UPDATE client_traffics SET last_online=MAX(COALESCE(last_online,0),?) '
                                 'WHERE email=?', (now_ms, name))
                for i in (0, 1):
                    sums[i] += delta[i]
            else:
                conn.execute('UPDATE inbounds SET up=COALESCE(up,0)+?, '
                             'down=COALESCE(down,0)+? WHERE tag=?',
                             (*delta, PORT_TAGS[int(name)]))
                for i in (0, 1):
                    totals[i] += delta[i]
            conn.execute('INSERT INTO zivpn_native_checkpoints VALUES (?,?,?,?,?) '
                         'ON CONFLICT(epoch,kind,name) DO UPDATE SET up=excluded.up,down=excluded.down',
                         (epoch, kind, name, up, down))
    for email in {row['email'] for row in data['sessions'].values()}:
        conn.execute('UPDATE client_traffics SET last_online=? WHERE email=?', (now_ms, email))
    values = {'last_sample_ms': now_ms,
              'last_interval_ms': max(0, now_ms-int(meta.get('last_sample_ms', now_ms))),
              'last_delta_up': totals[0], 'last_delta_down': totals[1],
              'measurement_started_ms': meta.get('measurement_started_ms', now_ms),
              'measurement_mode': 'authenticated_payload',
              'unattributed_up': int(meta.get('unattributed_up', 0))+unattributed[0],
              'unattributed_down': int(meta.get('unattributed_down', 0))+unattributed[1]}
    for key, value in values.items():
        conn.execute('INSERT INTO zivpn_accounting_meta VALUES (?,?) '
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))
    return {'up': totals[0], 'down': totals[1],
            'attributed_up': attributed[0], 'attributed_down': attributed[1]}


def sync_traffic(db_path):
    with open(db_path+'.zivpn-accounting.lock', 'a+') as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = snapshot()  # Network read outside the SQLite transaction.
        with sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=rw', uri=True, timeout=20) as conn:
            return apply_snapshot(conn, data, int(time.time()*1000))


def kick(target):
    return int(request('/kick', {'target': target})['closed'])
