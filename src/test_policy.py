import ast
import concurrent.futures
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import zivpn_policy as policy


class AccountPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = str(self.root / "accounts.db")
        with sqlite3.connect(self.db) as conn:
            conn.executescript("""
                CREATE TABLE clients (id INTEGER PRIMARY KEY, email TEXT,
                    password TEXT, enable INTEGER, total_gb INTEGER,
                    expiry_time INTEGER, updated_at INTEGER);
                CREATE TABLE client_traffics (email TEXT, inbound_id INTEGER,
                    enable INTEGER, total INTEGER, expiry_time INTEGER,
                    up INTEGER, down INTEGER);
                CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT,
                    enable INTEGER, expiry_time INTEGER);
                INSERT INTO inbounds VALUES (1, 'inbound-zivpn', 1, 0);
                INSERT INTO inbounds VALUES (2, 'inbound-zivpn-limited', 1, 0);
                INSERT INTO clients VALUES (1, 'test-user', 'fixture-secret', 1, 0, 0, 0);
                INSERT INTO client_traffics VALUES ('test-user', 1, 1, 0, 0, 0, 0);
            """)
        mock = patch.object(policy.subprocess, "run")
        self.runner = mock.start()
        self.addCleanup(mock.stop)
        self.now = 1_800_000_000_000

    def sql(self, query, args=()):
        with sqlite3.connect(self.db) as conn:
            return conn.execute(query, args).fetchall()

    def login(self, password="fixture-secret", required_tag=None, now=None):
        return policy.authenticate("203.0.113.10:12345", password,
                                   required_tag=required_tag, db_path=self.db,
                                   now_ms=self.now if now is None else now)

    def test_diagnostic_refusal_is_distinct_from_database_error(self):
        self.assertEqual(policy.authenticate('203.0.113.1:1', 'unknown', db_path=self.db, diagnostic=True),
                         {'ok': False, 'reason': 'invalid_credentials'})
        with patch.object(policy.sqlite3, 'connect', side_effect=sqlite3.OperationalError('private details')):
            self.assertEqual(policy.authenticate('203.0.113.1:1', 'fixture-secret', db_path=self.db, diagnostic=True),
                             {'ok': False, 'reason': 'database_error'})
        self.sql('UPDATE clients SET enable=0')
        self.assertEqual(policy.authenticate('203.0.113.1:1', 'fixture-secret', db_path=self.db, diagnostic=True)['reason'], 'disabled')

    def test_unlimited_account_allowed(self):
        self.assertEqual(self.login(), "test-user")

    def test_expiry_boundaries_on_client_traffic_and_inbound(self):
        for table in ("clients", "client_traffics", "inbounds"):
            for delta in (-1, 0, 1):
                with self.subTest(table=table, delta=delta):
                    self.sql(f"UPDATE {table} SET expiry_time = ?", (self.now + delta,))
                    self.assertEqual(self.login(), "test-user" if delta > 0 else None)
                self.sql(f"UPDATE {table} SET expiry_time = 0")

    def test_disabled_at_any_level_denied(self):
        for table in ("clients", "client_traffics", "inbounds"):
            with self.subTest(table=table):
                self.sql(f"UPDATE {table} SET enable = 0")
                self.assertIsNone(self.login())
                self.sql(f"UPDATE {table} SET enable = 1")

    def test_byte_quotas_and_upload_download_sum(self):
        for table, field in (("clients", "total_gb"), ("client_traffics", "total")):
            self.sql(f"UPDATE {table} SET {field} = 100")
            for down in (59, 60, 61):
                with self.subTest(table=table, down=down):
                    self.sql("UPDATE client_traffics SET up=40, down=?", (down,))
                    self.assertEqual(self.login(), "test-user" if down < 60 else None)
            self.sql(f"UPDATE {table} SET {field} = 0")

    def test_unknown_empty_or_sql_injection_password_denied(self):
        for password in ("", "unknown", "' OR 1=1 --"):
            self.assertIsNone(self.login(password))

    def test_duplicate_password_denied(self):
        self.sql("INSERT INTO clients VALUES (2,'other','fixture-secret',1,0,0,0)")
        self.sql("INSERT INTO client_traffics VALUES ('other',1,1,0,0,0,0)")
        self.assertIsNone(self.login())

    def test_vip_requires_vip_assignment_but_vip_can_use_standard(self):
        self.assertIsNone(self.login(required_tag="inbound-zivpn-limited"))
        self.sql("UPDATE client_traffics SET inbound_id = 2")
        self.assertEqual(self.login(required_tag="inbound-zivpn-limited"), "test-user")
        self.assertEqual(self.login(), "test-user")

    def test_unrelated_inbound_denied(self):
        self.sql("UPDATE inbounds SET tag='unrelated'")
        self.assertIsNone(self.login())

    def test_first_login_activates_delayed_expiry_once(self):
        duration = 86_400_000
        self.sql("UPDATE clients SET expiry_time = ?", (-duration,))
        self.sql("UPDATE client_traffics SET expiry_time = ?", (-duration,))
        self.assertEqual(self.login(), "test-user")
        self.assertEqual(self.sql("SELECT expiry_time FROM clients"), [(self.now + duration,)])
        self.assertEqual(self.sql("SELECT expiry_time FROM client_traffics"), [(self.now + duration,)])
        self.assertEqual(self.login(now=self.now + 1000), "test-user")
        self.assertEqual(self.sql("SELECT expiry_time FROM clients"), [(self.now + duration,)])
        self.assertIsNone(self.login(now=self.now + duration))

    def test_rejected_login_does_not_start_delayed_expiry(self):
        self.sql("UPDATE clients SET expiry_time=-1000, total_gb=10")
        self.sql("UPDATE client_traffics SET down=10")
        self.assertIsNone(self.login())
        self.assertEqual(self.sql("SELECT expiry_time FROM clients"), [(-1000,)])

    def test_concurrent_first_logins_do_not_extend_expiry(self):
        self.sql("UPDATE clients SET expiry_time=-10000")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda n: self.login(now=self.now+n), (0, 100)))
        self.assertEqual(results, ["test-user", "test-user"])
        expiry = self.sql("SELECT expiry_time FROM clients")[0][0]
        self.assertIn(expiry, (self.now+10000, self.now+10100))

    def test_database_failure_denied(self):
        self.assertIsNone(policy.authenticate("203.0.113.10:12345", "fixture-secret",
                                              db_path=str(self.root), now_ms=self.now))
        missing = self.root / "missing.db"
        self.assertIsNone(policy.authenticate("203.0.113.10:12345", "fixture-secret",
                                              db_path=str(missing), now_ms=self.now))
        self.assertFalse(missing.exists())

    def test_renewed_account_can_reconnect(self):
        self.sql("UPDATE clients SET expiry_time=?", (self.now-1,))
        self.assertIsNone(self.login())
        self.sql("UPDATE clients SET expiry_time=?", (self.now+10000,))
        self.assertEqual(self.login(), "test-user")

    def test_server_four_argument_call_records_correct_port(self):
        with patch.object(policy, "authenticate", return_value=None) as login:
            self.assertEqual(policy.main(["auth", "203.0.113.10:12345", "fixture-secret", "500000"]), 1)
            login.assert_called_once_with("203.0.113.10:12345", "fixture-secret", required_tag=None, server_port=5667)
        with patch.object(policy, "authenticate", return_value=None) as login:
            policy.main(["auth", "203.0.113.10:12345", "fixture-secret", "500000"],
                        required_tag="inbound-zivpn-limited", server_port=5668)
            self.assertEqual(login.call_args.kwargs["server_port"], 5668)

    def test_telemetry_failure_does_not_reject_valid_login(self):
        import subprocess
        self.sql("UPDATE clients SET expiry_time=-1000")
        with patch.object(policy, "record_session", side_effect=subprocess.CalledProcessError(1, "conntrack")):
            self.assertEqual(policy.authenticate("203.0.113.10:12345", "fixture-secret",
                                                  db_path=self.db, now_ms=self.now, server_port=5667), "test-user")
        self.assertEqual(self.sql("SELECT expiry_time FROM clients"), [(self.now+1000,)])

    def test_sync_removes_expired_password_without_reactivating(self):
        source = (Path(__file__).parent / "zivpn-xui-sync.py").read_text()
        node = next(n for n in ast.parse(source).body
                    if isinstance(n, ast.FunctionDef) and n.name == "sync_accounts")
        standard, vip = self.root / "standard.json", self.root / "vip.json"
        for file, passwords in ((standard, ["fixture-secret"]), (vip, [])):
            file.write_text(json.dumps({"auth": {"mode": "command", "config": passwords}}))
        self.sql("UPDATE client_traffics SET expiry_time=?", (self.now-1,))

        def connect():
            conn = sqlite3.connect(self.db)
            conn.row_factory = sqlite3.Row
            return conn

        import os
        env = {"get_db": connect, "ACCOUNT_QUERY": policy.ACCOUNT_QUERY,
               "native_qos_enabled": lambda: False,
               "access_denial": policy.access_denial, "time": time, "os": os,
               "subprocess": policy.subprocess, "CONFIG_FILE": str(standard),
               "CONFIG_LIMITED_FILE": str(vip), "send_telegram": lambda *a, **kw: None,
               "read_json_config": lambda p: json.loads(Path(p).read_text()),
               "write_json_config": lambda p, data: Path(p).write_text(json.dumps(data))}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<sync-test>", "exec"), env)
        with patch.object(time, "time", return_value=self.now/1000):
            env["sync_accounts"]()
        self.assertEqual(json.loads(standard.read_text())["auth"]["config"], [])
        self.runner.assert_called_once_with(["systemctl", "reload-or-restart", "zivpn"], capture_output=True, timeout=10)
        self.assertIsNone(self.login())
        # A renewed quota/date can restore access without altering enable flags.
        self.sql("UPDATE client_traffics SET expiry_time=?", (self.now+1000,))
        with patch.object(time, "time", return_value=self.now/1000):
            env["sync_accounts"]()
        self.assertEqual(json.loads(standard.read_text())["auth"]["config"], ["fixture-secret"])


if __name__ == "__main__":
    unittest.main()
