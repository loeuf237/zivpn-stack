#!/usr/bin/env python3
"""Expire one 3X-UI account without restarting unrelated VPN sessions."""
import argparse
import datetime
import json
from pathlib import Path
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

sys.path.append('/usr/local/lib')
DB_PATH = '/etc/x-ui/x-ui.db'


def resolve(conn, target):
    query = '''SELECT c.id,c.email FROM clients c
               JOIN client_traffics ct ON c.email=ct.email
               JOIN inbounds ib ON ct.inbound_id=ib.id
               WHERE ib.tag IN ('inbound-zivpn','inbound-zivpn-limited') AND {}=?'''
    rows = conn.execute(query.format('c.email'), (target,)).fetchall()
    if not rows:
        # Compatibility with the previous scheduled helper; never log passwords.
        rows = conn.execute(query.format('c.password'), (target,)).fetchall()
    if len(rows) > 1:
        raise ValueError('Ambiguous account; use a unique account name')
    return rows[0] if rows else None


def expire_account(target, db_path=DB_PATH, now_ms=None, dry_run=False):
    now_ms = int(time.time()*1000) if now_ms is None else now_ms
    uri = Path(db_path).resolve().as_uri() + ('?mode=ro' if dry_run else '?mode=rw')
    with sqlite3.connect(uri, uri=True, timeout=10) as conn:
        if not dry_run:
            conn.execute('BEGIN IMMEDIATE')
        account = resolve(conn, target)
        if account is None:
            return None
        client_id, email = account
        if not dry_run:
            conn.execute('UPDATE clients SET enable=0,expiry_time=?,updated_at=? WHERE id=?',
                         (now_ms, now_ms, client_id))
            conn.execute('UPDATE client_traffics SET enable=0,expiry_time=? WHERE email=?',
                         (now_ms, email))
        return email


def notify(email):
    token = Path('/etc/zivpn/telegram.token').read_text().strip()
    payload = urllib.parse.urlencode({'chat_id': str(__import__('zivpn_settings').load_settings()['primary_admin_id']),
               'text': f'Compte ZiVPN expiré : {email}. Historique conservé. Aucun VPN redémarré.'}).encode()
    req = urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage', data=payload)
    with urllib.request.urlopen(req, timeout=10) as response:
        if not json.load(response).get('ok'):
            raise ValueError('Notification rejected')


def main():
    parser = argparse.ArgumentParser(description='Expiration ciblée d’un compte ZiVPN dans 3X-UI')
    parser.add_argument('account', help='Nom du compte (ancien mot de passe accepté uniquement si unique)')
    parser.add_argument('--dry-run', action='store_true', help='Vérifier sans modifier ni notifier')
    parser.add_argument('--notify', action='store_true', help='Notifier l’administrateur principal après expiration')
    args = parser.parse_args()
    email = expire_account(args.account, dry_run=args.dry_run)
    if email is None:
        print('Compte introuvable ; aucune modification.')
        return 0
    if args.dry_run:
        print(f'Compte identifié : {email}. Vérification seule ; aucune modification.')
        return 0
    import zivpn_accounting
    if zivpn_accounting.native_enabled():
        import zivpn_native_accounting
        closed = zivpn_native_accounting.kick(email)
        print(f'Compte expiré : {email}. Sessions fermées : {closed}. Aucun VPN redémarré.')
    else:
        print(f'Compte expiré : {email}. Les nouvelles connexions sont refusées ; ancien mode serveur.')
    if args.notify:
        notify(email)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        # URLs and exception messages can contain secrets; emit types only.
        print('Expiration non terminée : '+type(error).__name__, file=sys.stderr)
        sys.exit(1)
