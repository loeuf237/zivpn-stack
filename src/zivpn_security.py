"""Read-only security summaries. No decrypted content or payload storage."""
from collections import Counter
import datetime
from zivpn_native_accounting import request, snapshot

# A domain query is a service indicator, not proof that an application was used.
SERVICES = {
 'Telegram': ('telegram.org', 't.me'),
 'WhatsApp': ('whatsapp.com', 'whatsapp.net'),
 'Instagram': ('instagram.com', 'cdninstagram.com'),
 'Facebook / services Meta': ('facebook.com', 'facebook.net', 'fbcdn.net'),
 'YouTube probable': ('youtube.com', 'youtu.be', 'googlevideo.com', 'ytimg.com'),
 'TikTok probable': ('tiktok.com', 'tiktokv.com', 'tiktokcdn.com'),
 'Netflix': ('netflix.com', 'nflxvideo.net', 'nflximg.net'),
 'Google (service indéterminé)': ('google.com', 'googleapis.com', 'gstatic.com'),
 'Microsoft (service indéterminé)': ('microsoft.com', 'windows.com', 'live.com'),
 'Apple (service indéterminé)': ('apple.com', 'icloud.com'),
}
def service(host):
    host = host.lower().rstrip('.')
    for name, domains in SERVICES.items():
        if any(host == d or host.endswith('.'+d) for d in domains):
            return name
    return None

def safe(value):
    # Telegram's legacy Markdown is used by the existing transport.
    value = str(value).replace('\n', ' ').replace('\r', ' ')[:253]
    for char in ('\\', '_', '*', '`', '['):
        value = value.replace(char, '\\'+char)
    return value

def build_security_text(mode='securite', account=None):
    data = request('/security')
    records = data['records']
    if account:
        records = [r for r in records if r['email'].casefold() == account.casefold()]
    records = sorted(records, key=lambda r:r['last_ms'], reverse=True)
    stamp = datetime.datetime.fromtimestamp(data['observed_ms']/1000, datetime.timezone.utc).strftime('%H:%M:%S UTC')
    lines = [f'🔒 Observation réseau — {stamp}',
             'Fenêtre : 30 minutes ; mémoire limitée à 4096 destinations/domaines.',
             'Contenus chiffrés : non accessibles. Aucun message, URL ou fichier conservé.']
    if account:
        lines.append('Filtre compte : '+safe(account))
    if mode in ('securite', 'apps'):
        counts = Counter()
        domains = {}
        for r in records:
            name = service(r['host'])
            if name:
                counts[name] += r['hits']
                domains.setdefault(name, set()).add(r['host'])
        lines.append('\nServices probables — confiance moyenne :')
        for name, count in counts.most_common(8):
            lines.append(f'• {safe(name)} : {count} observations ; '+safe(sorted(domains[name])[0]))
        if not counts:
            lines.append('Aucun service identifiable à partir des domaines observés.')
        lines.append('Une requête DNS peut être une prélecture. Les IP, DNS chiffrés et domaines partagés restent indéterminés.')
    if mode in ('securite', 'destinations', 'dns'):
        kind = 'dns' if mode == 'dns' else 'destination'
        subset = [r for r in records if r['kind'] == kind]
        lines.append('\n'+('Domaines DNS en clair' if kind=='dns' else 'Destinations demandées par les comptes')+' — 12 observations récentes :')
        for r in subset[:12]:
            when = datetime.datetime.fromtimestamp(r['last_ms']/1000, datetime.timezone.utc).strftime('%H:%M:%S')
            lines.append(f"• {safe(r['email'])} → {safe(r['host'])}:{safe(r['port'])} {safe(r['protocol'])} ; {r['hits']} obs. ; {when}")
        if not subset:
            lines.append('Aucune observation disponible dans cette fenêtre.')
        if len(subset)>12:
            lines.append(f'{len(subset)-12} autres regroupements ; utilisez un filtre de compte.')
    if mode=='securite':
        current=snapshot()['sessions'];current=current.values() if isinstance(current,dict) else current
        current=[r for r in current if not account or r['email'].casefold()==account.casefold()]
        lines.append(f'\nTunnels authentifiés actuels : {len(current)}.')
    lines.append('\nTCP : ouvertures réussies ; UDP : envois réussis ; DNS : questions en clair. Ces observations ne comptent pas les personnes ni les pages consultées.')
    from zivpn_rich import RichReport, heading, paragraph, table, details, footer
    blocks = [heading('Observation réseau — '+stamp),
              paragraph('Fenêtre : 30 minutes ; maximum 4096 regroupements en mémoire. Contenus chiffrés non accessibles.')]
    if account:
        blocks.append(paragraph('Compte : '+str(account)))
    if mode in ('securite', 'apps'):
        app_rows=[[name,str(count),sorted(domains[name])[0]] for name,count in counts.most_common(8)]
        blocks.append(heading('Services probables — confiance moyenne'))
        blocks.append(table(['Service','Observations','Domaine indicateur'],app_rows) if app_rows else paragraph('Aucun service identifiable depuis les domaines observés.'))
    if mode in ('securite', 'destinations', 'dns'):
        blocks.append(heading('Domaines DNS en clair' if kind=='dns' else 'Destinations des comptes'))
        network_rows=[[r['email'],r['host'],r['port'],r['protocol'],str(r['hits']),
                       datetime.datetime.fromtimestamp(r['last_ms']/1000,datetime.timezone.utc).strftime('%H:%M:%S')]
                      for r in subset[:12]]
        blocks.append(table(['Compte','Destination','Port','Protocole','Obs.','Heure UTC'],network_rows) if network_rows else paragraph('Aucune observation dans cette fenêtre.'))
        if len(subset)>12:
            blocks.append(paragraph(f'{len(subset)-12} autres regroupements ; utilisez un filtre de compte.'))
    if mode=='securite':
        blocks.append(paragraph(f'Tunnels authentifiés actuels : {len(current)}.'))
    blocks.append(details('Interpréter les observations', [paragraph('TCP : ouvertures réussies ; UDP : envois réussis ; DNS : questions en clair. Les observations ne comptent pas les personnes ni les pages.'),
                   paragraph('Une requête DNS peut être une prélecture. IP, DNS chiffrés et domaines partagés restent indéterminés.')]))
    blocks.append(footer('Confiance moyenne pour les services probables. Aucun message, URL complet ou fichier conservé. Aucun déchiffrement des contenus.'))
    return RichReport(blocks)
