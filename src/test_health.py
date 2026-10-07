import ast
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from zivpn_health import Monitor, bucket_rates


def health(epoch='a', seq=1, auth=None):
    return {'epoch': epoch, 'started_ms': 1000, 'observed_ms': 100000,
            'auth': auth or {'accepted': 1}, 'close_sequence': seq,
            'buckets': {'S:203.0.113.1': {'up': 0, 'down': 0, 'sessions': 4,
                'limit_bytes_per_second': 500000, 'wait_calls': 0, 'delayed_calls': 0}},
            'closes': [{'sequence': seq, 'closed_ms': 100000, 'email': 'fixture', 'ip': '203.0.113.1',
                'lifetime_seconds': 35, 'last_payload_ms': 70000, 'reason': 'idle_timeout', 'up': 1, 'down': 10,
                'transport': {'rtt_ms': 120, 'sent_packets': 100, 'acked_packets': 80, 'lost_packets': 5, 'pto_count': 1}}]}


class HealthTests(unittest.TestCase):
    def test_rates_and_epoch_or_bucket_reset(self):
        a, b = health(), health()
        b['buckets']['S:203.0.113.1'].update(down=2500000, wait_calls=100, delayed_calls=60)
        row = bucket_rates(a, b, 5)[0]
        self.assertEqual(row['utilization'], 1)
        self.assertEqual(row['sessions'], 4)
        self.assertEqual(row['delayed_percent'], 60)
        self.assertFalse(bucket_rates(b, a, 5))
        b['buckets']['S:203.0.113.1']['generation'] = 2
        self.assertFalse(bucket_rates(a, b, 5))
        b['epoch'] = 'b'
        self.assertFalse(bucket_rates(a, b, 5))

    @patch('zivpn_health.kernel_counters', return_value={'udp_InErrors': 0, 'conntrack_count': 10, 'conntrack_max': 100})
    @patch('zivpn_health.subprocess.run', return_value=Mock(stdout='active\nactive\nactive\n'))
    def test_dedup_persist_retention_and_no_secret(self, *_):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/'health.db')
            native = Mock(); native.request.return_value = health(); native.snapshot.return_value = {'epoch':'a','sessions':{}}
            transport = Mock(); transport.snapshot.return_value = {}
            monitor = Monitor(path)
            monitor.collect(native, transport, now=100)
            monitor.collect(native, transport, now=161)
            monitor = Monitor(path)
            monitor.collect(native, transport, now=222)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM closes').fetchone()[0],1)
            report = monitor.report(now=223)
            self.assertIn('idle_timeout=1', report)
            self.assertIn('Historique de mesures disponible : 0.0 h', report)
            self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
            native.request.return_value = dict(health(seq=2), closes=[])
            monitor.collect(native, transport, now=700000)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM closes').fetchone()[0],0)

    @patch('zivpn_health.kernel_counters', return_value={'udp_InErrors': 0, 'conntrack_count': 10, 'conntrack_max': 100})
    @patch('zivpn_health.subprocess.run', return_value=Mock(stdout='active\nactive\nactive\n'))
    def test_internal_error_alert_and_counter_window(self, *_):
        with tempfile.TemporaryDirectory() as directory:
            monitor = Monitor(str(Path(directory)/'health.db'))
            native, transport = Mock(), Mock()
            native.request.return_value = health(); native.snapshot.return_value = {'epoch':'a','sessions':{}}
            transport.snapshot.return_value = {}
            self.assertFalse(monitor.collect(native,transport,now=2000))
            native.request.return_value = health(auth={'accepted':2,'database_error':3})
            notices = monitor.collect(native,transport,now=2061)
            self.assertTrue(any('database_error' in notice for notice in notices))
            self.assertIn('database_error=3', monitor.report(now=2062))
            native.request.return_value = health(auth={'accepted':3,'database_error':7})
            self.assertFalse(monitor.collect(native,transport,now=2122))
            monitor = Monitor(str(Path(directory)/'health.db'))
            native.request.return_value = health(auth={'accepted':4,'database_error':11})
            self.assertFalse(monitor.collect(native,transport,now=2183))

    def test_sensitive_commands_and_buttons_require_primary_private(self):
        tree = ast.parse(Path(__file__).with_name('zivpn-xui-sync.py').read_text())
        nodes = [node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in ('handle_telegram_command','handle_telegram_callback')]
        self.assertEqual(len(nodes),2)
        g = {'PRIMARY_ADMIN_ID':7,'send_telegram':Mock(),'answer_callback':Mock(),'get_admin_ids':lambda:{7,8}}
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'guard','exec'),g)
        for command in ('/sante','/qualite','/add','/profil','/vitesse','/limit'):
            for chat,user,kind in ((8,8,'private'),(-100,7,'group'),(8,7,'private')):
                g['send_telegram'].reset_mock()
                g['handle_telegram_command'](chat,user,command,kind)
                self.assertIn('réservée',g['send_telegram'].call_args.args[0])
        for button in ('menu_health','menu_quality','account_create_standard','account_create_premium'):
            g['answer_callback'].reset_mock()
            g['handle_telegram_callback']({'id':'x','data':button,'from':{'id':8},'message':{'chat':{'id':8,'type':'private'}}})
            self.assertIn('réservé',g['answer_callback'].call_args.args[1])
