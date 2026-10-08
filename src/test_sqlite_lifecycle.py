"""Regression checks for exhaustion of descriptors by repeated SQLite work."""
import gc
from pathlib import Path
import sqlite3
import tempfile
import unittest

from zivpn_health import Monitor
from zivpn_sqlite import ClosingConnection


class SQLiteLifecycleTests(unittest.TestCase):
    def test_transaction_commit_and_rollback_close_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'accounts.db')
            with sqlite3.connect(path, factory=ClosingConnection) as db:
                db.execute('CREATE TABLE accounts (name TEXT)')
                db.execute("INSERT INTO accounts VALUES ('fixture')")
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute('SELECT 1')
            with self.assertRaisesRegex(ValueError, 'rollback'):
                with sqlite3.connect(path, factory=ClosingConnection) as failed:
                    failed.execute("INSERT INTO accounts VALUES ('discarded')")
                    raise ValueError('rollback')
            with self.assertRaises(sqlite3.ProgrammingError):
                failed.execute('SELECT 1')
            with sqlite3.connect(path, factory=ClosingConnection) as check:
                self.assertEqual(check.execute('SELECT name FROM accounts').fetchall(), [('fixture',)])

    @unittest.skipUnless(Path('/proc/self/fd').exists(), 'Linux descriptor check')
    def test_repeated_health_reads_do_not_depend_on_garbage_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = Monitor(str(Path(directory) / 'health.db'))
            before = len(list(Path('/proc/self/fd').iterdir()))
            enabled = gc.isenabled()
            gc.disable()
            retained = []
            try:
                for _ in range(500):
                    with monitor.connect() as db:
                        db.execute('SELECT COUNT(*) FROM samples').fetchone()
                    retained.append(db)
                self.assertLessEqual(len(list(Path('/proc/self/fd').iterdir())), before + 1)
            finally:
                for db in retained:
                    db.close()
                if enabled:
                    gc.enable()


if __name__ == '__main__':
    unittest.main()
