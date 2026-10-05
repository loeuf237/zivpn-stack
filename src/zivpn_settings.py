"""Per-server configuration without source-code edits or shared credentials."""
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess


def load_settings(path=None):
    path = Path(path or os.environ.get('ZIVPN_SETTINGS', '/etc/zivpn/settings.json'))
    settings = json.loads(path.read_text())
    host = settings.get('server_address', '')
    if not isinstance(host, str) or not host or len(host) > 253 or not re.fullmatch(r'[A-Za-z0-9.:-]+', host):
        raise ValueError('Invalid server_address')
    admin = settings.get('primary_admin_id')
    if isinstance(admin, bool) or not isinstance(admin, int) or admin <= 0:
        raise ValueError('Invalid primary_admin_id')
    interface = settings.get('network_interface')
    if interface is not None and (not isinstance(interface, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', interface)):
        raise ValueError('Invalid network_interface')
    return settings


def default_interface():
    routes = json.loads(subprocess.check_output(['ip', '-j', '-4', 'route', 'show', 'default'], timeout=5))
    if not routes or not routes[0].get('dev'):
        raise ValueError('No default IPv4 interface')
    return routes[0]['dev']
