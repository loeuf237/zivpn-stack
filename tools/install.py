#!/usr/bin/env python3
"""Install the full stack on a new Ubuntu/Debian systemd server.

Refuses existing installations. Never run this to upgrade a live server.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile

from download import archive
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from zivpn_settings import load_settings


def run(args, **kwargs):
    subprocess.run(args, check=True, timeout=kwargs.pop('timeout',120), **kwargs)


def install_file(source,target,mode=0o600):
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name(target.name+'.new')
    shutil.copyfile(source,temporary);temporary.chmod(mode);os.replace(temporary,target)


def seed_database(path,config,private):
    import bcrypt
    hashed=bcrypt.hashpw(private['panel_password'].encode(),bcrypt.gensalt()).decode()
    with sqlite3.connect(path,timeout=10) as conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT COUNT(*) FROM inbounds').fetchone()[0]:
            raise ValueError('Fresh installation requires an empty inbounds table')
        conn.execute('UPDATE users SET username=?,password=? WHERE id=(SELECT MIN(id) FROM users)',
                     (private['panel_username'],hashed))
        user_id=conn.execute('SELECT MIN(id) FROM users').fetchone()[0]
        if not user_id:raise ValueError('Panel administrator missing')
        for ident,tag,port,label in [(1,'inbound-zivpn',56670,'ZiVPN Standard'),(2,'inbound-zivpn-limited',56680,'ZiVPN Premium')]:
            conn.execute('''INSERT INTO inbounds
                (id,user_id,up,down,total,remark,enable,expiry_time,listen,port,protocol,settings,stream_settings,tag,sniffing)
                VALUES (?,?,0,0,0,?,1,0,'127.0.0.1',?,'trojan',?,?,?,?)''',
                (ident,user_id,label,port,json.dumps({'clients':[],'fallbacks':[]}),
                 json.dumps({'network':'tcp','security':'none'}),tag,json.dumps({'enabled':False})))
        for key,value in [('tgBotChatId',str(config['primary_admin_id'])),('tgBotEnable','false')]:
            conn.execute('DELETE FROM settings WHERE key=?',(key,))
            conn.execute('INSERT INTO settings(key,value) VALUES (?,?)',(key,value))
    Path(path).chmod(0o600)


def validate_private(path):
    import re
    path=Path(path)
    if path.stat().st_mode & 0o077:raise ValueError('Secrets file must have mode 0600')
    private=json.loads(path.read_text())
    if not re.fullmatch(r'[0-9]{6,}:[A-Za-z0-9_-]{30,}',private.get('telegram_token','')):
        raise ValueError('Invalid Telegram token format')
    if len(private.get('panel_password',''))<20 or private['panel_password'].startswith('REPLACE'):
        raise ValueError('Panel password must be unique and at least 20 characters')
    if not private.get('panel_username'):raise ValueError('Panel username missing')
    return private


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings',required=True)
    parser.add_argument('--secrets',required=True)
    parser.add_argument('--check',action='store_true',help='Validate prerequisites without making changes')
    args=parser.parse_args()
    config=load_settings(args.settings);private=validate_private(args.secrets)
    arch={'x86_64':'amd64','aarch64':'arm64'}.get(platform.machine())
    if not arch:raise SystemExit('Supported architectures: amd64, arm64')
    if not Path('/run/systemd/system').exists():raise SystemExit('systemd is required')
    os_release=Path('/etc/os-release').read_text()
    if not any(line in os_release.splitlines() for line in ['ID=ubuntu','ID=debian']):
        raise SystemExit('This installer supports Ubuntu and Debian only')
    for path in ['/etc/zivpn/config.json','/etc/x-ui/x-ui.db','/usr/local/bin/zivpn-native','/usr/local/x-ui/x-ui']:
        if Path(path).exists():raise SystemExit('Existing installation detected; refusing to overwrite: '+path)
    print('Preflight passed. Target: Linux '+arch+' / systemd; fresh installation only.')
    if args.check:return
    if os.geteuid()!=0:raise SystemExit('Run as root on the NEW target server')
    run(['apt-get','update'],timeout=180)
    run(['apt-get','install','-y','python3','python3-venv','python3-bcrypt','openssl','ca-certificates','iptables','iproute2','conntrack','ipset','git','make','gcc'],timeout=300)
    run([sys.executable,str(ROOT/'tools/build.py'),'--arch',arch],timeout=600)
    for directory in ['/etc/zivpn','/etc/x-ui','/var/lib/zivpn-telegram','/opt/zivpn']:
        Path(directory).mkdir(parents=True,exist_ok=True);Path(directory).chmod(0o700)
    lock=json.loads((ROOT/'config/artifacts.json').read_text());version=lock['xui_version']
    with tempfile.TemporaryDirectory(prefix='zivpn-xui-') as directory:
        archive(f'https://github.com/MHSanaei/3x-ui/releases/download/v{version}/x-ui-linux-{arch}.tar.gz',lock['xui'][arch],directory)
        package=Path(directory)/'x-ui'
        if not (package/'x-ui').is_file():raise ValueError('Unexpected 3X-UI archive layout')
        shutil.copytree(package,'/usr/local/x-ui',dirs_exist_ok=True)
    env=dict(os.environ,XUI_DB_FOLDER='/etc/x-ui')
    run(['/usr/local/x-ui/x-ui','setting','-port','2053','-listenIP','127.0.0.1','-webBasePath','/'+secrets.token_urlsafe(18)+'/'],env=env,stdout=subprocess.DEVNULL)
    seed_database('/etc/x-ui/x-ui.db',config,private)
    run([sys.executable,'-m','venv','/opt/zivpn/venv'])
    run(['/opt/zivpn/venv/bin/pip','install','-r',str(ROOT/'requirements.txt')],timeout=300)
    for source in (ROOT/'src').glob('zivpn_*.py'):install_file(source,'/usr/local/lib/'+source.name)
    install_file(ROOT/'src/zivpn-xui-sync.py','/usr/local/bin/zivpn-xui-sync.py',0o700)
    install_file(ROOT/'src/zivpn-expire-account.py','/usr/local/bin/zivpn-expire-account',0o700)
    install_file(ROOT/'scripts/zivpn-native-auth.py','/usr/local/sbin/zivpn-native-auth',0o700)
    install_file(ROOT/'scripts/zivpn-native-save-traffic','/usr/local/sbin/zivpn-native-save-traffic',0o700)
    install_file(ROOT/'scripts/zivpn-ports','/usr/local/sbin/zivpn-ports',0o700)
    install_file(ROOT/'scripts/zivpn-set-telegram-token','/usr/local/sbin/zivpn-set-telegram-token',0o700)
    install_file(ROOT/'scripts/auto-cleanup-logs.sh','/usr/local/bin/auto-cleanup-logs.sh',0o700)
    install_file(ROOT/'build/zivpn-native','/usr/local/bin/zivpn-native',0o700)
    config_path=Path('/etc/zivpn/settings.json');config_path.write_text(json.dumps(config,indent=2)+'\n');config_path.chmod(0o600)
    token=Path('/etc/zivpn/telegram.token');token.write_text(private['telegram_token']+'\n');token.chmod(0o600)
    run(['openssl','req','-x509','-nodes','-newkey','rsa:2048','-keyout','/etc/zivpn/zivpn.key',
         '-out','/etc/zivpn/zivpn.crt','-days','365','-subj','/CN=zivpn'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    Path('/etc/zivpn/zivpn.key').chmod(0o600)
    vpn={'listen':':5667','cert':'/etc/zivpn/zivpn.crt','key':'/etc/zivpn/zivpn.key','quic':{'disablePathMTUDiscovery':True}}
    p=Path('/etc/zivpn/config.json');p.write_text(json.dumps(vpn,indent=2)+'\n');p.chmod(0o600)
    p=Path('/etc/zivpn/native-qos.enabled');p.write_text('authenticated-payload-qos-v1\n');p.chmod(0o600)
    p=Path('/etc/zivpn/clients.csv');p.write_text('client,password\n');p.chmod(0o600)
    for source in (ROOT/'deploy/systemd').glob('*.service'):install_file(source,'/etc/systemd/system/'+source.name,0o644)
    run(['systemctl','daemon-reload'])
    run(['systemctl','enable','--now','x-ui','zivpn-ports','zivpn','zivpn-xui-sync'])
    run(['systemctl','is-active','x-ui','zivpn','zivpn-xui-sync'])
    print('Installed. Panel listens locally on 2053; use an SSH tunnel. No VPN accounts created.')

if __name__=='__main__':
    try:main()
    except Exception as error:
        print('Installation failed: '+type(error).__name__+'; inspect services and retained files before retrying.',file=sys.stderr)
        sys.exit(1)
