import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('expire', Path(__file__).with_name('zivpn-expire-account.py'))
expire = importlib.util.module_from_spec(spec)
spec.loader.exec_module(expire)


class ExpirationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name)/'test.db')
        with sqlite3.connect(self.db) as c:
            c.executescript('''
                CREATE TABLE clients(id INTEGER PRIMARY KEY,email TEXT,password TEXT,enable INTEGER,expiry_time INTEGER,updated_at INTEGER);
                CREATE TABLE client_traffics(email TEXT,inbound_id INTEGER,enable INTEGER,expiry_time INTEGER,up INTEGER,down INTEGER);
                CREATE TABLE inbounds(id INTEGER PRIMARY KEY,tag TEXT);
                INSERT INTO inbounds VALUES(1,'inbound-zivpn'),(2,'inbound-zivpn-limited');
                INSERT INTO clients VALUES(1,'standard','std-secret',1,0,0),(2,'premium','vip-secret',1,0,0);
                INSERT INTO client_traffics VALUES('standard',1,1,0,10,20),('premium',2,1,0,100,200);
            ''')

    def rows(self):
        with sqlite3.connect(self.db) as c:
            return c.execute('SELECT c.email,c.enable,ct.enable,c.expiry_time,ct.expiry_time,ct.up,ct.down '
                             'FROM clients c JOIN client_traffics ct ON c.email=ct.email ORDER BY c.id').fetchall()

    def test_target_only_and_history_preserved(self):
        self.assertEqual(expire.expire_account('premium',self.db,now_ms=1234),'premium')
        self.assertEqual(self.rows(),[('standard',1,1,0,0,10,20),('premium',0,0,1234,1234,100,200)])

    def test_dry_run_never_changes_account(self):
        before=self.rows()
        self.assertEqual(expire.expire_account('premium',self.db,now_ms=1234,dry_run=True),'premium')
        self.assertEqual(self.rows(),before)

    def test_unknown_target_changes_nothing(self):
        before=self.rows()
        self.assertIsNone(expire.expire_account('missing',self.db,now_ms=1234))
        self.assertEqual(self.rows(),before)

    def test_unique_legacy_password_works(self):
        self.assertEqual(expire.expire_account('vip-secret',self.db,now_ms=1234),'premium')

    def test_ambiguous_password_changes_nothing(self):
        with sqlite3.connect(self.db) as c:c.execute("UPDATE clients SET password='same-secret'")
        before=self.rows()
        with self.assertRaises(ValueError):expire.expire_account('same-secret',self.db,now_ms=1234)
        self.assertEqual(self.rows(),before)


if __name__ == '__main__':
    unittest.main()
