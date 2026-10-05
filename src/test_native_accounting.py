import copy
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import zivpn_native_accounting as native


class NativeAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name)/'test.db')
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE client_traffics(email TEXT PRIMARY KEY, up INTEGER, down INTEGER, last_online INTEGER);
            CREATE TABLE inbounds(tag TEXT PRIMARY KEY, up INTEGER, down INTEGER);
            INSERT INTO client_traffics VALUES ('premium',100,200,0);
            INSERT INTO client_traffics VALUES ('standard',0,0,0);
            INSERT INTO inbounds VALUES ('inbound-zivpn',0,0);
            INSERT INTO inbounds VALUES ('inbound-zivpn-limited',0,0);
        ''')
        self.data = {'epoch': 'a'*32,
                     'accounts': {'premium': {'up': 10, 'down': 100}, 'standard': {'up': 5, 'down': 50}},
                     'servers': {'5667': {'up': 15, 'down': 150}},
                     'sessions': {'b'*32: {'email': 'premium', 'ip': '203.0.113.1',
                                 'remote_port': 1234, 'port': 5667, 'vip': True,
                                 'up': 10, 'down': 100, 'authenticated_ms': 1000}}}

    def apply(self, data=None):
        with self.conn:
            return native.apply_snapshot(self.conn, data or self.data, 2000)

    def counters(self):
        return self.conn.execute('SELECT up,down FROM client_traffics WHERE email="premium"').fetchone()

    def test_preserves_history_and_does_not_recount(self):
        self.apply()
        self.assertEqual(self.counters(), (110, 300))
        second = self.apply()
        self.assertEqual(second['down'], 0)
        self.assertEqual(self.counters(), (110, 300))

    def test_process_restart_adds_new_epoch_once(self):
        self.apply()
        changed = copy.deepcopy(self.data)
        changed['epoch'] = 'c'*32
        self.apply(changed)
        self.assertEqual(self.counters(), (120, 400))
        self.apply(changed)
        self.assertEqual(self.counters(), (120, 400))

    def test_regression_rolls_back_whole_snapshot(self):
        self.apply()
        changed = copy.deepcopy(self.data)
        changed['accounts']['premium']['down'] = 110
        changed['accounts']['standard']['up'] = 0
        with self.assertRaises(ValueError):
            self.apply(changed)
        self.assertEqual(self.counters(), (110, 300))

    def test_missing_account_is_unattributed_without_guessing_ip(self):
        self.conn.execute('DELETE FROM client_traffics WHERE email="premium"')
        self.conn.commit()
        result = self.apply()
        self.assertEqual(result['attributed_down'], 50)
        value = self.conn.execute("SELECT value FROM zivpn_accounting_meta WHERE key='unattributed_down'").fetchone()
        self.assertEqual(value, ('100',))

    def test_live_view_uses_confirmed_identity(self):
        with patch.object(native, 'request', return_value=self.data):
            row = list(native.live_connections().values())[0]
        self.assertEqual((row['email'], row['vip'], row['bytes']), ('premium', True, 110))

    def test_short_disconnected_session_updates_last_online(self):
        changed = copy.deepcopy(self.data)
        changed['sessions'] = {}
        self.apply(changed)
        self.assertEqual(self.conn.execute('SELECT last_online FROM client_traffics '
                                          'WHERE email="premium"').fetchone(), (2000,))

    def test_invalid_snapshot_rejected_before_database_write(self):
        for mutate in [lambda d: d['servers']['5667'].update(down=1),
                       lambda d: d['accounts']['premium'].update(up=-1),
                       lambda d: d['sessions']['b'*32].update(ip='invalid')]:
            changed = copy.deepcopy(self.data)
            mutate(changed)
            with patch.object(native, 'request', return_value=changed), self.assertRaises(ValueError):
                native.snapshot()

    def test_api_failure_never_falls_back_to_conntrack(self):
        import zivpn_accounting as accounting
        with patch.object(accounting, 'native_enabled', return_value=True), \
             patch.object(accounting, 'read_flows') as kernel, \
             patch.object(native, 'request', side_effect=OSError('unavailable')):
            with self.assertRaises(OSError):
                accounting.sync_traffic(self.db)
            kernel.assert_not_called()
        self.assertEqual(self.counters(), (100, 200))


if __name__ == '__main__':
    unittest.main()
