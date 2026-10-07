import ast
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock
import zivpn_qos as qos


class QosPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/'accounts.db')
        with sqlite3.connect(self.path) as db:
            db.executescript('''
                CREATE TABLE clients (id INTEGER PRIMARY KEY,email TEXT UNIQUE,password TEXT,enable INTEGER,total_gb INTEGER,expiry_time INTEGER,created_at INTEGER,updated_at INTEGER);
                CREATE TABLE client_traffics (email TEXT UNIQUE,inbound_id INTEGER,enable INTEGER,up INTEGER,down INTEGER,expiry_time INTEGER,total INTEGER);
                CREATE TABLE client_inbounds (client_id INTEGER,inbound_id INTEGER,created_at INTEGER);
                CREATE TABLE inbounds (id INTEGER,tag TEXT);
                INSERT INTO inbounds VALUES (10,'inbound-zivpn'),(20,'inbound-zivpn-limited');
                INSERT INTO clients VALUES (1,'fixture','fixture-secret',1,999,888,0,0);
                INSERT INTO client_traffics VALUES ('fixture',10,1,123,456,888,999);
                INSERT INTO client_inbounds VALUES (1,10,0);
            ''')
    def test_numeric_validation(self):
        self.assertEqual(qos.speed_bytes('1'),1000000)
        self.assertEqual(qos.speed_bytes('0,25'),250000)
        for value in ('0','-1','NaN','inf','0.0000001','1e50','1e100000000','premium'):
            with self.subTest(value=value),self.assertRaises(ValueError):qos.speed_bytes(value)
    def test_defaults_overrides_and_restart(self):
        with sqlite3.connect(self.path) as db:
            self.assertEqual(qos.account_rate(db,'fixture',qos.STANDARD),1000000)
            qos.set_speed(db,'fixture','0.5')
            qos.set_speed(db,'standard','2',True)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(qos.account_rate(db,'fixture',qos.STANDARD),500000)
            self.assertEqual(qos.account_rate(db,'other',qos.STANDARD),2000000)
    def test_profile_change_preserves_counters_quotas_and_credentials(self):
        with sqlite3.connect(self.path) as db:
            qos.set_profile(db,'fixture','premium','7.5')
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT up,down,total,expiry_time,inbound_id FROM client_traffics').fetchone(),(123,456,999,888,20))
            self.assertEqual(db.execute('SELECT password,total_gb,expiry_time FROM clients').fetchone(),('fixture-secret',999,888))
            self.assertEqual(qos.account_rate(db,'fixture',qos.PREMIUM),7500000)
    def test_explicit_creation_and_duplicate_refusal(self):
        tree=ast.parse(Path(__file__).with_name('zivpn-xui-sync.py').read_text())
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='add_account')
        def get_db():
            db=sqlite3.connect(self.path);db.row_factory=sqlite3.Row;return db
        import csv,os,re
        g={'qos_policy':qos,'get_db':get_db,'time':time,'re':re,'os':os,'csv':csv,'CLIENTS_CSV':str(Path(self.tmp.name)/'clients.csv'),'sync_accounts':Mock(),'generate_ziv_config':Mock(return_value={})}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'create','exec'),g)
        g['add_account']('new-standard','new-secret','standard','9')
        with get_db() as db:
            self.assertEqual(db.execute("SELECT inbound_id FROM client_traffics WHERE email='new-standard'").fetchone()[0],10)
            self.assertEqual(qos.account_rate(db,'new-standard',qos.STANDARD),9000000)
        g['add_account']('new-premium','another-secret','premium','0.25')
        with get_db() as db:
            self.assertEqual(db.execute("SELECT inbound_id FROM client_traffics WHERE email='new-premium'").fetchone()[0],20)
            self.assertEqual(qos.account_rate(db,'new-premium',qos.PREMIUM),250000)
        with self.assertRaises(ValueError):g['add_account']('new-standard','changed','premium')
        with get_db() as db:
            self.assertEqual(db.execute("SELECT password FROM clients WHERE email='new-standard'").fetchone()[0],'new-secret')
