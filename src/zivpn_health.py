"""Private, bounded operational history. No credentials, payloads, or domains."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
from collections import Counter

RETENTION = 7 * 86400


class PlainText(str):
    plain_text = True


def bucket_rates(first, second, elapsed):
    if elapsed <= 0 or first['epoch'] != second['epoch']:
        return []
    rows = []
    for key, current in second['buckets'].items():
        old = first['buckets'].get(key)
        if old is None or old.get("generation") != current.get("generation"):
            continue
        delta = current['up'] + current['down'] - old['up'] - old['down']
        waits = current['wait_calls'] - old['wait_calls']
        delayed = current['delayed_calls'] - old['delayed_calls']
        if min(delta, waits, delayed) < 0:
            continue  # Bucket expired/recreated: no invented rate.
        ceiling = current['limit_bytes_per_second']
        rows.append({'key': key, 'sessions': current['sessions'], 'bytes_per_second': delta / elapsed,
                     'utilization': delta / elapsed / ceiling, 'limit': ceiling,
                     'delayed_percent': 100 * delayed / waits if waits else 0})
    return sorted(rows, key=lambda row: row['utilization'], reverse=True)


def kernel_counters(interface):
    result = {}
    for key in ('rx_errors', 'tx_errors', 'rx_dropped', 'tx_dropped'):
        result[key] = int(Path('/sys/class/net', interface, 'statistics', key).read_text())
    lines = Path('/proc/net/snmp').read_text().splitlines()
    udp = [line.split()[1:] for line in lines if line.startswith('Udp:')]
    values = dict(zip(udp[0], map(int, udp[1])))
    for key in ('InErrors', 'RcvbufErrors', 'SndbufErrors', 'InCsumErrors'):
        result['udp_' + key] = values[key]
    for key in ('count', 'max'):
        result['conntrack_' + key] = int(Path('/proc/sys/net/netfilter/nf_conntrack_' + key).read_text())
    return result


class Monitor:
    def __init__(self, path='/var/lib/zivpn-telegram/health.db', interface='eth0'):
        parent = Path(path).parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path, self.interface = str(path), interface
        self.lock = threading.RLock()
        self.previous = None
        self.alerted = {}
        self.last_collect = 0
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS closes (
                    epoch TEXT, sequence INTEGER, closed_ms INTEGER, email TEXT, ip TEXT,
                    lifetime REAL, last_payload_ms INTEGER, reason TEXT, up INTEGER, down INTEGER,
                    rtt_ms REAL, sent INTEGER, acked INTEGER, lost INTEGER, pto INTEGER,
                    PRIMARY KEY(epoch,sequence));
                CREATE INDEX IF NOT EXISTS closes_date ON closes(closed_ms);
                CREATE TABLE IF NOT EXISTS samples (created REAL PRIMARY KEY, data TEXT);
                CREATE TABLE IF NOT EXISTS cursors (epoch TEXT PRIMARY KEY, sequence INTEGER);
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value REAL);
            ''')
            row = db.execute("SELECT value FROM state WHERE key='last_alert'").fetchone()
            if row:
                self.alerted['notice'] = row[0]
        os.chmod(self.path, 0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def collect(self, native, transport, now=None):
        now = time.time() if now is None else now
        if now - self.last_collect < 60:
            return []
        self.last_collect = now
        data = {'telegram': transport.snapshot(), 'native_available': False}
        notices = []
        try:
            health, snapshot = native.request('/health'), native.snapshot()
            if health['epoch'] != snapshot['epoch']:
                raise ValueError('Epoch changed during sample')
            data.update(native_available=True, epoch=health['epoch'], auth=health['auth'],
                        native_started_ms=health['started_ms'], close_sequence=health['close_sequence'],
                        tunnels=len(snapshot['sessions']), ips=len({row['ip'] for row in snapshot['sessions'].values()}))
            data['kernel'] = kernel_counters(self.interface)
            if self.previous and self.previous[1]['epoch'] == health['epoch']:
                elapsed = now - self.previous[0]
                rates = bucket_rates(self.previous[1], health, elapsed)
                data['buckets'] = rates[:40]
                data['saturated_buckets'] = sum(row['utilization'] >= .9 for row in rates)
            self.previous = (now, health)
            with self.lock, self.connect() as db:
                row = db.execute('SELECT sequence FROM cursors WHERE epoch=?', (health['epoch'],)).fetchone()
                last = row[0] if row else 0
                events = [event for event in health['closes'] if event['sequence'] > last]
                data['missing_close_events'] = max(0, health['close_sequence'] - last - len(events))
                for event in events:
                    q = event['transport']
                    db.execute('INSERT OR IGNORE INTO closes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                               (health['epoch'], event['sequence'], event['closed_ms'], event['email'], event['ip'],
                                event['lifetime_seconds'], event['last_payload_ms'], event['reason'], event['up'], event['down'],
                                q['rtt_ms'], q['sent_packets'], q['acked_packets'], q['lost_packets'], q['pto_count']))
                db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?)', (health['epoch'], health['close_sequence']))
                db.execute('DELETE FROM cursors WHERE epoch!=?', (health['epoch'],))
            if data['missing_close_events']:
                notices.append('Historique incomplet : certains événements QUIC ont expiré avant collecte.')
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
            data['native_error'] = type(error).__name__
            notices.append('Télémétrie VPN indisponible ; vérifier le service.')
        try:
            result = subprocess.run(['systemctl', 'is-active', 'zivpn', 'zivpn-xui-sync', 'x-ui'],
                                    capture_output=True, text=True, timeout=5)
            states = result.stdout.splitlines()
            data['services'] = dict(zip(('VPN', 'Telegram', '3X-UI'), states))
            for name, state in data['services'].items():
                if state != 'active':
                    notices.append(f'{name} : {state}.')
        except (OSError, subprocess.SubprocessError):
            data['services'] = {}
        with self.lock, self.connect() as db:
            previous = db.execute('SELECT data FROM samples ORDER BY created DESC LIMIT 1').fetchone()
            if previous:
                old = json.loads(previous[0])
                if data.get('epoch') == old.get('epoch') and data.get('native_available'):
                    for key in ('database_error', 'internal_error', 'helper_timeout', 'helper_exit', 'helper_error', 'invalid_helper_response', 'recheck_error'):
                        delta = data['auth'].get(key, 0) - old.get('auth', {}).get(key, 0)
                        if delta >= 3:
                            notices.append(f'Authentification : {delta} erreurs {key} depuis la mesure précédente.')
                if 'kernel' in data and 'kernel' in old:
                    for key, value in data['kernel'].items():
                        if key not in ('conntrack_count', 'conntrack_max') and value > old['kernel'].get(key, value):
                            notices.append(f'Réseau : +{value-old["kernel"][key]} {key}.')
                if any(value > old.get('telegram', {}).get(key, 0) for key, value in data['telegram'].items() if key.endswith(':delivery_uncertain')):
                    notices.append('Telegram : livraison de message incertaine ; vérifier /taches avant de relancer une action.')
            if data.get('kernel', {}).get('conntrack_count', 0) >= .8 * data.get('kernel', {}).get('conntrack_max', float('inf')):
                notices.append('Conntrack dépasse 80 % de sa capacité.')
            db.execute('INSERT OR REPLACE INTO samples VALUES (?,?)', (now, json.dumps(data)))
            db.execute('DELETE FROM closes WHERE closed_ms<?', (int((now-RETENTION)*1000),))
            db.execute('DELETE FROM samples WHERE created<?', (now-RETENTION,))
            db.execute('DELETE FROM closes WHERE rowid IN (SELECT rowid FROM closes ORDER BY closed_ms DESC LIMIT -1 OFFSET 100000)')
        # Group all conditions, with one notification at most every 30 minutes.
        if notices and now - self.alerted.get('notice', 0) >= 1800:
            self.alerted['notice'] = now
            with self.lock, self.connect() as db:
                db.execute("INSERT OR REPLACE INTO state VALUES ('last_alert',?)", (now,))
            return notices
        return []

    def report(self, hours=48, now=None):
        if hours not in (24, 48):
            raise ValueError('Use 24 or 48 hours')
        now = time.time() if now is None else now
        with self.lock, self.connect() as db:
            events = db.execute('SELECT email,lifetime,reason,last_payload_ms,closed_ms,rtt_ms,sent,lost FROM closes WHERE closed_ms>=?',
                                (int((now-hours*3600)*1000),)).fetchall()
            samples = [(created, json.loads(data)) for created, data in db.execute(
                'SELECT created,data FROM samples WHERE created>=? ORDER BY created', (now-hours*3600,))]
        lines = [f'Santé du serveur — fenêtre demandée : {hours} h']
        if not samples:
            return PlainText('\n'.join(lines + ['Historique en cours de constitution ; aucune mesure enregistrée.']))
        latest = samples[-1][1]
        span = max(0, now - samples[0][0]) / 3600
        lines += [f'Historique de mesures disponible : {span:.1f} h ; dernière mesure il y a {int(max(0,now-samples[-1][0]))} s.',
                  f'Services : {", ".join(name+"="+state for name,state in latest.get("services",{}).items())}',
                  f'Tunnels : {latest.get("tunnels", "?")} ; IP publiques : {latest.get("ips", "?")}.']
        reasons = Counter(row[2] for row in events)
        short = sum(row[1] < 120 for row in events)
        recent_payload = sum(row[3] > 0 and 0 <= row[4] - row[3] <= 120000 and row[2] == 'idle_timeout' for row in events)
        lines += [f'Fermetures enregistrées : {len(events)} ; durée <2 min : {short}.',
                  'Motifs : ' + (', '.join(f'{key}={value}' for key, value in reasons.most_common()) or 'aucun'),
                  f'Expirations avec trafic utile dans les 2 dernières minutes : {recent_payload}.',
                  'Une expiration ne prouve pas une coupure pendant la navigation.']
        sent, lost = sum(row[6] for row in events), sum(row[7] for row in events)
        if sent:
            lines.append(f'Paquets déclarés perdus / envoyés sur les tunnels fermés : {100*lost/sent:.2f}% (estimation QUIC).')
        # Counters are cumulative within one native epoch / one Telegram process.
        auth = Counter(); telegram = Counter()
        for index, (_, current) in enumerate(samples):
            old = samples[index-1][1] if index else current
            if current.get('epoch') == old.get('epoch'):
                for key,value in current.get('auth',{}).items():
                    auth[key] += max(0,value-old.get('auth',{}).get(key,0))
            for key,value in current.get('telegram',{}).items():
                telegram[key] += max(0,value-old.get('telegram',{}).get(key,0))
        failures = {key:value for key,value in auth.items() if key != 'accepted' and value}
        lines += ['Authentification entre mesures : '+(', '.join(f'{key}={value}' for key,value in failures.items()) or 'aucun refus/incident'),
                  'Telegram entre mesures : '+(', '.join(f'{key}={value}' for key,value in telegram.items() if not key.endswith((':success', ':unchanged')) and value) or 'aucune erreur'),
                  f'Groupes à ≥90 % du plafond lors de la dernière mesure : {latest.get("saturated_buckets", "mesure en cours")}.',
                  f'Événements QUIC non récupérés : {sum(data.get("missing_close_events",0) for _,data in samples)}.',
                  'Standard : 500 Ko/s par IP publique ; Premium : 4 Mo/s par compte, montant + descendant.',
                  '/qualite : mesure de 5 s des plafonds et tunnels. Historique privé conservé 7 jours.']
        return PlainText('\n'.join(lines)[:3400])


def quality_report(native, wait=time.sleep, seconds=5):
    first = native.request('/health'); started = time.monotonic()
    wait(seconds)
    second, snapshot = native.request('/health'), native.snapshot()
    elapsed = time.monotonic() - started
    if first['epoch'] != second['epoch'] or second['epoch'] != snapshot['epoch']:
        return PlainText('VPN redémarré pendant la mesure ; relancer /qualite.')
    rows = bucket_rates(first, second, elapsed)
    lines = [f'Qualité et plafonds — mesure sur {elapsed:.1f} s',
             'Débits utiles, montant + descendant ; délais observés dans le limiteur.']
    for row in rows[:12]:
        kind = 'Compte Premium' if row['key'].startswith('P:') else 'IP Standard'
        label = row['key'][2:].replace('\n',' ')[:80]
        lines.append(f'{kind} {label} : {row["bytes_per_second"]/1000:.1f}/{row["limit"]/1000:.0f} Ko/s '
                     f'({100*row["utilization"]:.0f} %), {row["sessions"]} tunnels ; attentes >1 ms : {row["delayed_percent"]:.0f} %.')
    sessions = list(snapshot['sessions'].values())
    rtts = sorted(row['transport']['rtt_ms'] for row in sessions if row.get('transport',{}).get('rtt_ms',0)>0)
    if rtts:
        lines.append(f'RTT médian des tunnels actifs : {rtts[len(rtts)//2]:.1f} ms.')
    if not rows:
        lines.append('Aucun groupe comparable sur cet intervalle ; reconnecté, inactif ou aucun trafic.')
    lines += ['≥90 % indique une proximité du plafond, pas une panne.',
              'Une IP publique mobile peut regrouper plusieurs utilisateurs ; le plafond Standard est alors partagé.',
              'Les plafonds restent 500 Ko/s par IP et 4 Mo/s par compte Premium.']
    return PlainText('\n'.join(lines)[:3400])
