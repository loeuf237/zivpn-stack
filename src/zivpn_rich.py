"""Telegram block reports with a plain-text fallback from the same values."""
from datetime import datetime, timezone


def heading(text):
    return {'type': 'heading', 'text': str(text), 'size': 3}


def paragraph(text):
    return {'type': 'paragraph', 'text': str(text)}


def table(headers, rows):
    cells = []
    for index, row in enumerate([headers]+list(rows)):
        cells.append([dict(text=str(value), align='left', valign='top',
                           **({'is_header': True} if index == 0 else {})) for value in row])
    return {'type': 'table', 'cells': cells, 'is_bordered': True, 'is_striped': True, 'is_compact': True}


def details(summary, blocks):
    return {'type': 'details', 'summary': str(summary), 'blocks': blocks}


def footer(text):
    return {'type': 'footer', 'text': str(text)}


def _lines(block):
    if block['type'] == 'table':
        cells=block['cells'];headers=[c['text'] for c in cells[0]]
        for row in cells[1:]:
            yield ' • '.join(f'{key}: {cell["text"]}' for key,cell in zip(headers,row))
    elif block['type'] == 'details':
        yield '\n'+block['summary']
        for child in block['blocks']:
            yield from _lines(child)
    else:
        yield str(block.get('text',''))


def render_plain(blocks, limit=3500):
    # Keep the scope/measurement caveats even when a long table is abbreviated.
    notes=[line for b in blocks if b['type']=='footer' for line in _lines(b)]
    end='\n\n'+'\n'.join(notes) if notes else ''
    body=[];used=len(end);omitted=False
    for block in blocks:
        if block['type']=='footer':
            continue
        for line in _lines(block):
            if used+len(line)+1 <= limit-100:
                body.append(line);used+=len(line)+1
            else:
                omitted=True
    if omitted:
        body.append('… Certaines lignes sont abrégées dans le rendu texte.')
    return '\n'.join(body)+end


class RichReport(str):
    def __new__(cls, blocks):
        obj=super().__new__(cls,render_plain(blocks))
        obj.rich_message={'blocks':blocks}
        obj.force_plain=False
        return obj

    def as_plain(self):
        result=RichReport(self.rich_message['blocks'])
        result.force_plain=True
        return result


def sessions_report(live, format_bytes, native=True):
    known=sorted((r for r in live.values() if r.get('email')),key=lambda r:r['bytes'],reverse=True)
    blocks=[heading('Sessions authentifiées'),paragraph(f'{len(known)} tunnels ; {len(live)-len(known)} flux non attribués.\nActualisé à '+datetime.now(timezone.utc).strftime('%H:%M:%S UTC'))]
    if known:
        blocks.append(table(['Compte','IP publique','Source','Serveur','Volume du tunnel'],
                            [[str(r['email'])[:80],r['ip'],r['remote_port'],r['port'],format_bytes(r['bytes'])] for r in known[:12]]))
        blocks.append(details('Répartition par compte',[
            table(['Compte','Tunnels','IP distinctes'],[
                [email,sum(r['email']==email for r in known),len({r['ip'] for r in known if r['email']==email})]
                for email in sorted({r['email'] for r in known})[:20]])]))
    else:
        blocks.append(paragraph('Aucun tunnel authentifié actuellement visible.'))
    if len(known)>12:
        blocks.append(paragraph(f'{len(known)-12} autres tunnels non affichés.'))
    blocks.append(footer(('Sessions QUIC confirmées par le serveur.' if native else 'Une entrée conntrack ne confirme pas un tunnel QUIC.')+'\nLes volumes concernent ces tunnels ; consommation historique : /conso. Plusieurs tunnels ne signifient pas plusieurs personnes.'))
    return RichReport(blocks)


def consumption_report(rows,total_clients,started_ms,unattributed,format_bytes,native=True):
    blocks=[heading('Consommation des comptes'),paragraph(f'{total_clients} comptes configurés ; classement des 10 premiers.')]
    values=[]
    for r in rows:
        values.append([r['email'],'Premium' if r['inbound_id']==2 else 'Standard',
                       'Actif' if r['enable'] else 'Bloqué',format_bytes(r['up']),format_bytes(r['down']),format_bytes(r['total_used'])])
    if values:
        blocks.append(table(['Compte','Offre','État','Envoi','Réception','Total'],values))
    else:
        blocks.append(paragraph('Aucun compte configuré.'))
    notes=[]
    if started_ms:
        notes.append('Mesure depuis '+datetime.fromtimestamp(int(started_ms)/1000,timezone.utc).strftime('%Y-%m-%d %H:%M UTC')+'.')
    notes.append('Trafic applicatif authentifié depuis la migration QoS ; historique conservé.' if native else 'Octets UDP du tunnel, en-têtes compris.')
    if unattributed:
        notes.append('Volume non attribué : '+format_bytes(unattributed)+' ; non imputé aux comptes.')
    blocks.append(details('Origine des mesures',[paragraph('\n'.join(notes))]))
    blocks.append(footer('Envoi et réception sont vus du client. Les volumes historiques ne sont pas des débits instantanés.'))
    return RichReport(blocks)
