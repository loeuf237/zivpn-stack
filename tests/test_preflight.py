"""Fresh-install conflicts must never trigger destructive cleanup or setup."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from contextlib import nullcontext

import install
from preflight import firewall_conflict, foreign_nat, inspect_host, assess_host


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.write('proc/net/udp', 'header\n')
        self.write('proc/net/tcp', 'header\n')
        self.outputs = {}
        self.available = {'iptables-save'}
        self.commands = []

    def write(self, name, text=''):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def runner(self, command, **kwargs):
        self.commands.append(command)
        result = self.outputs.get(command[0], '')
        if isinstance(result, Exception):
            raise result
        return subprocess.CompletedProcess(command, 0, stdout=result)

    def inspect(self):
        return inspect_host(self.root, self.runner, lambda name: name if name in self.available else None)

    def socket_row(self, protocol, port, state='07'):
        self.write('proc/net/' + protocol, f'header\n 0: 00000000:{port:04X} 00000000:0000 {state} 0 0\n')

    def test_clean_host_is_read_only(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        report = self.inspect()
        report.require_clean()
        self.assertEqual(report.conflicts, [])
        self.assertEqual(report.errors, [])
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')))
        self.assertEqual({cmd[0] for cmd in self.commands}, {'systemctl', 'iptables-save'})
        self.assertIn('list-units', self.commands[0])

    def test_legacy_script_and_partial_installations_are_preserved(self):
        for name in ('usr/local/bin/zivpn', 'usr/local/bin/zivpn-native',
                     'etc/zivpn/zivpn.key', 'etc/x-ui/x-ui.db',
                     'opt/zivpn/venv/partial', 'usr/local/lib/zivpn_sqlite.py'):
            with self.subTest(name=name):
                target = self.write(name, 'private-fixture-value')
                report = self.inspect()
                with self.assertRaises(SystemExit) as error:
                    report.require_clean()
                self.assertNotIn('private-fixture-value', str(error.exception))
                self.assertEqual(target.read_text(), 'private-fixture-value')
                target.unlink()

    def test_stopped_masked_and_orphaned_units_block_installation(self):
        self.write('etc/systemd/system/zivpn_backfill.service')
        link = self.root / 'etc/systemd/system/multi-user.target.wants/zivpn.service'
        link.parent.mkdir(parents=True)
        link.symlink_to('/missing-zivpn-unit')
        self.outputs['systemctl'] = 'zivpn-port-forward.service loaded inactive dead Legacy aliases\n'
        report = self.inspect()
        self.assertTrue(any('zivpn_backfill.service' in c for c in report.conflicts))
        self.assertTrue(any('target.wants/zivpn.service' in c for c in report.conflicts))
        self.assertTrue(any('loaded systemd unit zivpn-port-forward.service' in c for c in report.conflicts))
        self.assertTrue(link.is_symlink())
        self.assertFalse(any('stop' in cmd or 'disable' in cmd for cmd in self.commands))

    def test_broken_reserved_symlink_blocks_installation(self):
        path = self.root / 'etc/zivpn'
        path.parent.mkdir()
        path.symlink_to('/missing-old-zivpn')
        self.assertTrue(any('/etc/zivpn' in c for c in self.inspect().conflicts))

    def test_native_and_legacy_processes_are_detected_without_reading_argv(self):
        for pid, name in ((100, 'zivpn'), (101, 'zivpn-native'), (102, 'udp-zivpn-linux'), (103, 'x-ui')):
            self.write(f'proc/{pid}/comm', name + '\n')
            self.write(f'proc/{pid}/cmdline', 'private-argv-must-not-be-read')
        report = self.inspect()
        self.assertEqual(len(report.conflicts), 4)
        self.assertNotIn('private-argv', str(report))

    def test_conflicting_ipv4_ipv6_listeners_and_udp_aliases(self):
        for protocol, port, state in (('udp', 5667, '07'), ('udp6', 5668, '07'),
                                      ('udp', 17000, '01'), ('udp', 443, '07'),
                                      ('tcp', 2053, '0A'), ('tcp6', 56670, '0A')):
            with self.subTest(protocol=protocol, port=port):
                self.socket_row(protocol, port, state)
                self.assertTrue(any(f'{port} is already bound' in c for c in self.inspect().conflicts))
                self.write('proc/net/' + protocol, 'header\n')

    def test_unrelated_sockets_do_not_block(self):
        self.socket_row('udp', 5353)
        self.socket_row('tcp', 22, '0A')
        self.socket_row('tcp6', 2053, '01')  # Outgoing TCP session, not a listener.
        self.inspect().require_clean()

    def test_legacy_nat_rule_survives_removed_binary_and_service(self):
        self.outputs['iptables-save'] = ('*nat\n-A PREROUTING -i eth0 -p udp '
                                         '--dport 6000:19999 -j DNAT --to-destination :5667\nCOMMIT\n')
        report = self.inspect()
        self.assertTrue(any('NAT' in c for c in report.conflicts))
        with self.assertRaises(SystemExit):
            report.require_clean()

    def test_persistent_and_nft_included_rules_are_checked(self):
        self.write('etc/nftables.conf', 'include "/etc/custom/*.nft"\n')
        self.write('etc/custom/old.nft', 'udp dport { 6000-19999 } redirect to :5667\n')
        self.write('etc/iptables/rules.v4', ':ZIVPN-UDP - [0:0]\n')
        conflicts = self.inspect().conflicts
        self.assertTrue(any('/etc/custom/old.nft' in c for c in conflicts))
        self.assertTrue(any('/etc/iptables/rules.v4' in c for c in conflicts))

    def test_firewall_detection_ignores_unrelated_accept_and_dns_nat(self):
        self.assertFalse(firewall_conflict('-A INPUT -p udp --dport 5667 -j ACCEPT'))
        self.assertFalse(firewall_conflict('udp dport 53 redirect to :5353'))
        self.assertTrue(firewall_conflict('udp dport 12345 dnat to 192.0.2.1:9999'))
        self.assertTrue(firewall_conflict('-A PREROUTING -p udp --dport 50001 -j DNAT --to-destination 192.0.2.1:5667'))
        self.assertFalse(foreign_nat('udp dport 6000-19999 counter packets 2 bytes 100 dnat to :5667'))
        self.assertTrue(foreign_nat('udp dport 6000-19999 dnat to 192.0.2.1:5667'))

    def test_missing_failed_or_unreadable_checks_fail_closed(self):
        cases = ('missing-firewall-tool', 'firewall-error', 'service-timeout', 'missing-sockets')
        for case in cases:
            with self.subTest(case=case):
                self.outputs.clear()
                self.available = {'iptables-save'}
                self.write('proc/net/udp', 'header\n')
                if case == 'missing-firewall-tool':
                    self.available.clear()
                elif case == 'firewall-error':
                    self.outputs['iptables-save'] = subprocess.CalledProcessError(1, 'iptables-save', stderr='private-error')
                elif case == 'service-timeout':
                    self.outputs['systemctl'] = subprocess.TimeoutExpired('systemctl', 15)
                else:
                    (self.root / 'proc/net/udp').unlink()
                report = self.inspect()
                self.assertTrue(report.errors)
                with self.assertRaises(SystemExit) as error:
                    report.require_clean()
                self.assertNotIn('private-error', str(error.exception))

    def test_stopped_zivpn_container_and_published_conflicting_ports(self):
        self.available.add('docker')
        self.outputs['docker'] = ('abc123\tzivpn-stack:local\told-native\t\n'
                                  'def456\tother:latest\tunrelated\t0.0.0.0:5667->9000/udp\n')
        report = self.inspect()
        self.assertTrue(any('container abc123' in c for c in report.conflicts))
        self.assertTrue(any('container def456' in c for c in report.conflicts))
        self.outputs['docker'] = subprocess.CalledProcessError(1, 'docker')
        self.assertTrue(self.inspect().errors)

    def test_non_root_audit_is_refused(self):
        with patch('preflight.os.geteuid', return_value=1000), patch('preflight.inspect_host') as inspect:
            with self.assertRaisesRegex(SystemExit, 'as root'):
                assess_host()
            inspect.assert_not_called()

    def test_install_and_check_stop_before_private_inputs_or_mutations(self):
        for check in ([], ['--check']):
            with self.subTest(check=check), \
                    patch('sys.argv', ['install.py', '--settings', '/private/settings.json',
                                       '--secrets', '/private/secrets.json', *check]), \
                    patch('install.platform.machine', return_value='x86_64'), \
                    patch('install.os.geteuid', return_value=0), \
                    patch('install.deployment_lock', return_value=nullcontext()), \
                    patch('install.Path.exists', return_value=True), \
                    patch('install.Path.read_text', return_value='ID=ubuntu\n'), \
                    patch('install.assess_host', side_effect=SystemExit('Unrelated service conflict')), \
                    patch('install.load_settings') as settings, \
                    patch('install.validate_private') as private, \
                    patch('install.run') as run, patch('install.install_file') as write:
                with self.assertRaisesRegex(SystemExit, 'Unrelated service conflict'):
                    install.main()
                settings.assert_not_called()
                private.assert_not_called()
                run.assert_not_called()
                write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
