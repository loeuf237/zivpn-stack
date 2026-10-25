"""Plan and transactionally update supported host installations."""
from dataclasses import dataclass, field
from contextlib import closing
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import ssl
import subprocess
import sys
import time
import uuid

from download import archive

SERVICES = ('x-ui.service', 'zivpn.service', 'zivpn-xui-sync.service',
            'zivpn-limited.service', 'zivpn_backfill.service',
            'zivpn-port-forward.service', 'zivpn-ports.service')
RETIRED = ('zivpn-limited.service', 'zivpn_backfill.service', 'zivpn-port-forward.service')


class UpdateFailure(RuntimeError):
    """A sanitized failure containing only recovery instructions, never secrets."""


@contextmanager
def deployment_lock(root=Path('/')):
    directory = root / 'run/lock'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'zivpn-stack-deploy.lock').open('a') as lock:
        os.fchmod(lock.fileno(), 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another ZiVPN deployment is already running; no runtime was changed.') from None
        yield


@dataclass
class Plan:
    mode: str
    passwords: list = field(default_factory=list, repr=False)
    config: dict = field(default_factory=dict, repr=False)


def read_json(path):
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError('object required')
        return value
    except (OSError, ValueError):
        raise SystemExit('Cannot read supported installation metadata: ' + str(path)) from None


def validate_database(path):
    from zivpn_policy import ACCOUNT_QUERY
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('integrity')
            db.execute(ACCOUNT_QUERY + ' LIMIT 0')
            db.execute('SELECT username,password FROM users LIMIT 1')
            tags = {row[0] for row in db.execute('SELECT tag FROM inbounds')}
            if not {'inbound-zivpn', 'inbound-zivpn-limited'} <= tags:
                raise ValueError('profiles')
    except (sqlite3.Error, ValueError):
        raise SystemExit('Existing database needs a schema/integrity review before an automatic update; it was not changed.') from None


def plan_installation(report, root=Path('/'), validate_tls=True):
    if report.errors:
        report.require_clean()
    cfg_path = root / 'etc/zivpn/config.json'
    database = root / 'etc/x-ui/x-ui.db'
    settings = root / 'etc/zivpn/settings.json'
    token = root / 'etc/zivpn/telegram.token'
    if database.is_file() and settings.is_file() and token.is_file() and cfg_path.is_file():
        validate_database(database)
        plan = Plan('update', config=read_json(cfg_path))
    elif cfg_path.is_file():
        if settings.exists() or token.exists() or (root / 'usr/local/bin/zivpn-native').exists():
            raise SystemExit('Incomplete stack metadata/database detected; recover the private database backup before updating. No data was reset.')
        cfg = read_json(cfg_path)
        auth = cfg.get('auth', {})
        if not isinstance(auth, dict):
            raise SystemExit('Unsupported legacy authentication metadata; no changes made.')
        passwords = auth.get('config')
        if (auth.get('mode') != 'passwords' or not isinstance(passwords, list)
                or not passwords or any(not isinstance(p, str) or not p for p in passwords)
                or cfg.get('listen') != ':5667' or cfg.get('obfs', 'zivpn') != 'zivpn'):
            raise SystemExit('Existing ZiVPN layout is not a supported password-based legacy installation; no changes made.')
        if database.exists() or (root / 'usr/local/x-ui/x-ui').exists():
            raise SystemExit('Legacy ZiVPN and an independent 3X-UI installation coexist; an explicit database migration is needed. No changes made.')
        plan = Plan('migrate', list(dict.fromkeys(passwords)), cfg)
    else:
        report.require_clean()
        return Plan('install')
    # Recognized installations are updated, but unrelated services are never stopped.
    for conflict in report.conflicts:
        if 'Docker container' in conflict:
            raise SystemExit('Docker deployment detected: update it with docker compose up -d --build; the host installer will not replace it.')
        if conflict.startswith('loaded systemd unit ') and conflict.split()[-1] not in SERVICES:
            raise SystemExit('An additional ZiVPN service needs an explicit migration: ' + conflict.split()[-1])
    if getattr(report, 'foreign_nat', False):
        raise SystemExit('An unrelated NAT rule overlaps the requested ports; resolve that mapping without stopping the existing ZiVPN.')
    owned_inodes = {}
    for pid in getattr(report, 'process_ids', set()):
        try:
            for fd in (root / 'proc' / pid / 'fd').iterdir():
                try:
                    target = os.readlink(fd)
                except FileNotFoundError:
                    continue
                if target.startswith('socket:['):
                    owned_inodes[target[8:-1]] = pid
        except FileNotFoundError:
            continue
    for protocol, port, inode in getattr(report, 'ports', []):
        if not inode or inode not in owned_inodes:
            raise SystemExit(f'{protocol.upper()} port {port} belongs to an unverified process; no service was stopped.')
        try:
            cgroups = (root / 'proc' / owned_inodes[inode] / 'cgroup').read_text().splitlines()
        except OSError:
            raise SystemExit('Cannot associate the existing listener with a supported systemd service; no changes made.') from None
        if not any(unit in line.split(':', 2)[-1].split('/') for line in cgroups for unit in SERVICES):
            raise SystemExit('An existing listener is not managed by a supported systemd service; no changes made.')
    for field in ('cert', 'key'):
        path = plan.config.get(field)
        if not isinstance(path, str) or not path.startswith('/') or not (root / path.lstrip('/')).is_file():
            raise SystemExit('Existing TLS ' + field + ' is missing; no certificates were replaced.')
    if validate_tls:
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(root / plan.config['cert'].lstrip('/'),
                                root / plan.config['key'].lstrip('/'), password=lambda: '')
        except (OSError, ssl.SSLError):
            raise SystemExit('Existing TLS certificate/key pair could not be loaded; no changes made.') from None
    return plan


def import_legacy_accounts(path, passwords, profile):
    """One stable account per unique password; never log or reset a password."""
    from zivpn_qos import PROFILES, ensure_schema
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        ensure_schema(db)
        inbound = db.execute('SELECT id FROM inbounds WHERE tag=?', (PROFILES[profile],)).fetchone()
        if not inbound:
            raise ValueError('Legacy target profile is missing')
        now = int(time.time() * 1000)
        for index, password in enumerate(dict.fromkeys(passwords), 1):
            if db.execute('SELECT 1 FROM clients WHERE password=?', (password,)).fetchone():
                continue  # Repeating the import cannot duplicate or reset accounts.
            name = f'legacy-{index:04d}'
            if db.execute('SELECT 1 FROM clients WHERE email=?', (name,)).fetchone():
                raise ValueError('Legacy account name already exists')
            cursor = db.execute('INSERT INTO clients (email,password,enable,total_gb,expiry_time,created_at,updated_at) '
                                'VALUES (?,?,1,0,0,?,?)', (name, password, now, now))
            db.execute('INSERT INTO client_traffics (inbound_id,enable,email,up,down,expiry_time,total) '
                       'VALUES (?,1,?,0,0,0,0)', (inbound[0], name))
            db.execute('INSERT INTO client_inbounds (client_id,inbound_id,created_at) VALUES (?,?,?)',
                       (cursor.lastrowid, inbound[0], now))


class Backup:
    """Private file journal. An update never rolls account data back in time."""
    def __init__(self, root, directory):
        self.root = root
        self.directory = directory
        directory.mkdir(parents=True, mode=0o700)
        directory.chmod(0o700)
        self.entries = []

    def remember(self, name):
        if any(entry['path'] == name for entry in self.entries):
            return
        path = self.root / name.lstrip('/')
        saved = self.directory / 'files' / name.lstrip('/')
        entry = {'path': name, 'exists': path.exists() or path.is_symlink()}
        if entry['exists']:
            saved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.is_symlink():
                saved.symlink_to(os.readlink(path))
            elif path.is_dir():
                shutil.copytree(path, saved, symlinks=True)
            else:
                shutil.copy2(path, saved)
        self.entries.append(entry)
        self.save_manifest('prepared')

    def save_manifest(self, state):
        path = self.directory / 'journal.json'
        path.write_text(json.dumps({'state': state, 'entries': self.entries}, indent=2) + '\n')
        path.chmod(0o600)

    def restore(self):
        for entry in reversed(self.entries):
            path = self.root / entry['path'].lstrip('/')
            saved = self.directory / 'files' / entry['path'].lstrip('/')
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
            if entry['exists']:
                path.parent.mkdir(parents=True, exist_ok=True)
                if saved.is_symlink():
                    path.symlink_to(os.readlink(saved))
                elif saved.is_dir():
                    shutil.copytree(saved, path, symlinks=True)
                else:
                    shutil.copy2(saved, path)
        self.save_manifest('rolled-back')


def legacy_nat_rules(text):
    """Only the exact local redirection installed by zahidbd2/udp-zivpn."""
    rules = []
    in_nat = False
    for line in text.splitlines():
        if line.startswith('*'):
            in_nat = line == '*nat'
        if not in_nat or not line.startswith('-A PREROUTING '):
            continue
        tokens = shlex.split(line)
        def value(key):
            return tokens[tokens.index(key) + 1] if key in tokens else None
        if (value('-p') == 'udp' and value('--dport') == '6000:19999'
                and value('-j') == 'DNAT' and value('--to-destination') == ':5667'):
            rules.append(['iptables', '-w', '5', '-t', 'nat', '-D', *tokens[1:]])
    return rules


class Updater:
    def __init__(self, plan, source, root=Path('/'), runner=subprocess.run):
        self.plan, self.source, self.root, self.runner = plan, source, root, runner
        self.ident = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:8]
        self.workspace = root / 'var/lib/zivpn-stack/updates' / self.ident
        self.backup = None
        self.states = {}
        self.firewall = None
        self.network_changed = False

    def command(self, args, check=True, **kwargs):
        return self.runner(args, check=check, capture_output=True, text=True,
                           timeout=kwargs.pop('timeout', 120), **kwargs)

    def states_before_update(self):
        for unit in SERVICES:
            loaded = self.command(['systemctl', 'show', unit, '--property=LoadState', '--value'], check=False).stdout.strip()
            if loaded not in ('loaded', 'masked', 'not-found'):
                raise RuntimeError('Cannot determine managed service state')
            if loaded in ('loaded', 'masked'):
                if loaded == 'masked':
                    raise RuntimeError('A managed service is masked; review it before updating')
                self.states[unit] = {
                    'active': self.command(['systemctl', 'is-active', unit], check=False).stdout.strip() == 'active',
                    'enabled': self.command(['systemctl', 'is-enabled', unit], check=False).stdout.strip() == 'enabled',
                }

    def stage(self, arch, config, private, profile):
        from install import seed_database
        self.workspace.mkdir(parents=True, mode=0o700)
        self.workspace.chmod(0o700)
        self.workspace.parent.chmod(0o700)
        self.states_before_update()
        env = dict(os.environ, DEBIAN_FRONTEND='noninteractive', NEEDRESTART_MODE='l')
        self.command(['apt-get', 'update'], timeout=180, env=env)
        self.command(['apt-get', 'install', '-y', 'python3', 'python3-venv', 'python3-bcrypt', 'openssl',
                      'ca-certificates', 'iptables', 'iproute2', 'conntrack', 'ipset', 'git', 'make', 'gcc'],
                     timeout=300, env=env)
        self.command([sys.executable, str(self.source / 'tools/build.py'), '--arch', arch], timeout=600)
        # Use its final absolute location: venv entrypoint shebangs stay valid.
        self.venv = self.root / 'opt/zivpn/releases' / self.ident / 'venv'
        self.venv.parent.mkdir(parents=True, mode=0o700)
        self.venv.parent.chmod(0o700)
        self.command([sys.executable, '-m', 'venv', str(self.venv)])
        self.command([str(self.venv / 'bin/pip'), 'install', '-r', str(self.source / 'requirements.txt')], timeout=300)
        # Save the tested runtime now; a later git checkout cannot change it mid-switch.
        self.runtime = self.workspace / 'runtime'
        from install import runtime_files
        self.staged_files = []
        for source, target, mode in runtime_files():
            if self.plan.mode == 'update' and target.endswith(('/x-ui.service', '/zivpn-ports.service')):
                continue  # Preserve the existing panel and network service configuration.
            staged = self.runtime / target.lstrip('/')
            staged.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(source, staged)
            staged.chmod(mode)
            self.staged_files.append((staged, target, mode))
        if self.plan.mode == 'migrate':
            lock = json.loads((self.source / 'config/artifacts.json').read_text())
            package = self.workspace / 'panel'
            archive(f"https://github.com/MHSanaei/3x-ui/releases/download/v{lock['xui_version']}/x-ui-linux-{arch}.tar.gz",
                    lock['xui'][arch], package)
            self.panel = package / 'x-ui'
            if not (self.panel / 'x-ui').is_file():
                raise RuntimeError('Unexpected panel archive layout')
            self.database = self.workspace / 'database'
            self.database.mkdir(mode=0o700)
            self.command([str(self.panel / 'x-ui'), 'setting', '-port', '2053', '-listenIP', '127.0.0.1',
                          '-webBasePath', '/' + uuid.uuid4().hex + '/'],
                         env=dict(os.environ, XUI_DB_FOLDER=str(self.database)))
            seed_database(self.database / 'x-ui.db', config, private)
            import_legacy_accounts(self.database / 'x-ui.db', self.plan.passwords, profile)

    def snapshot(self):
        self.backup = Backup(self.root, self.workspace / 'backup')
        for _, target, _ in self.staged_files:
            self.backup.remember(target)
        self.backup.remember('/opt/zivpn/venv')
        self.backup.remember('/etc/zivpn/native-qos.enabled')
        if self.plan.mode == 'migrate':
            for path in ('/etc/zivpn', '/etc/x-ui', '/usr/local/x-ui'):
                self.backup.remember(path)
            self.firewall = self.command(['iptables-save']).stdout
            path = self.backup.directory / 'iptables.rules'
            path.write_text(self.firewall)
            path.chmod(0o600)
        else:
            shutil.copytree(self.root / 'etc/zivpn', self.backup.directory / 'configuration', symlinks=True)
        state = self.backup.directory / 'services.json'
        state.write_text(json.dumps(self.states, indent=2) + '\n')
        state.chmod(0o600)

    def stop_old_runtime(self):
        if self.states.get('zivpn-xui-sync.service', {}).get('active'):
            self.command(['systemctl', 'stop', 'zivpn-xui-sync.service'])
        if self.plan.mode == 'update' and self.states.get('zivpn.service', {}).get('active'):
            self.command(['/usr/local/sbin/zivpn-native-save-traffic'])
        for unit in ('zivpn.service', *RETIRED, 'x-ui.service'):
            if self.states.get(unit, {}).get('active'):
                self.command(['systemctl', 'stop', unit])
        # Capture a consistent final database after all known writers have stopped.
        path = self.root / 'etc/x-ui/x-ui.db'
        if path.exists():
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                with closing(sqlite3.connect(self.backup.directory / 'x-ui.db')) as target, target:
                    db.backup(target)
            (self.backup.directory / 'x-ui.db').chmod(0o600)

    def activate_files(self, config, private):
        from install import install_file
        for source, target, mode in self.staged_files:
            install_file(source, self.root / target.lstrip('/'), mode)
        target = self.root / 'opt/zivpn/venv'
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        target.symlink_to(self.venv)
        directory = self.root / 'etc/zivpn'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.plan.mode == 'migrate':
            directory.chmod(0o700)
            shutil.copytree(self.panel, self.root / 'usr/local/x-ui', dirs_exist_ok=True)
            (self.root / 'etc/x-ui').mkdir(parents=True, exist_ok=True, mode=0o700)
            install_file(self.database / 'x-ui.db', self.root / 'etc/x-ui/x-ui.db')
            (directory / 'settings.json').write_text(json.dumps(config, indent=2) + '\n')
            (directory / 'settings.json').chmod(0o600)
            (directory / 'telegram.token').write_text(private['telegram_token'] + '\n')
            (directory / 'telegram.token').chmod(0o600)
            # The existing native listen/certificate paths and passwords remain intact.
            (directory / 'config.json').chmod(0o600)
        marker = directory / 'native-qos.enabled'
        marker.write_text('authenticated-payload-qos-v1\n')
        marker.chmod(0o600)
        self.command(['systemctl', 'daemon-reload'])
        if self.plan.mode == 'migrate':
            self.network_changed = True
            for rule in legacy_nat_rules(self.firewall):
                self.command(rule)
            self.command(['systemctl', 'restart', 'zivpn-ports.service'])
        self.command(['systemctl', 'start', 'x-ui.service', 'zivpn.service'])

    def validate_runtime(self):
        # The collector starts only after checks pass, so failed upgrades do not
        # send Telegram messages or race with a database rollback.
        from zivpn_native_accounting import request
        deadline = time.monotonic() + 30
        while True:
            try:
                request('/health')
                result = self.command(['/usr/local/sbin/zivpn-native-auth'],
                                      input=json.dumps({'check': True, 'ids': []}))
                if json.loads(result.stdout).get('ok') is True:
                    break
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError('Updated server did not become healthy')
            time.sleep(1)
        mode = (self.root / 'run/zivpn-native.sock').stat().st_mode & 0o777
        if mode != 0o600:
            raise RuntimeError('Statistics socket permissions are not private')
        for unit in ('x-ui.service', 'zivpn.service'):
            self.command(['systemctl', 'is-active', '--quiet', unit])

    def finish(self):
        for unit in RETIRED:
            if unit in self.states:
                self.command(['systemctl', 'disable', unit])
        if self.plan.mode == 'migrate':
            self.command(['systemctl', 'enable', 'x-ui.service', 'zivpn.service',
                          'zivpn-ports.service', 'zivpn-xui-sync.service'])
            self.command(['systemctl', 'start', 'zivpn-xui-sync.service'])
            self.command(['systemctl', 'is-active', '--quiet', 'zivpn-xui-sync.service'])
        else:
            # Keep intentionally stopped/disabled services in their previous state.
            for unit in ('zivpn.service', 'x-ui.service'):
                if not self.states.get(unit, {}).get('active'):
                    self.command(['systemctl', 'stop', unit])
            if self.states.get('zivpn-xui-sync.service', {}).get('active'):
                self.command(['systemctl', 'start', 'zivpn-xui-sync.service'])
                self.command(['systemctl', 'is-active', '--quiet', 'zivpn-xui-sync.service'])
        self.backup.save_manifest('committed')

    def rollback(self):
        errors = []
        # Never overwrite an existing stack's live DB with an older snapshot.
        if self.plan.mode == 'update':
            try:
                self.command(['/usr/local/sbin/zivpn-native-save-traffic'])
            except Exception:
                pass  # The new server may have failed before opening its socket.
        units = ['zivpn-xui-sync.service', 'zivpn.service', 'x-ui.service']
        if self.network_changed:
            units.append('zivpn-ports.service')
        for unit in units:
            try:
                loaded = self.command(['systemctl', 'show', unit, '--property=LoadState', '--value'], check=False).stdout.strip()
                if loaded == 'loaded':
                    self.command(['systemctl', 'stop', unit])
                elif loaded != 'not-found':
                    raise RuntimeError('Cannot determine service state during rollback')
            except Exception as error:
                errors.append(type(error).__name__)
        # If a writer cannot be stopped, leave backups for manual recovery.
        if errors:
            raise RuntimeError('Rollback could not stop all managed writers')
        self.backup.restore()
        if self.network_changed:
            self.command(['iptables-restore', '-w', '5'], input=self.firewall)
        self.command(['systemctl', 'daemon-reload'])
        for unit in SERVICES:
            if unit in self.states:
                self.command(['systemctl', 'enable' if self.states[unit]['enabled'] else 'disable', unit])
                if self.states[unit]['active']:
                    self.command(['systemctl', 'start', unit])
            elif self.plan.mode == 'migrate':
                self.command(['systemctl', 'disable', unit], check=False)

    def apply(self, arch, config, private=None, profile='standard'):
        try:
            self.stage(arch, config, private, profile)
            self.snapshot()
        except Exception as error:
            raise UpdateFailure('Preparation failed (' + type(error).__name__ + '); existing services were not stopped. '
                                'Private staging data: ' + str(self.workspace)) from None
        phase = 'stopping the old runtime'
        try:
            self.stop_old_runtime()
            phase = 'activating the prepared runtime'
            self.activate_files(config, private)
            phase = 'validating the updated runtime'
            self.validate_runtime()
            phase = 'restoring service policy'
            self.finish()
        except (Exception, KeyboardInterrupt) as error:
            try:
                self.rollback()
            except Exception:
                self.backup.save_manifest('rollback-failed')
                raise UpdateFailure('Update failed; automatic rollback is incomplete. Private recovery data: ' + str(self.backup.directory)) from None
            raise UpdateFailure('Update failed while ' + phase + ' (' + type(error).__name__ + '); '
                                'the previous runtime was restored. Private backup: ' + str(self.backup.directory)) from None
        return self.backup.directory
