#!/usr/bin/env bash
# ==============================================================================
# Script de Nettoyage Automatique des Journaux et Protection Disque
# Exécuté toutes les 15 minutes par systemd timer et cron
# ==============================================================================
set -euo pipefail

LOG_FILE="/var/log/auto-cleanup.log"

# Rotation du log de maintenance s'il dépasse 1 Mo
if [[ -f "$LOG_FILE" ]] && [[ $(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0) -gt 1048576 ]]; then
    truncate -s 0 "$LOG_FILE"
fi

log_msg() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

# 1. Nettoyage journald (plafond strict à 150 Mo / 3 jours)
journalctl --vacuum-size=150M --vacuum-time=3d >/dev/null 2>&1 || true

# 2. Détection et troncature immédiate de tout fichier de log dépassant 100 Mo dans /var/log
find /var/log -path /var/log/journal -prune -o -type f ! -name '*.journal' ! -name '*.journal~' -size +100M -print0 2>/dev/null | while IFS= read -r -d '' bigfile; do
    log_msg "ALERTE: Fichier volumineux détecté et purgé: $bigfile ($(du -h "$bigfile" | cut -f1))"
    truncate -s 0 "$bigfile"
done

# 3. Vérification du pourcentage d'utilisation de la partition racine /
DISK_USAGE=$(df -P / | awk 'NR==2 {gsub("%",""); print $5}')

if [[ "$DISK_USAGE" -ge 70 ]]; then
    log_msg "Avertissement: Espace disque à ${DISK_USAGE}%, déclenchement du nettoyage approfondi"

    # Suppression des archives compressées de logs de plus de 24h
    find /var/log -path /var/log/journal -prune -o -type f ! -name '*.journal' ! -name '*.journal~' \( -name "*.gz" -o -name "*.1" -o -name "*.old" -o -name "*.tail" \) -delete 2>/dev/null || true

    # Nettoyage du cache APT
    apt-get clean >/dev/null 2>&1 || true

    # Compression agressive de journald
    journalctl --vacuum-size=50M >/dev/null 2>&1 || true
fi

# Si le disque est critique (>= 85%), purge d'urgence de tous les logs applicatifs volumineux
DISK_USAGE_AFTER=$(df -P / | awk 'NR==2 {gsub("%",""); print $5}')
if [[ "$DISK_USAGE_AFTER" -ge 85 ]]; then
    log_msg "URGENCE: Espace disque critique à ${DISK_USAGE_AFTER}%, purge d'urgence"
    find /var/log -path /var/log/journal -prune -o -type f ! -name '*.journal' ! -name '*.journal~' -size +20M -exec truncate -s 0 {} + 2>/dev/null || true
fi

exit 0
