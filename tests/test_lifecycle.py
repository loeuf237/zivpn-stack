"""Exercise real temporary files/databases with simulated systemd, never production."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import install
from lifecycle import (Backup, Plan, Updater, UpdateFailure, deployment_lock,
                       import_legacy_accounts, legacy_nat_rules, plan_installation)
from preflight import Report


def database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript('''
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password TEXT);
            INSERT INTO users VALUES (1,'existing-admin','unchanged-hash');
            CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT, enable INTEGER,
                expiry_time INTEGER, up INTEGER, down INTEGER);
            INSERT INTO inbounds VALUES (1,'inbound-zivpn',1,0,17,23);
            INSERT INTO inbounds VALUES (2,'inbound-zivpn-limited',1,0,0,0);
            CREATE TABLE clients (id INTEGER PRIMARY KEY, email TEXT UNIQUE,
                password TEXT, enable INTEGER, total_gb INTEGER, expiry_time INTEGER,
                created_at INTEGER, updated_at INTEGER);
            CREATE TABLE client_traffics (email TEXT UNIQUE, inbound_id INTEGER,
                enable INTEGER, up INTEGER, down INTEGER, total INTEGER, expiry_time INTEGER);
            CREATE TABLE client_inbounds (client_id INTEGER, inbound_id INTEGER, created_at INTEGER);
            INSERT INTO clients VALUES (1,'existing-account','fixture-original',0,1000,2000000000000,1,1);
            INSERT INTO client_traffics VALUES ('existing-account',1,0,17,23,1000,2000000000000);
            INSERT INTO client_inbounds VALUES (1,1,1);
            CREATE TABLE zivpn_qos_defaults (profile TEXT PRIMARY KEY, rate INTEGER);
            INSERT INTO zivpn_qos_defaults VALUES ('standard',2000000);
            INSERT INTO zivpn_qos_defaults VALUES ('premium',5000000);
            CREATE TABLE zivpn_qos_accounts (email TEXT PRIMARY KEY, rate INTEGER);
            INSERT INTO zivpn_qos_accounts VALUES ('existing-account',3500000);
        ''')


class Host:
    def __init__(self, root, mode='update'):
        self.root = root
        self.commands = []
        self.active = {'zivpn.service'}
        if mode == 'update':
            self.active.update({'x-ui.service', 'zivpn-xui-sync.service', 'zivpn-ports.service'})
        self.enabled = set(self.active)
        self.original = set(self.active)
        self.firewall = '*nat\n-A PREROUTING -i eth0 -p udp -m udp --dport 6000:19999 -j DNAT --to-destination :5667\nCOMMIT\n'

    def run(self, args, check=True, **kwargs):
        self.commands.append((args, kwargs))
        output, code = '', 0
        if args[0] == 'systemctl':
            action = args[1]
            units = [x for x in args[2:] if x.endswith('.service')]
            if action == 'show':
                unit = units[0]
                output = 'loaded' if (unit in self.original or (self.root / 'etc/systemd/system' / unit).exists()) else 'not-found'
            elif action == 'is-active':
                output = 'active' if units[0] in self.active else 'inactive'
                code = 0 if units[0] in self.active else 3
            elif action == 'is-enabled':
                output = 'enabled' if units[0] in self.enabled else 'disabled'
            elif action == 'stop':
                self.active.difference_update(units)
            elif action == 'start':
                self.active.update(units)
            elif action == 'restart':
                self.active.update(units)
            elif action == 'enable':
                self.enabled.update(units)
            elif action == 'disable':
                self.enabled.difference_update(units)
        elif args[0] == 'iptables-save':
            output = self.firewall
        if check and code:
            raise subprocess.CalledProcessError(code, args)
        return subprocess.CompletedProcess(args, code, stdout=output, stderr='')


class SimulatedUpdater(Updater):
    """Use the real file switch, backup and rollback; simulate only provisioning/health."""
    fail_health = False
    add_traffic_before_failure = False

    def stage(self, arch, config, private, profile):
        self.workspace.mkdir(parents=True, mode=0o700)
        self.states_before_update()
        self.venv = self.root / 'opt/zivpn/releases/tested/venv'
        self.venv.mkdir(parents=True)
        staged = self.workspace / 'new-runtime'
        staged.write_text('new-runtime')
        self.staged_files = [(staged, '/usr/local/bin/zivpn-native', 0o700)]
        for unit in ('x-ui.service', 'zivpn.service', 'zivpn-xui-sync.service', 'zivpn-ports.service'):
            unitfile = self.workspace / unit
            unitfile.write_text('new-unit')
            if self.plan.mode == 'migrate' or unit not in ('x-ui.service', 'zivpn-ports.service'):
                self.staged_files.append((unitfile, '/etc/systemd/system/' + unit, 0o644))
        if self.plan.mode == 'migrate':
            self.panel = self.workspace / 'panel'
            self.panel.mkdir()
            (self.panel / 'x-ui').write_text('new-panel')
            self.database = self.workspace / 'database'
            database(self.database / 'x-ui.db')
            import_legacy_accounts(self.database / 'x-ui.db', self.plan.passwords, profile)

    def validate_runtime(self):
        if self.add_traffic_before_failure:
            with closing(sqlite3.connect(self.root / 'etc/x-ui/x-ui.db')) as db, db:
                db.execute("UPDATE client_traffics SET up=up+10 WHERE email='existing-account'")
        if self.fail_health:
            raise RuntimeError('simulated failure with no production connections')


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.write('etc/zivpn/config.json', json.dumps({'listen': ':5667', 'cert': '/etc/zivpn/zivpn.crt', 'key': '/etc/zivpn/zivpn.key'}))
        self.write('etc/zivpn/zivpn.crt', 'fixture-certificate')
        self.write('etc/zivpn/zivpn.key', 'fixture-key')

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        return path

    def existing_stack(self):
        database(self.root / 'etc/x-ui/x-ui.db')
        self.write('etc/zivpn/settings.json', '{"server_address":"vpn.example.com","primary_admin_id":123}')
        self.write('etc/zivpn/telegram.token', 'fixture-telegram-token')
        self.write('etc/zivpn/clients.csv', 'private-existing-export')
        self.write('usr/local/bin/zivpn-native', 'old-runtime')
        self.write('opt/zivpn/venv/keep', 'old-venv')
        self.write('etc/systemd/system/x-ui.service', 'existing-custom-panel-unit')
        self.write('etc/systemd/system/zivpn-ports.service', 'existing-network-unit')
        return plan_installation(Report(conflicts=['existing or partial installation']), self.root, validate_tls=False)

    def legacy(self):
        cfg = {'listen': ':5667', 'cert': '/etc/zivpn/zivpn.crt', 'key': '/etc/zivpn/zivpn.key',
               'obfs': 'zivpn', 'auth': {'mode': 'passwords', 'config': ['fixture-legacy', 'fixture-legacy']}}
        self.write('etc/zivpn/config.json', json.dumps(cfg))
        self.write('usr/local/bin/zivpn', 'old-legacy-binary')
        self.write('etc/systemd/system/zivpn.service', 'old-legacy-unit')
        return plan_installation(Report(conflicts=['legacy installation']), self.root, validate_tls=False)

    def rows(self):
        with closing(sqlite3.connect(self.root / 'etc/x-ui/x-ui.db')) as db:
            return {table: db.execute('SELECT * FROM ' + table).fetchall()
                    for table in ('users', 'clients', 'client_traffics', 'inbounds', 'zivpn_qos_defaults', 'zivpn_qos_accounts')}

    def test_supported_existing_stack_updates_instead_of_refusing(self):
        plan = self.existing_stack()
        self.assertEqual(plan.mode, 'update')
        before = self.rows()
        config_before = {p.name: p.read_bytes() for p in (self.root / 'etc/zivpn').iterdir()}
        host = Host(self.root)
        updater = SimulatedUpdater(plan, self.root, self.root, host.run)
        backup = updater.apply('amd64', {}, None)
        self.assertEqual(self.rows(), before)
        for name, data in config_before.items():
            self.assertEqual((self.root / 'etc/zivpn' / name).read_bytes(), data)
        self.assertEqual((self.root / 'etc/systemd/system/x-ui.service').read_text(), 'existing-custom-panel-unit')
        self.assertEqual((self.root / 'etc/systemd/system/zivpn-ports.service').read_text(), 'existing-network-unit')
        self.assertEqual((self.root / 'usr/local/bin/zivpn-native').read_text(), 'new-runtime')
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads((backup / 'journal.json').read_text())['state'], 'committed')
        self.assertFalse(any(cmd[0].startswith('iptables') for cmd, _ in host.commands))
        commands = [cmd for cmd, _ in host.commands]
        flush = commands.index(['/usr/local/sbin/zivpn-native-save-traffic'])
        stop = commands.index(['systemctl', 'stop', 'zivpn.service'])
        self.assertLess(flush, stop)

    def test_failed_update_restores_runtime_without_rewinding_counters(self):
        plan = self.existing_stack()
        host = Host(self.root)
        updater = SimulatedUpdater(plan, self.root, self.root, host.run)
        updater.fail_health = updater.add_traffic_before_failure = True
        before = self.rows()
        with self.assertRaisesRegex(UpdateFailure, 'previous runtime was restored'):
            updater.apply('amd64', {}, None)
        self.assertEqual((self.root / 'usr/local/bin/zivpn-native').read_text(), 'old-runtime')
        self.assertEqual((self.root / 'opt/zivpn/venv/keep').read_text(), 'old-venv')
        after = self.rows()
        self.assertEqual(after['users'], before['users'])
        self.assertEqual(after['clients'], before['clients'])
        self.assertEqual(after['client_traffics'][0][3], before['client_traffics'][0][3] + 10)
        self.assertEqual(host.active, host.original)
        self.assertEqual(json.loads((updater.backup.directory / 'journal.json').read_text())['state'], 'rolled-back')

    def test_legacy_migration_keeps_passwords_certificates_and_retires_old_service(self):
        plan = self.legacy()
        self.assertEqual(plan.mode, 'migrate')
        self.assertEqual(len(plan.passwords), 1)
        self.assertNotIn('fixture-legacy', repr(plan))
        cfg = (self.root / 'etc/zivpn/config.json').read_bytes()
        host = Host(self.root, 'migrate')
        updater = SimulatedUpdater(plan, self.root, self.root, host.run)
        updater.apply('amd64', {'server_address': 'vpn.example.com', 'primary_admin_id': 123}, {'telegram_token': 'fixture-new-token'}, 'premium')
        self.assertEqual((self.root / 'etc/zivpn/config.json').read_bytes(), cfg)
        self.assertEqual((self.root / 'etc/zivpn/zivpn.key').read_text(), 'fixture-key')
        with closing(sqlite3.connect(self.root / 'etc/x-ui/x-ui.db')) as db:
            self.assertEqual(db.execute("SELECT password FROM clients WHERE email='legacy-0001'").fetchone(), ('fixture-legacy',))
            self.assertEqual(db.execute("SELECT inbound_id FROM client_traffics WHERE email='legacy-0001'").fetchone(), (2,))
        deletes = [cmd for cmd, _ in host.commands if cmd[0] == 'iptables']
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0][5:7], ['-D', 'PREROUTING'])
        self.assertEqual((self.root / 'usr/local/bin/zivpn').read_text(), 'old-legacy-binary')

    def test_failed_legacy_migration_restores_old_config_service_and_firewall(self):
        plan = self.legacy()
        host = Host(self.root, 'migrate')
        updater = SimulatedUpdater(plan, self.root, self.root, host.run)
        updater.fail_health = True
        with self.assertRaises(UpdateFailure):
            updater.apply('amd64', {}, {'telegram_token': 'fixture-new-token'})
        self.assertFalse((self.root / 'etc/x-ui/x-ui.db').exists())
        self.assertFalse((self.root / 'etc/zivpn/telegram.token').exists())
        self.assertEqual((self.root / 'etc/systemd/system/zivpn.service').read_text(), 'old-legacy-unit')
        self.assertEqual(host.active, host.original)
        self.assertTrue(any(cmd[0] == 'iptables-restore' and kw['input'] == host.firewall for cmd, kw in host.commands))

    def test_import_is_idempotent_and_does_not_reset_existing_accounts(self):
        self.existing_stack()
        path = self.root / 'etc/x-ui/x-ui.db'
        before = self.rows()
        import_legacy_accounts(path, ['fixture-original', 'fixture-new', 'fixture-new'], 'standard')
        first = self.rows()
        import_legacy_accounts(path, ['fixture-original', 'fixture-new'], 'standard')
        self.assertEqual(self.rows(), first)
        self.assertEqual(first['clients'][0], before['clients'][0])
        self.assertEqual(first['client_traffics'][0], before['client_traffics'][0])

    def test_unrelated_ports_and_docker_do_not_trigger_host_replacement(self):
        self.existing_stack()
        cases = [Report(ports=[('udp', 5667, '123')]),
                 Report(conflicts=['existing ZiVPN in Docker container abc123']),
                 Report(foreign_nat=True)]
        for report in cases:
            with self.subTest(report=report), self.assertRaises(SystemExit):
                plan_installation(report, self.root, validate_tls=False)

    def test_socket_ownership_allows_the_existing_zivpn_listener(self):
        self.existing_stack()
        fd = self.root / 'proc/123/fd/4'
        fd.parent.mkdir(parents=True)
        fd.symlink_to('socket:[999]')
        self.write('proc/123/cgroup', '0::/system.slice/zivpn.service\n')
        report = Report(process_ids={'123'}, ports=[('udp', 5667, '999')])
        self.assertEqual(plan_installation(report, self.root, validate_tls=False).mode, 'update')

    def test_unknown_auth_and_broken_metadata_are_not_overwritten(self):
        self.write('etc/zivpn/config.json', '{"auth":{"mode":"http"}}')
        with self.assertRaises(SystemExit):
            plan_installation(Report(), self.root, validate_tls=False)

    def test_deployment_lock_prevents_two_installers_from_racing(self):
        with deployment_lock(self.root), self.assertRaisesRegex(SystemExit, 'already running'):
            with deployment_lock(self.root):
                self.fail('Concurrent installer entered the switch phase')

    def test_only_known_legacy_nat_rule_is_removed(self):
        text = '*nat\n-A PREROUTING -p udp --dport 6000:19999 -j DNAT --to-destination 192.0.2.1:5667\nCOMMIT\n'
        self.assertEqual(legacy_nat_rules(text), [])

    def test_preparation_failure_leaves_the_previous_runtime_running(self):
        plan = self.existing_stack()
        host = Host(self.root)
        source = self.root / 'checkout'
        source.mkdir()
        binary = source / 'staged-native'
        binary.write_text('prepared-new-runtime')

        def run(args, **kwargs):
            if args[0].endswith('/bin/pip'):
                raise subprocess.CalledProcessError(1, args)
            return host.run(args, **kwargs)

        updater = Updater(plan, source, self.root, run)
        with patch('install.runtime_files', return_value=[(binary, '/usr/local/bin/zivpn-native', 0o700)]):
            with self.assertRaisesRegex(UpdateFailure, 'existing services were not stopped'):
                updater.apply('amd64', {})
        self.assertEqual((self.root / 'usr/local/bin/zivpn-native').read_text(), 'old-runtime')
        self.assertEqual(host.active, host.original)
        self.assertFalse(any(cmd[:2] == ['systemctl', 'stop'] for cmd, _ in host.commands))

    def test_failed_counter_flush_aborts_the_switch_and_restarts_the_collector(self):
        plan = self.existing_stack()
        host = Host(self.root)

        def run(args, **kwargs):
            if args == ['/usr/local/sbin/zivpn-native-save-traffic']:
                raise subprocess.CalledProcessError(1, args)
            return host.run(args, **kwargs)

        updater = SimulatedUpdater(plan, self.root, self.root, run)
        before = self.rows()
        with self.assertRaises(UpdateFailure):
            updater.apply('amd64', {})
        self.assertEqual((self.root / 'usr/local/bin/zivpn-native').read_text(), 'old-runtime')
        self.assertEqual(self.rows(), before)
        self.assertEqual(host.active, host.original)

    def test_read_only_update_plan_does_not_construct_an_updater(self):
        args = type('Args', (), dict(check=True, settings=None, secrets=None, legacy_profile='standard'))()
        with patch('install.assess_host', return_value=Report()), \
                patch('install.plan_installation', return_value=Plan('update')), \
                patch('install.load_settings', return_value={'primary_admin_id': 123}), \
                patch('install.Updater') as updater, patch('install.run') as run:
            install.deploy(args, 'amd64')
            updater.assert_not_called()
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
