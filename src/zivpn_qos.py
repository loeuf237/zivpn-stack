"""Persistent native bandwidth policy: Standard per public IP, Premium per account."""
from decimal import Decimal, DecimalException
import sqlite3

STANDARD = 'inbound-zivpn'
PREMIUM = 'inbound-zivpn-limited'
PROFILES = {'standard': STANDARD, 'premium': PREMIUM}
DEFAULTS = {'standard': 1000000, 'premium': 4000000}
MAX_RATE = 1000000000000


def speed_bytes(value):
    try:
        number = Decimal(str(value).replace(',', '.')) * 1000000
        if not number.is_finite() or number < 1 or number > MAX_RATE or number != number.to_integral_value():
            raise ValueError('Vitesse positive en Mo/s, précision maximale : un octet/s.')
        return int(number)
    except (DecimalException, TypeError):
        raise ValueError('Vitesse invalide : utilisez par exemple 1, 4 ou 0.5 Mo/s.') from None


def ensure_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS zivpn_qos_defaults (profile TEXT PRIMARY KEY, rate INTEGER NOT NULL CHECK(rate>0))')
    db.execute('CREATE TABLE IF NOT EXISTS zivpn_qos_accounts (email TEXT PRIMARY KEY, rate INTEGER NOT NULL CHECK(rate>0))')
    for profile, rate in DEFAULTS.items():
        db.execute('INSERT OR IGNORE INTO zivpn_qos_defaults VALUES (?,?)', (profile, rate))


def defaults(db):
    try:
        rows = dict(db.execute('SELECT profile,rate FROM zivpn_qos_defaults'))
    except sqlite3.OperationalError as error:
        if 'no such table' not in str(error):
            raise
        rows = {}
    result = dict(DEFAULTS, **{k:v for k,v in rows.items() if k in DEFAULTS})
    if any(type(rate) is not int or not 1 <= rate <= MAX_RATE for rate in result.values()):
        raise ValueError('Invalid persisted bandwidth')
    return result


def account_rate(db, email, tag):
    profile = 'premium' if tag == PREMIUM else 'standard'
    try:
        row = db.execute('SELECT rate FROM zivpn_qos_accounts WHERE email=?', (email,)).fetchone()
    except sqlite3.OperationalError as error:
        if 'no such table' not in str(error):
            raise
        row = None
    rate = row[0] if row else defaults(db)[profile]
    if type(rate) is not int or not 1 <= rate <= MAX_RATE:
        raise ValueError('Invalid persisted account bandwidth')
    return rate


def describe(db, email, tag):
    profile = 'Premium' if tag == PREMIUM else 'Standard'
    scope = 'par compte, toutes IP confondues' if tag == PREMIUM else 'par IP publique, toutes connexions confondues'
    return f'{profile} : {account_rate(db,email,tag)/1000000:g} Mo/s {scope}'


def set_speed(db, target, value, global_default=False):
    rate = speed_bytes(value)
    ensure_schema(db)
    if global_default:
        profile = 'premium' if target.lower() == 'vip' else target.lower()
        if profile not in PROFILES:
            raise ValueError('Profil attendu : standard ou premium.')
        db.execute('UPDATE zivpn_qos_defaults SET rate=? WHERE profile=?', (rate,profile))
        return f'Défaut {profile} : {rate/1000000:g} Mo/s. Les comptes personnalisés sont conservés.'
    row = db.execute('SELECT ib.tag FROM client_traffics ct JOIN inbounds ib ON ib.id=ct.inbound_id WHERE ct.email=?', (target,)).fetchone()
    if not row or row[0] not in (STANDARD,PREMIUM):
        raise ValueError('Compte VPN introuvable.')
    db.execute('INSERT OR REPLACE INTO zivpn_qos_accounts VALUES (?,?)', (target,rate))
    return describe(db,target,row[0])


def set_profile(db, email, profile, speed=None):
    if profile not in PROFILES:
        raise ValueError('Profil attendu : standard ou premium.')
    inbound = db.execute('SELECT id FROM inbounds WHERE tag=?', (PROFILES[profile],)).fetchone()
    client = db.execute('SELECT id FROM clients WHERE email=?', (email,)).fetchone()
    if not inbound or not client:
        raise ValueError('Compte ou profil introuvable.')
    ensure_schema(db)
    if speed is not None:
        set_speed(db,email,speed)
    db.execute('UPDATE client_traffics SET inbound_id=? WHERE email=?', (inbound[0],email))
    db.execute('DELETE FROM client_inbounds WHERE client_id=?', (client[0],))
    db.execute('INSERT INTO client_inbounds (client_id,inbound_id,created_at) VALUES (?,?,?)', (client[0],inbound[0],__import__('time').time_ns()//1000000))
    return describe(db,email,PROFILES[profile])
