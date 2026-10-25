#!/usr/bin/env python3
"""Read-only conflict checks for a fresh ZiVPN Stack installation.

Never stops services, removes files, reads credentials, or changes firewall rules.
"""
from dataclasses import dataclass, field
import fnmatch
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

UDP_PORTS = {80, 443, 5666, 5667, 5668, 8080, 8443, 8888}
TCP_PORTS = {2053, 56670, 56680}
SERVICE_PATTERNS = ('zivpn*.service', 'udp-zivpn*.service', 'x-ui*.service')
RESERVED_PATHS = (
    '/etc/zivpn', '/etc/x-ui', '/opt/zivpn', '/usr/local/x-ui',
    '/var/lib/zivpn-telegram', '/run/zivpn-native.sock', '/run/zivpn-container',
)
PATH_PATTERNS = (
    '/usr/local/bin/*zivpn*', '/usr/bin/*zivpn*',
    '/usr/local/sbin/zivpn*', '/usr/local/lib/zivpn_*.py',
)
UNIT_DIRS = ('/etc/systemd/system', '/run/systemd/system',
             '/usr/lib/systemd/system', '/lib/systemd/system')
FIREWALL_FILES = (
    '/etc/iptables/rules.v4', '/etc/iptables/rules.v6',
    '/etc/ufw/before.rules', '/etc/ufw/after.rules', '/etc/ufw/user.rules',
    '/etc/ufw/before6.rules', '/etc/ufw/after6.rules', '/etc/ufw/user6.rules',
    '/etc/nftables.conf',
)


@dataclass
class Report:
    conflicts: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    process_ids: set = field(default_factory=set, repr=False)
    ports: list = field(default_factory=list, repr=False)
    foreign_nat: bool = False

    def require_clean(self):
        if self.conflicts or self.errors:
            details = ['Deployment could not be planned safely; no installation changes were made.']
            details += ['Conflict: ' + item for item in sorted(set(self.conflicts))]
            details += ['Not verified: ' + item for item in sorted(set(self.errors))]
            details.append('Supported ZiVPN installations are updated or migrated automatically. '
                           'Review these unrecognized conflicts or failed checks; nothing is removed automatically.')
            raise SystemExit('\n'.join(details))


def udp_overlap(first, last=None):
    last = first if last is None else last
    return (any(first <= port <= last for port in UDP_PORTS)
            or (first <= 30000 and last >= 6000))


def port_expression_overlaps(expression):
    for first, last in re.findall(r'(\d+)(?:[:-](\d+))?', expression):
        if udp_overlap(int(first), int(last) if last else None):
            return True
    return False


def firewall_conflict(text):
    """Recognize owned chains and overlapping UDP DNAT/REDIRECT rules."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if re.search(r'zivpn|BLOCK-P2P', line, re.IGNORECASE):
            return True
        if not re.search(r'\b(?:DNAT|REDIRECT)\b', line, re.IGNORECASE):
            continue
        if not re.search(r'\budp\b', line, re.IGNORECASE):
            continue
        # iptables-save and numeric nft list ruleset output.
        match = re.search(r'--dports?\s+([\d,:-]+)', line)
        if match is None:
            match = re.search(r'\budp\s+dport\s+(\{[^}]+\}|[\d,-]+)', line)
        if match and port_expression_overlaps(match.group(1)):
            return True
        # Also catch differently named rules redirecting to the native listener.
        if re.search(r'(?:--to-(?:destination|ports)\s+|\b(?:dnat|redirect)\s+to\s+)'
                     r'(?:\S*?:)?566[78](?:\s|$)', line, re.IGNORECASE):
            return True
    return False


def foreign_nat(text):
    for line in text.splitlines():
        if not firewall_conflict(line):
            continue
        if re.search(r'zivpn|BLOCK-P2P', line, re.IGNORECASE):
            continue
        if ('--dport 6000:19999' in line and '--to-destination :5667' in line
                and '-j DNAT' in line and '-p udp' in line):
            continue
        if (re.search(r'\budp\s+dport\s+6000-19999\b', line)
                and re.search(r'\bdnat\s+to\s+:5667(?:\s|$)', line)):
            continue  # nft rendering of the same iptables-nft legacy rule.
        return True
    return False


def query(command, runner):
    try:
        return runner(command, check=True, capture_output=True, text=True,
                      timeout=15, env=dict(os.environ, LC_ALL='C')).stdout
    except (OSError, subprocess.SubprocessError) as error:
        # Do not include command output: it may contain private host details.
        raise RuntimeError(f'{Path(command[0]).name} failed ({type(error).__name__})') from None


def scan_paths(root, report):
    for name in RESERVED_PATHS:
        try:
            (root / name.lstrip('/')).lstat()
        except FileNotFoundError:
            continue
        report.conflicts.append('existing or partial installation at ' + name)
    for pattern in PATH_PATTERNS:
        for path in root.glob(pattern.lstrip('/')):
            report.conflicts.append('installed ZiVPN component at /' + str(path.relative_to(root)))
    for directory in UNIT_DIRS:
        for pattern in SERVICE_PATTERNS:
            for path in (root / directory.lstrip('/')).glob('**/' + pattern):
                report.conflicts.append('systemd unit or enablement link at /' + str(path.relative_to(root)))


def scan_units(runner, report):
    output = query(['systemctl', 'list-units', '--all', '--type=service',
                    '--no-legend', '--plain', '--no-pager'], runner)
    for line in output.splitlines():
        fields = line.split()
        if fields and any(fnmatch.fnmatchcase(fields[0], pattern) for pattern in SERVICE_PATTERNS):
            report.conflicts.append('loaded systemd unit ' + fields[0])


def scan_processes(root, report):
    for path in (root / 'proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            name = (path / 'comm').read_text().strip()
        except (FileNotFoundError, ProcessLookupError):
            continue  # Process exited during the snapshot.
        managed = bool(re.match(r'(?i)(?:udp[-_])?zivpn(?:[-_.]|$)|x-ui(?:[-_.]|$)', name))
        if name.startswith('xray'):
            try:
                managed = os.readlink(path / 'exe').startswith('/usr/local/x-ui/bin/')
            except FileNotFoundError:
                continue
        if managed:
            report.process_ids.add(path.name)
            report.conflicts.append('running ZiVPN/3X-UI process (PID ' + path.name + ')')


def scan_sockets(root, report):
    for protocol in ('udp', 'udp6', 'tcp', 'tcp6'):
        path = root / 'proc/net' / protocol
        try:
            lines = path.read_text().splitlines()[1:]
        except FileNotFoundError:
            if protocol.endswith('6'):
                continue  # IPv6 may be disabled on the kernel.
            raise RuntimeError('missing kernel socket table ' + protocol) from None
        for line in lines:
            fields = line.split()
            if len(fields) < 4:
                raise RuntimeError('invalid kernel socket table ' + protocol)
            port = int(fields[1].rsplit(':', 1)[1], 16)
            udp = protocol.startswith('udp')
            if (udp and udp_overlap(port)) or (not udp and fields[3] == '0A' and port in TCP_PORTS):
                report.ports.append((protocol, port, fields[9] if len(fields) > 9 else None))
                report.conflicts.append(f'{protocol.upper()} port {port} is already bound')


def scan_firewall(root, runner, which, report):
    commands = []
    for name in ('iptables-save', 'ip6tables-save', 'iptables-legacy-save', 'ip6tables-legacy-save'):
        if which(name):
            commands.append([name])
    if which('nft'):
        commands.append(['nft', '-nn', 'list', 'ruleset'])
    if not commands:
        report.errors.append('firewall inspection needs iptables-save or nft; install one and re-run as root')
    for command in commands:
        try:
            output = query(command, runner)
            report.foreign_nat |= foreign_nat(output)
            if firewall_conflict(output):
                report.conflicts.append('existing ZiVPN chains or overlapping UDP NAT rules (' + command[0] + ')')
        except RuntimeError as error:
            report.errors.append(str(error))
    paths = [root / name.lstrip('/') for name in FIREWALL_FILES]
    for pattern in ('etc/nftables.d/*.nft', 'etc/nftables/*.nft'):
        paths.extend(root.glob(pattern))
    seen = set()
    while paths:
        path = paths.pop()
        if path in seen:
            continue
        seen.add(path)
        try:
            text = path.read_text()
        except FileNotFoundError:
            continue
        if firewall_conflict(text):
            report.conflicts.append('persistent firewall configuration at /' + str(path.relative_to(root)))
            report.foreign_nat |= foreign_nat(text)
        for include in re.findall(r'^\s*include\s+"([^"]+)"', text, re.MULTILINE):
            # nft includes may live outside the conventional nftables.d directory.
            pattern = include.lstrip('/') if include.startswith('/') else str(path.parent.relative_to(root) / include)
            if '..' in Path(pattern).parts:
                raise RuntimeError('cannot verify parent-relative nftables include')
            paths.extend(root.glob(pattern))


def scan_containers(runner, which, report):
    if not which('docker'):
        return
    output = query(['docker', '--host', 'unix:///var/run/docker.sock', 'ps', '-a', '--format',
                    '{{.ID}}\t{{.Image}}\t{{.Names}}\t{{.Ports}}'], runner)
    for line in output.splitlines():
        fields = line.split('\t')
        if len(fields) != 4:
            raise RuntimeError('invalid Docker inventory')
        ident, image, name, ports = fields
        occupied = any(udp_overlap(int(first), int(last) if last else None)
                       for first, last in re.findall(r':(\d+)(?:-(\d+))?->[^,\s]+/udp', ports))
        occupied = occupied or any(int(port) in TCP_PORTS
                                  for port in re.findall(r':(\d+)->[^,\s]+/tcp', ports))
        if 'zivpn' in image.lower() or 'zivpn' in name.lower() or occupied:
            # IDs identify containers without exposing names, mounts or env secrets.
            report.conflicts.append('existing ZiVPN or conflicting port mapping in Docker container ' + ident)


def inspect_host(root=Path('/'), runner=subprocess.run, which=shutil.which):
    report = Report()
    probes = (
        ('installation files', lambda: scan_paths(root, report)),
        ('systemd services', lambda: scan_units(runner, report)),
        ('running processes', lambda: scan_processes(root, report)),
        ('bound ports', lambda: scan_sockets(root, report)),
        ('firewall rules', lambda: scan_firewall(root, runner, which, report)),
        ('Docker containers', lambda: scan_containers(runner, which, report)),
    )
    for label, probe in probes:
        try:
            probe()
        except (OSError, RuntimeError, ValueError) as error:
            report.errors.append(label + ': ' + (str(error) if isinstance(error, RuntimeError) else type(error).__name__))
    return report


def assess_host():
    if os.geteuid() != 0:
        raise SystemExit('Run the read-only conflict check as root to inspect all services, sockets and firewall rules.')
    return inspect_host()


if __name__ == '__main__':
    from lifecycle import plan_installation
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    plan = plan_installation(assess_host())
    print('Planned action: ' + plan.mode + '. No changes made.')
