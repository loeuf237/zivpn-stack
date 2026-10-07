#!/usr/bin/env python3
"""
ZiVPN <-> 3X-UI Bidirectional Synchronization & Advanced Telegram Bot Master Daemon
- Full dual-instance integration: Standard (UDP 5667 - 500 Ko/s) & VIP / Limited (UDP 5668 - 4 Mo/s)
- Comprehensive Low-Level Admin: CRUD accounts, dynamic QoS limits, live conntrack telemetry,
  application breakdown, P2P/Torrent toggling, session kicking, root shell execution, system logs.
- Interactive French Telegram Bot with rich inline buttons and instant mobile configs (.ziv / Base64).
- Real-time traffic accounting and quota / expiry enforcement.
"""
import sqlite3
import sys
sys.path.append("/usr/local/lib")
from zivpn_policy import ACCOUNT_QUERY, access_denial
import zivpn_accounting
import json
import csv
import time
import os
import shutil
import subprocess
import threading
import requests
import re
import base64
import tempfile
import signal
from zivpn_telegram import Transport, Tasks
from collections import defaultdict

DB_PATH = "/etc/x-ui/x-ui.db"
CONFIG_FILE = "/etc/zivpn/config.json"
CONFIG_LIMITED_FILE = "/etc/zivpn/config-limited.json"
CLIENTS_CSV = "/etc/zivpn/clients.csv"
from zivpn_settings import load_settings, default_interface
DEPLOYMENT = load_settings()
SERVER_PUBLIC_IP = DEPLOYMENT["server_address"]
NETWORK_INTERFACE = DEPLOYMENT.get("network_interface") or default_interface()
TG_TOKEN = open("/etc/zivpn/telegram.token", encoding="utf-8").read().strip()
PRIMARY_ADMIN_ID = DEPLOYMENT["primary_admin_id"]
API_BASE = f"https://api.telegram.org/bot{TG_TOKEN}"
TELEGRAM = Transport(API_BASE)
from zivpn_health import Monitor, quality_report
HEALTH = Monitor(interface=NETWORK_INTERFACE)

# ==========================================
# Core Database & Admin Helpers
# ==========================================

def get_admin_ids():
    admin_ids = {PRIMARY_ADMIN_ID}
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = 'tgBotChatId'")
        row = c.fetchone()
        if row and row[0]:
            for part in str(row[0]).split(","):
                part = part.strip()
                if part.isdigit():
                    admin_ids.add(int(part))
        conn.close()
    except Exception:
        pass
    return admin_ids

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.row_factory = sqlite3.Row
    return conn

def read_json_config(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def write_json_config(path, cfg):
    temp_file = path + ".tmp"
    with open(temp_file, "w") as f:
        os.fchmod(f.fileno(), 0o600)
        json.dump(cfg, f, indent=2)
    os.replace(temp_file, path)

def format_bytes(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.2f} Ko"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} Mo"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} Go"

# ==========================================
# ZiVPN Metrics & Status Helpers
# ==========================================


def is_real_client_ip(ip):
    if not ip:
        return False
    if ip.startswith("127.") or ip.startswith("172.16.") or ip.startswith("10.") or ip.startswith("192.168."):
        return False
    if ip in (SERVER_PUBLIC_IP, "0.0.0.0", "localhost", "::1"):
        return False
    return True

def get_live_connections():
    """Observed UDP sockets, named only by their recorded authentication."""
    try:
        return zivpn_accounting.live_connections(DB_PATH)
    except Exception as e:
        print(f"[Live Accounting Error] {type(e).__name__}")
        return {}


def native_qos_enabled():
    return zivpn_accounting.native_enabled()


def get_server_metrics():
    """Returns uptime, CPU load, RAM usage, and disk usage"""
    try:
        with open('/proc/uptime', 'r') as f:
            uptime_sec = float(f.readline().split()[0])
        days = int(uptime_sec // 86400)
        hours = int((uptime_sec % 86400) // 3600)
        mins = int((uptime_sec % 3600) // 60)
        uptime_str = f"{days}j {hours}h {mins}m"
    except Exception:
        uptime_str = "Indisponible"

    try:
        mem = {}
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                parts = line.split(':')
                if len(parts) == 2:
                    mem[parts[0].strip()] = int(parts[1].split()[0])
        total_mem = mem.get('MemTotal', 1) / 1024
        avail_mem = mem.get('MemAvailable', 0) / 1024
        used_mem = total_mem - avail_mem
        mem_pct = (used_mem / total_mem) * 100
        ram_str = f"{used_mem:.0f} Mo / {total_mem:.0f} Mo ({mem_pct:.1f}%)"
    except Exception:
        ram_str = "Indisponible"

    try:
        load = os.getloadavg()
        cpu_str = f"{load[0]:.2f}, {load[1]:.2f}, {load[2]:.2f}"
    except Exception:
        cpu_str = "Indisponible"

    try:
        disk = shutil.disk_usage('/')
        disk_total = disk.total / (1024**3)
        disk_used = disk.used / (1024**3)
        disk_pct = (disk.used / disk.total) * 100
        disk_str = f"{disk_used:.1f} Go / {disk_total:.1f} Go ({disk_pct:.1f}%)"
    except Exception:
        disk_str = "Indisponible"

    try:
        zivpn_active = subprocess.check_output(['systemctl', 'is-active', 'zivpn'], timeout=10).decode().strip()
    except Exception:
        zivpn_active = "inactive"

    try:
        zivpn_ltd_active = subprocess.check_output(['systemctl', 'is-active', 'zivpn-limited'], timeout=10).decode().strip()
    except Exception:
        zivpn_ltd_active = "inactive"

    if native_qos_enabled():
        zivpn_ltd_active = zivpn_active

    try:
        xui_active = subprocess.check_output(['systemctl', 'is-active', 'x-ui'], timeout=10).decode().strip()
    except Exception:
        xui_active = "inactive"

    return {
        "uptime": uptime_str,
        "ram": ram_str,
        "cpu": cpu_str,
        "disk": disk_str,
        "zivpn": zivpn_active,
        "zivpn_limited": zivpn_ltd_active,
        "xui": xui_active
    }

# ==========================================
# Telegram Bot API Methods
# ==========================================

def deliver_rich_report(message, chat_id, message_id=None, reply_markup=None):
    if reply_markup:
        from zivpn_ui import style_keyboard
        jobs = globals().get('DIAGNOSTICS')
        reply_markup = style_keyboard(reply_markup, chat_id == PRIMARY_ADMIN_ID,
                                     bool(jobs and jobs.running(chat_id)))

    """None means a definite rejection that permits a text fallback."""
    method = "sendRichMessage" if message_id is None else "editMessageText"
    payload = {"chat_id": chat_id, "rich_message": message.rich_message}
    if message_id is not None:
        payload["message_id"] = message_id
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        result = TELEGRAM.call(method, payload)
    except Exception as error:
        # Delivery may have succeeded before the response timed out: do not duplicate it.
        print(f"[Telegram Rich Transport Error] {type(error).__name__}")
        return {"ok": False, "delivery_uncertain": True}
    if result.get("ok"):
        return result
    code = result.get("error_code")
    if message_id is not None and code == 400 and "message is not modified" in result.get("description", "").lower():
        return {"ok": True, "unchanged": True}
    print(f"[Telegram Rich Rejection] method={method} code={code} reason={result.get('failure_reason', 'api_rejection')} uncertain={result.get('delivery_uncertain', False)}")
    if code in (400, 404):
        return None
    return result


def send_telegram(message, chat_id=PRIMARY_ADMIN_ID, reply_markup=None):
    if reply_markup:
        from zivpn_ui import style_keyboard
        jobs = globals().get('DIAGNOSTICS')
        reply_markup = style_keyboard(reply_markup, chat_id == PRIMARY_ADMIN_ID,
                                     bool(jobs and jobs.running(chat_id)))

    is_report = hasattr(message, 'rich_message')
    if is_report and not getattr(message, 'force_plain', False):
        result = deliver_rich_report(message, chat_id, None, reply_markup)
        if result is not None:
            return result

    truncated = len(message) > 3500
    if truncated:
        message = message[:3400] + "\n… Liste abrégée pour Telegram."
    try:
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown"
        }
        if truncated or is_report or getattr(message, "plain_text", False):
            payload.pop("parse_mode", None)
        if reply_markup:
            payload["reply_markup"] = reply_markup
        res = TELEGRAM.call("sendMessage", payload)
        if not res.get("ok") and "can't parse entities" in res.get("description", "").lower():
            payload.pop("parse_mode", None)
            return TELEGRAM.call("sendMessage", payload)
        if not res.get("ok"):
            print(f"[Telegram API Rejection] method=sendMessage code={res.get('error_code')} reason={res.get('failure_reason', 'api_rejection')} uncertain={res.get('delivery_uncertain', False)}")
        return res
    except Exception as e:
        print(f"[TG Error] {type(e).__name__}")
        return None

def edit_telegram_message(message, chat_id, message_id, reply_markup=None):
    if reply_markup:
        from zivpn_ui import style_keyboard
        jobs = globals().get('DIAGNOSTICS')
        reply_markup = style_keyboard(reply_markup, chat_id == PRIMARY_ADMIN_ID,
                                     bool(jobs and jobs.running(chat_id)))

    is_report = hasattr(message, 'rich_message')
    if is_report and not getattr(message, 'force_plain', False):
        result = deliver_rich_report(message, chat_id, message_id, reply_markup)
        if result is not None:
            return result

    truncated = len(message) > 3500
    if truncated:
        message = message[:3400] + "\n… Liste abrégée pour Telegram."
    try:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": message,
            "parse_mode": "Markdown"
        }
        if truncated or is_report or getattr(message, "plain_text", False):
            payload.pop("parse_mode", None)
        if reply_markup:
            payload["reply_markup"] = reply_markup
        res = TELEGRAM.call("editMessageText", payload)
        if not res.get("ok") and "can't parse entities" in res.get("description", "").lower():
            payload.pop("parse_mode", None)
            return TELEGRAM.call("editMessageText", payload)
        if not res.get("ok"):
            print(f"[Telegram API Rejection] method=editMessageText code={res.get('error_code')} reason={res.get('failure_reason', 'api_rejection')} uncertain={res.get('delivery_uncertain', False)}")
        return res
    except Exception as e:
        print(f"[TG Edit Error] {type(e).__name__}")
        return None

def answer_callback(callback_id, text=None):
    try:
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        TELEGRAM.call("answerCallbackQuery", payload)
    except Exception:
        pass

def send_backup_file(chat_id):
    try:
        # SQLite backup includes committed WAL data; a raw DB copy does not.
        with tempfile.TemporaryDirectory(prefix="zivpn-backup-") as directory:
            path = os.path.join(directory, "x-ui.db")
            with sqlite3.connect(DB_PATH, timeout=5) as source, sqlite3.connect(path) as target:
                deadline = time.monotonic() + 15
                def progress(status, remaining, total):
                    if time.monotonic() > deadline:
                        raise TimeoutError("SQLite backup timeout")
                source.backup(target, pages=128, progress=progress)
            os.chmod(path, 0o600)
            with open(path, "rb") as document:
                result = TELEGRAM.call("sendDocument", {"chat_id": chat_id, "caption": "Sauvegarde cohérente 3X-UI & ZiVPN"},
                                       files={"document": ("x-ui.db", document)})
            if not result.get("ok"):
                send_telegram("⚠️ Sauvegarde non confirmée par Telegram. Vérifiez la réception avant de réessayer.", chat_id)
            return bool(result.get("ok"))
    except Exception as error:
        print(f"[Backup Error] {type(error).__name__}")
        send_telegram("❌ Sauvegarde indisponible. Consultez les journaux du bot.", chat_id)
        return False

# ==========================================
# Low-Level Administration & Operations
# ==========================================

def generate_ziv_config(username, password, inbound_id):
    is_vip = (inbound_id == 2)
    port = 5667 if native_qos_enabled() else (5668 if is_vip else 5667)
    speed = "Plafonné à 4 Mo/s (32 Mbps)" if is_vip else "Standard (500 Ko/s / 4 Mbps)"
    # Android exports its own encrypted .ziv format; do not invent import codes.
    return {"port": port, "speed": speed, "username": username, "password": password}

def add_account(username, password, speed_mo_s=None):
    username = username.strip()
    password = password.strip()
    is_vip = False
    if speed_mo_s is not None:
        try:
            sp = float(speed_mo_s)
            if sp > 1.0:
                is_vip = True
        except ValueError:
            if str(speed_mo_s).lower() in ("vip", "ltd", "limited", "dedie"):
                is_vip = True
    target_inbound = 2 if is_vip else 1

    conn = get_db()
    c = conn.cursor()
    now_ms = int(time.time() * 1000)

    c.execute("SELECT id FROM clients WHERE email = ?", (username,))
    existing = c.fetchone()
    if existing:
        client_id = existing[0]
        c.execute("UPDATE clients SET password = ?, enable = 1, updated_at = ? WHERE id = ?", (password, now_ms, client_id))
        c.execute("""
            INSERT INTO client_traffics (inbound_id, enable, email, up, down, expiry_time, total)
            VALUES (?, 1, ?, 0, 0, 0, 0)
            ON CONFLICT(email) DO UPDATE SET inbound_id=excluded.inbound_id, enable=1
        """, (target_inbound, username))
        c.execute("UPDATE client_inbounds SET inbound_id = ? WHERE client_id = ?", (target_inbound, client_id))
    else:
        c.execute("""
            INSERT INTO clients (email, password, enable, total_gb, expiry_time, created_at, updated_at)
            VALUES (?, ?, 1, 0, 0, ?, ?)
        """, (username, password, now_ms, now_ms))
        client_id = c.lastrowid
        c.execute("""
            INSERT INTO client_traffics (inbound_id, enable, email, up, down, expiry_time, total)
            VALUES (?, 1, ?, 0, 0, 0, 0)
        """, (target_inbound, username))
        c.execute("""
            INSERT OR IGNORE INTO client_inbounds (client_id, inbound_id, created_at)
            VALUES (?, ?, ?)
        """, (client_id, target_inbound, now_ms))

    conn.commit()
    conn.close()

    # Sync clients.csv
    csv_rows = []
    if os.path.exists(CLIENTS_CSV):
        try:
            with open(CLIENTS_CSV, "r", newline="") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                for row in reader:
                    if len(row) >= 2 and row[0].strip() and row[0].strip() != username:
                        csv_rows.append((row[0].strip(), row[1].strip()))
        except Exception:
            pass
    csv_rows.append((username, password))
    with open(CLIENTS_CSV, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["client", "password"])
        for u, p in csv_rows:
            writer.writerow([u, p])

    # Refresh daemons
    sync_accounts()
    return generate_ziv_config(username, password, target_inbound)

def delete_account(username):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, password FROM clients WHERE email = ?", (username,))
    row = c.fetchone()
    if not row:
        conn.close()
        return False, f"❗ Compte `{username}` introuvable dans 3X-UI."
    client_id, pwd = row[0], row[1]
    c.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    c.execute("DELETE FROM client_traffics WHERE email = ?", (username,))
    c.execute("DELETE FROM client_inbounds WHERE client_id = ?", (client_id,))
    conn.commit()
    conn.close()

    # Update clients.csv
    if os.path.exists(CLIENTS_CSV):
        try:
            csv_rows = []
            with open(CLIENTS_CSV, "r", newline="") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                for r in reader:
                    if len(r) >= 2 and r[0].strip() != username:
                        csv_rows.append((r[0].strip(), r[1].strip()))
            with open(CLIENTS_CSV, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["client", "password"])
                for u, p in csv_rows:
                    writer.writerow([u, p])
        except Exception:
            pass

    # Remove from config files
    for cfg_path, svc in [(CONFIG_FILE, "zivpn"), (CONFIG_LIMITED_FILE, "zivpn-limited")]:
        if os.path.exists(cfg_path):
            cfg = read_json_config(cfg_path)
            pwds = cfg.get("auth", {}).get("config", [])
            if pwd in pwds:
                pwds.remove(pwd)
                cfg["auth"]["config"] = sorted(pwds)
                write_json_config(cfg_path, cfg)
                if not native_qos_enabled():
                    subprocess.run(["systemctl", "reload-or-restart", svc], capture_output=True, timeout=10)

    kick_client(username)
    return True, f"🗑 *Compte `{username}` définitivement supprimé.*"

def toggle_account_block(username, block=True):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, password FROM clients WHERE email = ?", (username,))
    row = c.fetchone()
    if not row:
        conn.close()
        return False, f"❗ Compte `{username}` introuvable."
    new_val = 0 if block else 1
    c.execute("UPDATE clients SET enable = ? WHERE email = ?", (new_val, username))
    c.execute("UPDATE client_traffics SET enable = ? WHERE email = ?", (new_val, username))
    conn.commit()
    conn.close()

    sync_accounts()
    if block:
        kick_client(username)
    action_str = "🚫 *bloqué (accès suspendu)*" if block else "🟢 *débloqué (accès rétabli)*"
    return True, f"Compte `{username}` {action_str} avec succès."

def kick_client(target):
    target = target.strip()
    if native_qos_enabled():
        import zivpn_native_accounting
        if target.lower() in ('tout', '*'):
            target = 'all'
        if target.lower() in ('premium-example',):
            target = 'premium-example'
        live = get_live_connections()
        ips = sorted({row['ip'] for row in live.values()
                      if target == 'all' or row['email'] == target or row['ip'] == target})
        return zivpn_native_accounting.kick(target), ips
    kicked_ips = []
    if re.match(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$", target):
        kicked_ips.append(target)
    else:
        if target.lower() in ("zivpn_legacy_vip", "landry"):
            try:
                out = subprocess.check_output(["ipset", "list", "zivpn_legacy_vip"], timeout=10).decode()
                ips = re.findall(r"^([0-9.]+)\s+timeout", out, re.MULTILINE)
                kicked_ips.extend(ips)
            except Exception:
                pass
        live = get_live_connections()
        if target.lower() in ("all", "tout", "*"):
            kicked_ips.extend([c["ip"] for c in live.values()])
        elif not kicked_ips:
            for c in live.values():
                kicked_ips.append(c["ip"])

    count = 0
    for ip in set(kicked_ips):
        try:
            subprocess.run(["conntrack", "-D", "-s", ip], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            subprocess.run(["conntrack", "-D", "-d", ip], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            subprocess.run(["conntrack", "-D", "-p", "udp", "--orig-src", ip], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            subprocess.run(["conntrack", "-D", "-p", "udp", "--reply-src", ip], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            count += 1
        except Exception:
            pass
    return count, list(set(kicked_ips))

def set_qos_limit(target, speed_mo_s):
    if native_qos_enabled():
        return False, ("Les offres sont fixées à 4 Mo/s par compte Premium et 500 Ko/s par IP Standard. "
                       "Le réglage de plafonds personnalisés n’est pas disponible actuellement.")
    try:
        speed_float = float(speed_mo_s)
        mbps = max(1, int(speed_float * 8))
    except ValueError:
        return False, "❌ Vitesse invalide. Entrez une valeur en Mo/s (ex: `4`, `2`, `0.5`)."

    target_l = target.lower().strip()
    if target_l in ("vip", "zivpn_legacy_vip", "landry"):
        try:
            subprocess.run([
                "tc", "class", "change", "dev", "ifb0", "classid", "1:10",
                "htb", "rate", f"{mbps}Mbit", "ceil", f"{mbps}Mbit",
                "burst", "128Kb", "cburst", "128Kb"
            ], check=True, timeout=10)
            return True, f"⚡ *Plafond VIP ajusté à {speed_float} Mo/s ({mbps} Mbps)*\n• Classe noyau HTB `1:10` mise à jour à chaud sur le port principal."
        except Exception as e:
            return False, f"❌ Erreur QoS VIP : {e}"

    elif target_l in ("standard", "all", "public", "standard-example"):
        try:
            subprocess.run([
                "tc", "class", "change", "dev", "ifb0", "classid", "1:20",
                "htb", "rate", f"{mbps}Mbit", "ceil", f"{mbps}Mbit",
                "burst", "32Kb", "cburst", "32Kb"
            ], check=True, timeout=10)
            return True, f"⚡ *Plafond Standard ajusté à {speed_float} Mo/s ({mbps} Mbps)*\n• Classe noyau HTB `1:20` mise à jour à chaud sur le port principal."
        except Exception as e:
            return False, f"❌ Erreur QoS Standard : {e}"
    else:
        return False, f"Cible `{target}` non reconnue. Utilisez `vip` ou `standard`."

def p2p_toggle(action="status"):
    action = action.lower().strip()
    if action == "status":
        try:
            r = subprocess.run(["iptables", "-C", "FORWARD", "-j", "BLOCK-P2P"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            is_active = (r.returncode == 0)
        except Exception:
            is_active = False
        return is_active, f"🛡 *Protection P2P / Torrent :* {'🟢 *ACTIVE*' if is_active else '🔴 *DÉSACTIVÉE*'}\n\n_Le filtrage des signatures DHT / BitTorrent est {'pleinement opérationnel' if is_active else 'actuellement inactif'}._"

    elif action in ("on", "enable", "activer"):
        try:
            r = subprocess.run(["iptables", "-C", "FORWARD", "-j", "BLOCK-P2P"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            if r.returncode != 0:
                subprocess.run(["iptables", "-I", "FORWARD", "1", "-j", "BLOCK-P2P"], check=True, timeout=10)
            r2 = subprocess.run(["iptables", "-C", "OUTPUT", "-j", "BLOCK-P2P"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            if r2.returncode != 0:
                subprocess.run(["iptables", "-I", "OUTPUT", "1", "-j", "BLOCK-P2P"], check=True, timeout=10)
            return True, "🟢 *Protection P2P / Torrent ACTIVÉE dans le pare-feu noyau.*"
        except Exception as e:
            return False, f"❌ Échec de l'activation P2P : {e}"

    elif action in ("off", "disable", "desactiver"):
        try:
            deadline = time.monotonic() + 15
            while subprocess.run(["iptables", "-D", "FORWARD", "-j", "BLOCK-P2P"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0:
                if time.monotonic() > deadline:
                    raise TimeoutError("P2P rule removal timeout")
            while subprocess.run(["iptables", "-D", "OUTPUT", "-j", "BLOCK-P2P"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0:
                if time.monotonic() > deadline:
                    raise TimeoutError("P2P rule removal timeout")
            return True, "🔴 *Protection P2P / Torrent DÉSACTIVÉE dans le pare-feu.*"
        except Exception as e:
            return False, f"❌ Échec de la désactivation P2P : {e}"

def execute_admin_command(cmd):
    try:
        process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=25)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
            raise
        res = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
        stdout = res.stdout.strip()
        stderr = res.stderr.strip()
        code = res.returncode
        out = ""
        if stdout:
            out += stdout
        if stderr:
            if out:
                out += "\n--- STDERR ---\n"
            out += stderr
        if not out:
            out = "(Succès - Aucune sortie texte)"
        if len(out) > 3500:
            out = out[:3500] + "\n... [Tronqué pour Telegram]"
        icon = "🟢" if code == 0 else "🔴"
        return f"{icon} *Exécution Shell (Code {code}) :*\n```bash\n{out}\n```"
    except subprocess.TimeoutExpired:
        return "⏰ *Erreur :* Commande interrompue après 25 secondes de dépassement."
    except Exception as e:
        return f"❌ *Erreur système :* `{type(e).__name__}`"

def get_service_logs(service_name="zivpn"):
    svc_map = {
        "zivpn": "zivpn",
        "standard": "zivpn",
        "limited": "zivpn-limited",
        "zivpn-limited": "zivpn-limited",
        "vip": "zivpn-limited",
        "xui": "x-ui",
        "x-ui": "x-ui",
        "sync": "zivpn-xui-sync",
        "bot": "zivpn-xui-sync"
    }
    svc = svc_map.get(service_name.lower().strip(), "zivpn")
    if native_qos_enabled() and svc == 'zivpn-limited':
        svc = 'zivpn'
    try:
        out = subprocess.check_output(["journalctl", "-u", svc, "-n", "25", "--no-pager"], timeout=5).decode("utf-8", errors="ignore")
        if len(out) > 3500:
            out = out[-3500:]
        return f"📋 *Logs Récent Service : `{svc}`*\n```text\n{out}\n```"
    except Exception as e:
        return f"❌ Erreur lecture journalctl `{svc}`: {type(e).__name__}"

# ==========================================
# UI Keyboards & Message Views
# ==========================================

def get_main_menu_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Stats en direct", "callback_data": "menu_stats"},
                {"text": "👥 Sessions Actives", "callback_data": "menu_users"}
            ],
            [
                {"text": "📱 Flux Applications", "callback_data": "menu_apps"},
                {"text": "⚙️ Contrôle QoS & Débit", "callback_data": "menu_qos"}
            ],
            [
                {"text": "👤 Gestion Comptes", "callback_data": "menu_accounts"},
                {"text": "🛡 Protection P2P", "callback_data": "menu_p2p"}
            ],
            [
                {"text": "🖥 État Serveur", "callback_data": "menu_status"},
                {"text": "📈 Consommation Go", "callback_data": "menu_conso"}
            ],
            [{"text": "🔎 Diagnostic progressif", "callback_data": "menu_diagnostic"}],
            [{"text": "🩺 Santé et erreurs", "callback_data": "menu_health"},
             {"text": "📶 Qualité et plafonds", "callback_data": "menu_quality"}],
            [
                {"text": "🔄 Redémarrer Services", "callback_data": "menu_restart"},
                {"text": "💾 Sauvegarder DB", "callback_data": "menu_backup"}
            ],
            [
                {"text": "🔄 Rafraîchir", "callback_data": "menu_main"},
                {"text": "❌ Fermer", "callback_data": "menu_close"}
            ]
        ]
    }

def get_back_keyboard(refresh_action=None):
    row = [{"text": "🔙 Menu Principal", "callback_data": "menu_main"}]
    if refresh_action:
        row.append({"text": "🔄 Actualiser", "callback_data": refresh_action})
    return {"inline_keyboard": [row]}

def get_qos_keyboard():
    if native_qos_enabled():
        keyboard = get_back_keyboard('menu_qos')
        keyboard['inline_keyboard'].insert(0, [{'text': 'Plafonds fixes : modification indisponible', 'disabled': {}}])
        return keyboard
    return {
        "inline_keyboard": [
            [{"text": "⚡ Réinitialiser Baux IP (Flush)", "callback_data": "qos_flush_ips"}],
            [{"text": "🔄 Actualiser", "callback_data": "menu_qos"}, {"text": "🔙 Menu Principal", "callback_data": "menu_main"}]
        ]
    }

def get_p2p_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "🟢 Activer Blocage P2P", "callback_data": "p2p_on"},
                {"text": "🔴 Désactiver Blocage P2P", "callback_data": "p2p_off"}
            ],
            [
                {"text": "🔄 Actualiser", "callback_data": "menu_p2p"},
                {"text": "🔙 Menu Principal", "callback_data": "menu_main"}
            ]
        ]
    }

def get_accounts_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "🔄 Actualiser", "callback_data": "menu_accounts"},
                {"text": "🔙 Menu Principal", "callback_data": "menu_main"}
            ]
        ]
    }

def build_main_menu_text():
    live = get_live_connections()
    metrics = get_server_metrics()
    z_icon = "🟢" if metrics["zivpn"] == "active" else "🔴"
    x_icon = "🟢" if metrics["xui"] == "active" else "🔴"

    return (
        "🚀 *Panneau d'Administration ZiVPN Master*\n"
        "Contrôle automatique et transparent du serveur ZiVPN.\n\n"
        f"• *ZiVPN Server UDP (Port 5667) :* {z_icon} `{metrics['zivpn']}`\n"
        f"• *Premium :* ⭐ `4 Mo/s partagés par compte`\n"
        f"• *Standard :* 🔹 `500 Ko/s partagés par IP`\n"
        f"• *3X-UI Panel (Port 2053) :* {x_icon} `{metrics['xui']}`\n"
        f"• *Flux UDP observés :* `{len(live)} socket(s)`\n"
        f"• *Uptime Serveur :* `{metrics['uptime']}`\n\n"
        "👇 _Sélectionnez une option ou tapez une commande :_"
    )

def build_status_text():
    m = get_server_metrics()
    z_icon = "🟢" if m["zivpn"] == "active" else "🔴"
    zl_icon = "🟢" if m["zivpn_limited"] == "active" else "🔴"
    x_icon = "🟢" if m["xui"] == "active" else "🔴"

    return (
        "🖥 *État Général du Serveur & des Services*\n\n"
        f"⏱ *Uptime :* `{m['uptime']}`\n"
        f"⚡ *Charge CPU :* `{m['cpu']}`\n"
        f"🧠 *Mémoire RAM :* `{m['ram']}`\n"
        f"💾 *Espace Disque :* `{m['disk']}`\n\n"
        "🔧 *Services Noyau & Daemons :*\n"
        f"• *ZiVPN Standard (5667 - 500Ko/s) :* {z_icon} `{m['zivpn']}`\n"
        f"• *Premium (port 5667, compatibilité 5668) :* {zl_icon} `{m['zivpn_limited']}`\n"
        f"• *3X-UI Web (Port 2053) :* {x_icon} `{m['xui']}`\n"
        f"• *Daemon Synchro & Bot :* 🟢 `actif`\n\n"
        f"_Actualisé à {time.strftime('%H:%M:%S')}_"
    )

def build_stats_text():
    live = get_live_connections()
    known = sum(bool(row.get("email")) for row in live.values())
    conn = get_db()
    try:
        rows = conn.execute("SELECT tag,COALESCE(up,0),COALESCE(down,0) FROM inbounds "
                            "WHERE tag IN ('inbound-zivpn','inbound-zivpn-limited')").fetchall()
        meta = dict(conn.execute("SELECT key,value FROM zivpn_accounting_meta"))
    finally:
        conn.close()
    totals = {row[0]: row[1]+row[2] for row in rows}
    interval = int(meta.get("last_interval_ms", "0")) / 1000
    sampled = int(meta.get("last_sample_ms", "0")) / 1000
    age = max(0, time.time() - sampled)
    rate = "indisponible"
    if interval > 0 and age < 30:
        up = int(meta.get("last_delta_up", "0")) / interval
        down = int(meta.get("last_delta_down", "0")) / interval
        if native_qos_enabled():
            def native_rate(value):
                return f"{value/1000000:.2f} Mo/s" if value >= 1000000 else f"{value/1000:.1f} Ko/s"
            rate = f"↑ {native_rate(up)} | ↓ {native_rate(down)}"
        else:
            rate = f"↑ {format_bytes(int(up))}/s | ↓ {format_bytes(int(down))}/s"
    start = int(meta.get("measurement_started_ms", "0")) / 1000
    date = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(start))
    return (
        "📊 Statistiques ZIVPN\n\n"
        f"📈 Consommation cumulée depuis {date} :\n"
        f"   Port principal : {format_bytes(totals.get('inbound-zivpn', 0))}\n"
        f"   Port VIP : {format_bytes(totals.get('inbound-zivpn-limited', 0))}\n\n"
        f"⚡ Débit moyen du dernier relevé : {rate}\n"
        f"   Relevé il y a {age:.0f} s.\n\n"
        f"Sockets authentifiés observés : {known}\n"
        f"Flux UDP non attribués : {len(live)-known}\n"
        + ("Sessions QUIC authentifiées confirmées par le serveur.\n"
         "Trafic applicatif TCP/UDP. Détail par compte : /conso" if native_qos_enabled() else
         "Les flux expirent ; leurs volumes ne sont pas des totaux historiques.\n"
         "Octets du tunnel UDP, overhead compris. Détail par compte : /conso")
    )


def build_users_text():
    from zivpn_rich import sessions_report
    return sessions_report(get_live_connections(), format_bytes, native_qos_enabled())


def build_live_connections_detailed_text():
    live = get_live_connections()
    if not live:
        return ("👥 *Sessions Actives*\n\nAucune session authentifiée active sur le serveur." if native_qos_enabled() else
                "👥 *Connexions UDP Actives (Bas-Niveau)*\n\nAucune session active enregistrée dans la table conntrack.")

    lines = [f"🌐 *Connexions UDP Actives ({len(live)} session(s))* \n"]
    idx = 1
    total_b = 0
    total_p = 0
    for key, data in sorted(live.items(), key=lambda x: x[1]['bytes'], reverse=True):
        b = data['bytes']
        p = data['packets']
        total_b += b
        total_p += p
        ip = data['ip']

        client_tag = data.get("email") or "Non attribué"
        lines.append(
            f"*{idx}.* `{ip}` — *{client_tag}*\n"
            f"   • Données : *{format_bytes(b)}* | Paquets : `{p:,}`\n"
            f"   • Couper session : `/kick {ip}`"
        )
        idx += 1

    lines.append(f"\n📊 *Trafic des sessions affichées :* `{format_bytes(total_b)}` ({total_p:,} paquets)")
    return "\n".join(lines)

def build_apps_traffic_text():
    try:
        out = subprocess.check_output(["conntrack", "-L"], stderr=subprocess.DEVNULL, timeout=10).decode("utf-8", errors="ignore")
    except Exception as e:
        return f"Erreur lecture conntrack: {e}"

    app_traffic = defaultdict(int)
    total_tracked = 0

    for line in out.splitlines():
        if not (line.startswith("tcp") or line.startswith("udp")):
            continue
        parts = line.split()
        d = {}
        is_reply = False
        for p in parts:
            if "=" in p:
                k, v = p.split("=", 1)
                if k == "src":
                    if "src" in d:
                        is_reply = True
                key = (k + "_reply") if is_reply else k
                d[key] = v
        if d.get("src") == "172.16.0.4" and "dst" in d:
            dst = d["dst"]
            b_total = int(d.get("bytes_reply", 0)) + int(d.get("bytes", 0))
            dport = d.get("dport", "")

            if dport == "53" or dst in ("8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1"):
                cat = "DNS & Résolution Noms"
            elif dst.startswith("149.154.") or dst.startswith("91.108."):
                cat = "Telegram Messenger"
            elif dst.startswith("157.240.") or dst.startswith("31.13."):
                cat = "WhatsApp / Facebook / Meta"
            elif dst.startswith("142.250.") or dst.startswith("172.217.") or dst.startswith("216.58.") or dst.startswith("173.194."):
                cat = "YouTube / Google Services"
            elif dst.startswith("104.") or dst.startswith("172.64.") or dst.startswith("172.67."):
                cat = "Cloudflare CDN"
            elif dst.startswith("156.251.") or dst.startswith("82.27.") or dst.startswith("161.117."):
                cat = "TikTok / Video Streaming CDN"
            elif dst.startswith("20.") or dst.startswith("52.") or dst.startswith("40."):
                cat = "Microsoft / Azure"
            elif dport == "443":
                cat = "HTTPS Web & Applications"
            elif dport == "80":
                cat = "HTTP Web Standard"
            else:
                cat = f"Flux TCP/UDP (Port {dport})"

            app_traffic[cat] += b_total
            total_tracked += b_total

    lines = ["📱 *Consommation par Application en Direct*\n"]
    if not app_traffic or total_tracked == 0:
        lines.append("ℹ️ _Aucun flux applicatif actif capturé pour le moment._")
    else:
        lines.append(f"📦 *Volume analysé en mémoire :* `{format_bytes(total_tracked)}`\n")
        idx = 1
        for cat, b in sorted(app_traffic.items(), key=lambda x: x[1], reverse=True)[:10]:
            pct = (b / total_tracked) * 100 if total_tracked > 0 else 0
            lines.append(f"*{idx}. {cat}*\n   • Données : *{format_bytes(b)}* ({pct:.1f}%)")
            idx += 1

    lines.append(f"\n_Inspection en temps réel des flux réseau traversant le serveur._")
    return "\n".join(lines)

def build_qos_status_text():
    if native_qos_enabled():
        live = get_live_connections()
        premium = defaultdict(int)
        standard_ips = set()
        for row in live.values():
            if row['vip']:
                premium[row['email']] += 1
            else:
                standard_ips.add(row['ip'])
        lines = ['⚙️ Limites de débit par identité authentifiée\n',
                 '⭐ Premium : 4 Mo/s au total par compte, toutes IP et connexions confondues.',
                 '🔹 Standard : 500 Ko/s au total par IP publique, tous appareils confondus.',
                 'TCP et UDP partagent le plafond ; montant et descendant cumulés.\n',
                 f'IP Standard connectées : {len(standard_ips)}',
                 f'Comptes Premium connectés : {len(premium)}']
        for email, count in sorted(premium.items())[:12]:
            lines.append(f'• {email[:80]} : {count} connexion(s), 4 Mo/s partagés')
        return '\n'.join(lines)
    try:
        out_tc = subprocess.check_output(["tc", "-s", "class", "show", "dev", "ifb0"], timeout=10).decode()
    except Exception as e:
        out_tc = f"Erreur tc: {e}"

    try:
        out_ipset = subprocess.check_output(["ipset", "list", "zivpn_legacy_vip"], timeout=10).decode()
        m_ips = re.findall(r"^([0-9.]+)\s+timeout\s+(\d+)", out_ipset, re.MULTILINE)
    except Exception:
        m_ips = []

    m_10 = re.search(r"class htb 1:10 root.*?rate ([0-9a-zA-Z]+) ceil ([0-9a-zA-Z]+).*?Sent (\d+) bytes (\d+) pkt", out_tc, re.DOTALL)
    m_20 = re.search(r"class htb 1:20 root.*?rate ([0-9a-zA-Z]+) ceil ([0-9a-zA-Z]+).*?Sent (\d+) bytes (\d+) pkt", out_tc, re.DOTALL)

    lines = ["⚙️ *Contrôle de Débit Intelligent (QoS / Traffic Control)*\n"]
    lines.append("Tout le trafic transite par le port unique de l\'application ZiVPN. Le débit est régulé au niveau du noyau selon le profil :\n")

    if m_10:
        rate_10, ceil_10, b_10, p_10 = m_10.groups()
        lines.append(f"⭐ *Profil VIP (premium-example) :*\n   • Plafond : `{rate_10}` (Ceil: `{ceil_10}`)\n   • Données lissées : *{format_bytes(int(b_10))}* ({int(p_10):,} paquets)")

    if m_20:
        rate_20, ceil_20, b_20, p_20 = m_20.groups()
        lines.append(f"\n🔹 *Profil Standard (standard-example / Public) :*\n   • Plafond : `{rate_20}` (Ceil: `{ceil_20}`)\n   • Données lissées : *{format_bytes(int(b_20))}* ({int(p_20):,} paquets)")

    lines.append("\n📱 *Adresses IP actives sur le profil VIP :*")
    if m_ips:
        for ip, t in m_ips:
            mins = int(t) // 60
            lines.append(f"   • `{ip}` (bail actif : {mins} min)")
    else:
        lines.append("   • _Aucune IP active dans la table VIP actuellement_")

    lines.append("\n🛠 *Commandes Rapides :*")
    lines.append("• `/limit vip <Mo_s>` : Régler le débit VIP (ex: `/limit vip 4`)")
    lines.append("• `/limit standard <Mo_s>` : Régler le débit Standard (ex: `/limit standard 0.5`)")
    return "\n".join(lines)

def build_accounts_list_text():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT c.id, c.email, c.password, c.enable, ct.up, ct.down, ct.inbound_id, ib.remark
        FROM clients c
        LEFT JOIN client_traffics ct ON c.email = ct.email
        LEFT JOIN inbounds ib ON ct.inbound_id = ib.id
        ORDER BY c.id ASC
    """)
    rows = c.fetchall()
    conn.close()

    lines = ["👤 *Liste des Comptes Enregistrés ZiVPN*\n"]
    if not rows:
        lines.append("Aucun compte dans la base de données.")
    else:
        for r in rows:
            status = "🟢" if r["enable"] == 1 else "🔴 (Bloqué)"
            if native_qos_enabled():
                ib_type = "⭐ Premium 4 Mo/s par compte (5667)" if r["inbound_id"] == 2 else "🔹 Standard 500 Ko/s par IP (5667)"
            else:
                ib_type = "⭐ VIP 4Mo/s (Port 5668)" if r["inbound_id"] == 2 else "🔹 Standard 500Ko/s (Port 5667)"
            conso = format_bytes((r["up"] or 0) + (r["down"] or 0))
            lines.append(f"• *{r['email']}* {status}\n   - Profil : `{ib_type}`\n   - Conso : `{conso}` | MDP : `{r['password']}`")

    lines.append("\n🛠 *Commandes Rapides Disponibles :*")
    lines.append("• `/add <nom> <mdp> [vitesse]` : Créer un compte")
    lines.append("• `/del <nom>` : Supprimer définitivement")
    lines.append("• `/block <nom>` / `/unblock <nom>` : Bloquer/Débloquer")
    lines.append("• `/limit <nom> <Mo_s>` : Changer vitesse")
    lines.append("• `/config <nom>` : Exporter fichier .ziv")
    return "\n".join(lines)

def build_conso_text():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT c.email, ct.up, ct.down, (ct.up + ct.down) as total_used, c.enable, ct.inbound_id
        FROM clients c
        JOIN client_traffics ct ON c.email = ct.email
        ORDER BY total_used DESC
        LIMIT 10
    """)
    rows = c.fetchall()
    c.execute("SELECT COUNT(*) FROM clients")
    total_clients = c.fetchone()[0]
    c.execute("SELECT value FROM zivpn_accounting_meta WHERE key='measurement_started_ms'")
    started = c.fetchone()
    c.execute("SELECT key,value FROM zivpn_accounting_meta WHERE key IN ('unattributed_up','unattributed_down')")
    unattributed = sum(int(row[1]) for row in c.fetchall())
    conn.close()

    from zivpn_rich import consumption_report
    return consumption_report(rows, total_clients, started[0] if started else None,
                              unattributed, format_bytes, native_qos_enabled())


def build_usage_text(email):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT c.email, c.password, c.enable, c.total_gb, c.expiry_time,
               ct.up, ct.down, ct.total, ct.enable as ct_enable, ct.last_online, ct.inbound_id,
               ib.remark as inbound_remark, ib.tag as inbound_tag
        FROM clients c
        JOIN client_traffics ct ON c.email = ct.email
        LEFT JOIN inbounds ib ON ct.inbound_id = ib.id
        WHERE c.email = ?
    """, (email,))
    row = c.fetchone()
    conn.close()

    if not row:
        return f"❗ Compte `{email}` introuvable dans la base 3X-UI."

    status = "🟢 Actif" if (row["enable"] == 1 and row["ct_enable"] == 1) else "🔴 Désactivé"
    down_str = format_bytes(row["down"] or 0)
    up_str = format_bytes(row["up"] or 0)
    total_used = format_bytes((row["up"] or 0) + (row["down"] or 0))

    quota_gb = row["total_gb"]
    quota_str = format_bytes(quota_gb) if quota_gb and quota_gb > 0 else "♾ Illimité"

    exp = row["expiry_time"]
    if exp and exp > 0:
        exp_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(exp / 1000))
    else:
        exp_str = "♾ Illimitée"

    last_online = row["last_online"]
    if last_online and last_online > 0:
        online_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_online / 1000))
    else:
        online_str = "Jamais connecté"

    pwd = row["password"] or ""
    masked_pwd = (pwd[:4] + "..." + pwd[-4:]) if len(pwd) > 8 else pwd

    is_ltd = (row["inbound_tag"] == "inbound-zivpn-limited" or row["inbound_id"] == 2)
    server_port = "5667 UDP (commun)" if native_qos_enabled() else ("5668 UDP (VIP)" if is_ltd else "5667 UDP (Standard)")
    speed_ceiling = "⭐ 4 Mo/s partagés par compte" if is_ltd else "🔹 500 Ko/s partagés par IP"

    return (
        f"🔍 *Fiche Client : `{email}`*\n\n"
        f"• *Statut :* {status}\n"
        f"• *Mot de passe :* `{masked_pwd}`\n"
        f"• *Serveur assigné :* `{row['inbound_remark'] or 'ZiVPN UDP Server'}`\n"
        f"• *Port d'écoute :* `{server_port}`\n"
        f"• *Contrôle de vitesse :* {speed_ceiling}\n"
        f"• *Téléchargement (Down) :* `{down_str}`\n"
        f"• *Téléversement (Up) :* `{up_str}`\n"
        f"• *Total Consommé :* `{total_used}`\n"
        f"• *Quota Total :* `{quota_str}`\n"
        f"• *Expiration :* `{exp_str}`\n"
        f"• *Dernière activité :* `{online_str}`\n\n"
        f"🛠 _Commandes : `/config {email}`, `/block {email}`, `/del {email}`_"
    )

def get_account_config_text(username):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT c.email, c.password, ct.inbound_id, ib.remark
        FROM clients c
        JOIN client_traffics ct ON c.email = ct.email
        LEFT JOIN inbounds ib ON ct.inbound_id = ib.id
        WHERE c.email = ?
    """, (username,))
    row = c.fetchone()
    conn.close()

    if not row:
        return f"❗ Compte `{username}` introuvable dans 3X-UI."

    inbound_id = row["inbound_id"]
    is_vip = inbound_id == 2
    speed_tag = "⭐ VIP (4 Mo/s / 32 Mbps)" if is_vip else "🔹 Standard (500 Ko/s / 4 Mbps)"

    return (
        f"📱 *Configuration Client ZiVPN : `{username}`*\n\n"
        f"Saisissez ces paramètres manuellement dans ZiVPN, sans importer de code Base64 :\n"
        f"1. Sélectionnez le protocole **UDP**\n"
        f"2. Serveur / IP : `{SERVER_PUBLIC_IP}`\n"
        f"3. Mot de passe : `{row['password']}`\n"
        f"4. Cliquez sur **Connecter**\n\n"
        f"• *Profil alloué :* {speed_tag}\n"
        f"• *Gestion du débit :* 100% automatique côté serveur"
    )

def build_help_text():
    text = (
        "🛠 *Centre d'Administration Bas-Niveau ZiVPN*\n\n"
        "📱 *Tableau de Bord & Télémétrie :*\n"
        "• `/menu` : Menu interactif complet avec boutons tactiles\n"
        "• `/status` : État matériel (CPU/RAM/Disque) et daemons\n"
        "• `/diagnostic` : Diagnostic progressif privé ; `/annuler` pour arrêter\n"
        "• `/sante [24|48]` : Historique privé de santé et erreurs (administrateur principal)\n"
        "• `/qualite` : Débits, plafonds et RTT sur 5 s (administrateur principal)\n"
        "• `/taches` : État des dernières tâches en privé\n"
        "• `/stats` : Statistiques globales des ports 5667 & 5668\n"
        "• `/users` : Tableau des sessions (ajoutez --texte pour le rendu classique)\n"
        "• `/live` ou `/conns` : Flux bruts conntrack (IPs, paquets, octets)\n"
        "• `/apps [compte]` : Services probables depuis les domaines observés\n"
        "• `/securite [compte]` : Rapport réseau privé (administrateur principal)\n"
        "• `/destinations [compte]` / `/dns [compte]` : Destinations et DNS observables\n"
        "• `/conso` : Classement des volumes consommés\n"
        "• `/usage <nom>` : Fiche détaillée d'un compte\n\n"
        "👤 *Gestion des Comptes (CRUD) :*\n"
        "• `/accounts` ou `/list` : Liste des comptes enregistrés\n"
        "• `/add <nom> <mdp> [vitesse]` : Créer un compte et ses paramètres Android\n"
        "• `/del <nom>` : Supprimer un compte et déconnecter ses sessions\n"
        "• `/block <nom>` / `/unblock <nom>` : Bloquer/Débloquer l'accès\n"
        "• `/config <nom>` : Afficher les paramètres à saisir dans Android\n\n"
        "🚦 *Contrôle du Débit (QoS) & Sécurité :*\n"
        "• `/qos` : État Traffic Control `ifb0`, files d'attente, baux ipset\n"
        "• `/limit <vip|standard|nom> <Mo_s>` : Régler le plafond de vitesse en direct\n"
        "• `/kick <ip|nom|all>` : Déconnecter immédiatement des sessions conntrack\n"
        "• `/p2p [on|off|status]` : Activer/Désactiver le filtrage BitTorrent\n\n"
        "💻 *Administration Système Serveur :*\n"
        "• `/exec <commande>` : Exécuter une commande shell root en direct\n"
        "• `/logs [zivpn|limited|xui|sync]` : Consulter les derniers logs système\n"
        "• `/restart` : Redémarrer tous les services VPN et 3X-UI\n"
        "• `/backup` : Télécharger une sauvegarde de la base de données\n"
        "• `/id` : Vos identifiants Telegram"
    )
    if native_qos_enabled():
        text = text.replace("Flux bruts conntrack (IPs, paquets, octets)", "Sessions QUIC confirmées et octets transférés")
        text = text.replace("État Traffic Control `ifb0`, files d'attente, baux ipset", "Premium : 4 Mo/s par compte ; Standard : 500 Ko/s par IP")
        text = text.replace("• `/limit <vip|standard|nom> <Mo_s>` : Régler le plafond de vitesse en direct\n", "")
        text = text.replace("sessions conntrack", "sessions QUIC")
    return text

def build_inbound_text():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, tag, up, down, total, remark, port, protocol FROM inbounds WHERE tag IN ('inbound-zivpn', 'inbound-zivpn-limited') ORDER BY id ASC")
    rows = c.fetchall()
    c.execute("SELECT COUNT(*) FROM clients")
    client_count = c.fetchone()[0]
    conn.close()

    if not rows:
        return "❗ Inbounds ZiVPN introuvables dans 3X-UI."

    lines = ["🌐 *Serveurs Inbound ZiVPN (3X-UI)*\n"]
    for row in rows:
        is_ltd = (row["tag"] == "inbound-zivpn-limited")
        listen_p = "5667 UDP (commun)" if native_qos_enabled() else ("5668 UDP (VIP)" if is_ltd else "5667 UDP (Standard)")
        ceiling = "⭐ Plafond 4 Mo/s (32 Mbps)" if is_ltd else "🔹 Plafond 500 Ko/s (4 Mbps)"
        lines.append(
            f"• *{row['remark']}*\n"
            f"   - Port écoute : `{listen_p}` (mappé 3X-UI `{row['port']}`)\n"
            f"   - Débit : {ceiling}\n"
            f"   - Trafic reçu : `{format_bytes(row['down'])}`\n"
            f"   - Statut : 🟢 Actif & Synchronisé\n"
        )
    lines.append(f"_Total des comptes administrés dans 3X-UI : {client_count}_")
    return "\n".join(lines)

def perform_services_restart():
    try:
        subprocess.run(["systemctl", "restart", "zivpn"], check=True, timeout=45)
        zivpn_ok = True
    except Exception:
        zivpn_ok = False

    try:
        if native_qos_enabled():
            zivpn_ltd_ok = zivpn_ok
        else:
            subprocess.run(["systemctl", "restart", "zivpn-limited"], check=True, timeout=45)
            zivpn_ltd_ok = True
    except Exception:
        zivpn_ltd_ok = False

    try:
        subprocess.run(["systemctl", "restart", "x-ui"], check=True, timeout=45)
        xui_ok = True
    except Exception:
        xui_ok = False

    z_text = "🟢 Redémarré (Port 5667 actif)" if zivpn_ok else "🔴 Échec"
    zl_text = "🟢 Redémarré (Port 5668 actif)" if zivpn_ltd_ok else "🔴 Échec"
    x_text = "🟢 Redémarré (Port 2053 actif)" if xui_ok else "🔴 Échec"

    return (
        "🔄 *Redémarrage des Services Effectué*\n\n"
        f"• *ZiVPN Standard (5667) :* {z_text}\n"
        f"• *ZiVPN VIP (5668 - 4 Mo/s) :* {zl_text}\n"
        f"• *3X-UI Web Panel :* {x_text}\n\n"
        "Tous les processus ont été rafraîchis."
    )

# ==========================================
# Telegram Bot Worker (Long Polling)
# ==========================================

def handle_telegram_command(chat_id, user_id, text, chat_type=None):
    parts = text.strip().split()
    if not parts:
        return
    cmd = parts[0].lower().replace("@zivpn_master_bot", "")
    args = parts[1:]
    force_text = cmd in ("/users", "/onlines", "/conso", "/top", "/securite", "/destinations", "/dns", "/apps") and bool(args) and args[-1] == "--texte"
    if force_text:
        args = args[:-1]

    if cmd in ("/exec", "/securite", "/destinations", "/dns", "/apps", "/sante", "/qualite") and not (
        user_id == PRIMARY_ADMIN_ID
        and chat_type == "private"
        and chat_id == PRIMARY_ADMIN_ID
    ):
        send_telegram(
            "⛔ Cette commande est réservée à l'administrateur principal en conversation privée.",
            chat_id=chat_id
        )
        return

    admin_ids = get_admin_ids()
    if user_id not in admin_ids:
        send_telegram(
            f"⛔ *Accès Non Autorisé*\nVotre ID Telegram est : `{user_id}`.\nVeuillez l'ajouter aux administrateurs pour interagir avec ce bot.",
            chat_id=chat_id
        )
        return

    if cmd in ("/sante", "/qualite"):
        try:
            if cmd == "/sante":
                hours = int(args[0]) if args else 48
                if hours not in (24, 48) or len(args) > 1:
                    raise ValueError('hours')
                report = HEALTH.report(hours)
            else:
                import zivpn_native_accounting as native
                report = quality_report(native)
            send_telegram(report, chat_id=chat_id)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            send_telegram("Mesure indisponible. Utilisation : /sante [24|48] ou /qualite.", chat_id=chat_id)
        return
    if cmd in ("/diagnostic", "/annuler"):
        if chat_type != "private" or chat_id != user_id:
            send_telegram("Le diagnostic s’utilise en conversation privée.", chat_id=chat_id)
        elif cmd == "/annuler":
            send_telegram("Arrêt demandé." if DIAGNOSTICS.cancel(chat_id, user_id) else "Aucun diagnostic en cours.", chat_id=chat_id)
        else:
            result = DIAGNOSTICS.start(chat_id, user_id, chat_type)
            if result:
                send_telegram(result, chat_id=chat_id)
        return
    if cmd in ("/securite", "/destinations", "/dns", "/apps"):
        from zivpn_security import build_security_text
        try:
            report = build_security_text(cmd[1:], args[0] if args else None)
        except (OSError, ValueError, KeyError, TypeError):
            report = "Observation réseau indisponible ; réessayez après la reconnexion des clients."
        send_telegram(report.as_plain() if force_text and hasattr(report, "as_plain") else report, chat_id=chat_id)
        return
    if cmd in ("/start", "/menu"):
        send_telegram(build_main_menu_text(), chat_id=chat_id, reply_markup=get_main_menu_keyboard())
    elif cmd == "/status":
        send_telegram(build_status_text(), chat_id=chat_id, reply_markup=get_back_keyboard("menu_status"))
    elif cmd == "/stats":
        send_telegram(build_stats_text(), chat_id=chat_id, reply_markup=get_back_keyboard("menu_stats"))
    elif cmd in ("/users", "/onlines"):
        report = build_users_text()
        send_telegram(report.as_plain() if force_text else report, chat_id=chat_id, reply_markup=get_back_keyboard("menu_users"))
    elif cmd in ("/live", "/conns"):
        send_telegram(build_live_connections_detailed_text(), chat_id=chat_id, reply_markup=get_back_keyboard("menu_live"))
    elif cmd == "/apps":
        send_telegram(build_apps_traffic_text(), chat_id=chat_id, reply_markup=get_back_keyboard("menu_apps"))
    elif cmd in ("/qos", "/limits"):
        send_telegram(build_qos_status_text(), chat_id=chat_id, reply_markup=get_qos_keyboard())
    elif cmd in ("/conso", "/top"):
        report = build_conso_text()
        send_telegram(report.as_plain() if force_text else report, chat_id=chat_id, reply_markup=get_back_keyboard("menu_conso"))
    elif cmd in ("/accounts", "/list"):
        send_telegram(build_accounts_list_text(), chat_id=chat_id, reply_markup=get_accounts_keyboard())
    elif cmd == "/p2p":
        action = args[0] if args else "status"
        _, txt = p2p_toggle(action)
        send_telegram(txt, chat_id=chat_id, reply_markup=get_p2p_keyboard())
    elif cmd == "/kick":
        if not args:
            send_telegram("⚠️ *Syntaxe :* `/kick <ip | nom_client | all>`\nExemple : `/kick 165.210.39.137`", chat_id=chat_id)
        else:
            cnt, ips = kick_client(args[0])
            ips_str = ", ".join(ips) if ips else args[0]
            send_telegram(f"⚡ *Session(s) déconnectée(s) :* `{cnt}` pour `{ips_str}`.", chat_id=chat_id)
    elif cmd == "/limit":
        if len(args) < 2:
            send_telegram("⚠️ *Syntaxe :* `/limit <vip|standard|nom> <vitesse_Mo_s>`\nExemple : `/limit vip 4` ou `/limit standard 0.5`", chat_id=chat_id)
        else:
            ok, msg = set_qos_limit(args[0], args[1])
            send_telegram(msg, chat_id=chat_id, reply_markup=get_qos_keyboard())
    elif cmd == "/add":
        if not args:
            send_telegram("⚠️ *Syntaxe :* `/add <nom> [mdp] [vitesse_Mo_s]`\nExemples :\n• `/add Jean monpass 4` (Compte VIP 4 Mo/s)\n• `/add Paul pass123` (Compte Standard 500 Ko/s)", chat_id=chat_id)
        else:
            username = args[0]
            password = args[1] if len(args) >= 2 else username
            speed = args[2] if len(args) >= 3 else None
            ziv = add_account(username, password, speed)
            send_telegram(
                f"✅ Compte `{username}` créé avec succès !\n\n" + get_account_config_text(username),
                chat_id=chat_id,
                reply_markup=get_accounts_keyboard()
            )
    elif cmd == "/del":
        if not args:
            send_telegram("⚠️ *Syntaxe :* `/del <nom>`\nExemple : `/del client-002`", chat_id=chat_id)
        else:
            ok, msg = delete_account(args[0])
            send_telegram(msg, chat_id=chat_id, reply_markup=get_accounts_keyboard())
    elif cmd == "/block":
        if not args:
            send_telegram("⚠️ *Syntaxe :* `/block <nom>`", chat_id=chat_id)
        else:
            ok, msg = toggle_account_block(args[0], block=True)
            send_telegram(msg, chat_id=chat_id, reply_markup=get_accounts_keyboard())
    elif cmd == "/unblock":
        if not args:
            send_telegram("⚠️ *Syntaxe :* `/unblock <nom>`", chat_id=chat_id)
        else:
            ok, msg = toggle_account_block(args[0], block=False)
            send_telegram(msg, chat_id=chat_id, reply_markup=get_accounts_keyboard())
    elif cmd == "/config":
        if not args:
            send_telegram("⚠️ *Syntaxe :* `/config <nom>`\nExemple : `/config premium-example`", chat_id=chat_id)
        else:
            send_telegram(get_account_config_text(args[0]), chat_id=chat_id)
    elif cmd == "/usage":
        if args:
            send_telegram(build_usage_text(args[0]), chat_id=chat_id, reply_markup=get_back_keyboard())
        else:
            send_telegram("🔍 *Utilisation de la commande :*\n`/usage <email>`\n\nExemple : `/usage premium-example`", chat_id=chat_id)
    elif cmd == "/exec":
        cmd_to_run = text[len(parts[0]):].strip()
        if not cmd_to_run:
            send_telegram("⚠️ *Syntaxe :* `/exec <commande bash>`\nExemple : `/exec uptime`", chat_id=chat_id)
        else:
            send_telegram("⏳ *Exécution de la commande...*", chat_id=chat_id)
            res = execute_admin_command(cmd_to_run)
            send_telegram(res, chat_id=chat_id)
    elif cmd == "/logs":
        svc = args[0] if args else "zivpn"
        send_telegram(get_service_logs(svc), chat_id=chat_id)
    elif cmd == "/inbound":
        send_telegram(build_inbound_text(), chat_id=chat_id, reply_markup=get_back_keyboard())
    elif cmd == "/restart":
        send_telegram("⏳ Redémarrage des services en cours...", chat_id=chat_id)
        res = perform_services_restart()
        send_telegram(res, chat_id=chat_id, reply_markup=get_back_keyboard())
    elif cmd == "/backup":
        send_telegram("📦 Préparation de la sauvegarde...", chat_id=chat_id)
        send_backup_file(chat_id)
    elif cmd == "/help":
        send_telegram(build_help_text(), chat_id=chat_id, reply_markup=get_back_keyboard())
    elif cmd == "/id":
        send_telegram(f"🆔 *Vos Identifiants :*\n• Telegram User ID : `{user_id}`\n• Chat ID : `{chat_id}`", chat_id=chat_id)
    else:
        send_telegram(
            f"❓ Commande inconnue : `{cmd}`\nTapez /help pour voir toutes les commandes ou ouvrez le menu :",
            chat_id=chat_id,
            reply_markup=get_main_menu_keyboard()
        )

def handle_telegram_callback(callback):
    callback_id = callback["id"]
    data = callback.get("data", "")
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    user_id = callback.get("from", {}).get("id")

    if data in ("menu_apps", "menu_health", "menu_quality") and not (user_id == PRIMARY_ADMIN_ID and chat_id == PRIMARY_ADMIN_ID and message.get("chat", {}).get("type") == "private"):
        answer_callback(callback_id, "Rapport réservé à l’administrateur principal en privé.")
        return
    admin_ids = get_admin_ids()
    if user_id not in admin_ids:
        answer_callback(callback_id, "Accès refusé.")
        return

    answer_callback(callback_id)

    if data in ("menu_health", "menu_quality"):
        handle_telegram_command(chat_id, user_id, "/sante" if data == "menu_health" else "/qualite", chat_type=message.get("chat", {}).get("type"))
    elif data == "menu_diagnostic":
        result = DIAGNOSTICS.start(chat_id, user_id, message.get("chat", {}).get("type"))
        if result:
            send_telegram(result, chat_id=chat_id)
    elif data.startswith("diagnostic_cancel:"):
        try:
            draft_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        DIAGNOSTICS.cancel(chat_id, user_id, draft_id)
    elif data == "menu_main":
        edit_telegram_message(build_main_menu_text(), chat_id, message_id, reply_markup=get_main_menu_keyboard())
    elif data == "menu_stats":
        edit_telegram_message(build_stats_text(), chat_id, message_id, reply_markup=get_back_keyboard("menu_stats"))
    elif data == "menu_users":
        edit_telegram_message(build_users_text(), chat_id, message_id, reply_markup=get_back_keyboard("menu_users"))
    elif data == "menu_live":
        edit_telegram_message(build_live_connections_detailed_text(), chat_id, message_id, reply_markup=get_back_keyboard("menu_live"))
    elif data == "menu_apps":
        from zivpn_security import build_security_text
        edit_telegram_message(build_security_text("apps"), chat_id, message_id, reply_markup=get_back_keyboard("menu_apps"))
    elif data == "menu_qos":
        edit_telegram_message(build_qos_status_text(), chat_id, message_id, reply_markup=get_qos_keyboard())
    elif data == "menu_accounts":
        edit_telegram_message(build_accounts_list_text(), chat_id, message_id, reply_markup=get_accounts_keyboard())
    elif data == "menu_p2p":
        _, txt = p2p_toggle("status")
        edit_telegram_message(txt, chat_id, message_id, reply_markup=get_p2p_keyboard())
    elif data == "p2p_on":
        _, txt = p2p_toggle("on")
        edit_telegram_message(txt, chat_id, message_id, reply_markup=get_p2p_keyboard())
    elif data == "p2p_off":
        _, txt = p2p_toggle("off")
        edit_telegram_message(txt, chat_id, message_id, reply_markup=get_p2p_keyboard())
    elif data == "qos_flush_ips" and native_qos_enabled():
        edit_telegram_message(build_qos_status_text(), chat_id, message_id, reply_markup=get_qos_keyboard())
    elif data == "qos_flush_ips":
        try:
            subprocess.run(["ipset", "flush", "zivpn_legacy_vip"], check=True, timeout=10)
            txt = "⚡ *Table des baux IP réinitialisée avec succès.*"
        except Exception as e:
            txt = f"❌ Erreur ipset: {e}"
        edit_telegram_message(txt + "\n\n" + build_qos_status_text(), chat_id, message_id, reply_markup=get_qos_keyboard())
    elif data == "menu_conso":
        edit_telegram_message(build_conso_text(), chat_id, message_id, reply_markup=get_back_keyboard("menu_conso"))
    elif data == "menu_status":
        edit_telegram_message(build_status_text(), chat_id, message_id, reply_markup=get_back_keyboard("menu_status"))
    elif data == "menu_restart":
        edit_telegram_message("⏳ Redémarrage des services ZiVPN & 3X-UI...", chat_id, message_id)
        res = perform_services_restart()
        edit_telegram_message(res, chat_id, message_id, reply_markup=get_back_keyboard())
    elif data == "menu_backup":
        send_backup_file(chat_id)
    elif data == "menu_close":
        edit_telegram_message("❌ *Menu fermé.*\nTapez /menu pour le rouvrir à tout moment.", chat_id, message_id)

MUTATION_LOCK = threading.Lock()


def dispatch_telegram_update(update):
    msg = update.get('message', {})
    words = msg.get('text', '').split()
    command = words[0].split('@')[0].lower() if words else ''
    callback = update.get('callback_query', {}).get('data', '')
    mutations = {'/add', '/del', '/block', '/unblock', '/kick', '/limit', '/exec', '/restart', '/p2p'}
    if command in mutations or callback in {'menu_restart', 'p2p_on', 'p2p_off', 'qos_flush_ips'}:
        with MUTATION_LOCK:
            return execute_telegram_update(update)
    return execute_telegram_update(update)


def execute_telegram_update(update):
    if "message" in update:
        msg = update["message"]
        text = msg.get("text", "")
        chat_id = msg.get("chat", {}).get("id")
        user_id = msg.get("from", {}).get("id")
        if text and chat_id and user_id:
            if text.split()[0].split('@')[0] == '/taches' and user_id in get_admin_ids():
                if msg.get('chat', {}).get('type') == 'private' and chat_id == user_id:
                    send_telegram(TASKS.status(chat_id), chat_id)
                return
            handle_telegram_command(chat_id, user_id, text, chat_type=msg.get("chat", {}).get("type"))
    elif "callback_query" in update:
        handle_telegram_callback(update["callback_query"])


def task_failure(chat_id, update_id):
    if chat_id in get_admin_ids():
        send_telegram(f"❌ Tâche {update_id} en erreur. Vérifiez son résultat avant de la relancer. Détails techniques dans les journaux du bot.", chat_id)


def telegram_bot_worker():
    global TASKS
    TASKS = Tasks('/var/lib/zivpn-telegram/tasks.db', dispatch_telegram_update, task_failure)
    print("[Telegram Bot Worker Started]")
    offset = 0
    failures = 0
    while True:
        try:
            result = TELEGRAM.call('getUpdates', {"offset": offset, "timeout": 25,
                "allowed_updates": json.dumps(["message", "callback_query", "stopped_message_generation"])})
            if not result.get('ok'):
                failures = min(failures+1, 5)
                time.sleep(min(2 ** failures, 30))
                continue
            failures = 0
            updates = result.get('result', [])
            # Stop events are idempotent and bypass a saturated work backlog.
            for update in updates:
                if 'stopped_message_generation' in update:
                    DIAGNOSTICS.stopped(update['stopped_message_generation'])
            for update in updates:
                if 'stopped_message_generation' in update:
                    pass
                else:
                    msg = update.get('message', {})
                    command = msg.get('text', '').split()
                    # Cancellation must remain responsive while a chat is busy.
                    if command and command[0].split('@')[0] in ('/annuler', '/taches'):
                        dispatch_telegram_update(update)
                    elif update.get('callback_query', {}).get('data', '').startswith('diagnostic_cancel:'):
                        dispatch_telegram_update(update)
                    else:
                        if not TASKS.submit(update):
                            time.sleep(0.2)
                            break
                # Advance only after acceptance. Persisted IDs prevent duplicate mutations.
                offset = update['update_id'] + 1
        except Exception as error:
            print(f"[Telegram Poll Error] {type(error).__name__}")
            time.sleep(3)

# ==========================================
# ZiVPN <-> 3X-UI Core Synchronization
# ==========================================

def sync_csv_to_db():
    """Detects any client added to clients.csv and adds to 3X-UI"""
    if not os.path.exists(CLIENTS_CSV):
        return
    try:
        csv_clients = []
        with open(CLIENTS_CSV, "r", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            for row in reader:
                if len(row) >= 2 and row[0].strip() and row[1].strip():
                    csv_clients.append((row[0].strip(), row[1].strip()))
    except Exception:
        return

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, settings FROM inbounds WHERE tag = 'inbound-zivpn'")
    ib_row = c.fetchone()
    if not ib_row:
        conn.close()
        return

    inbound_id = ib_row["id"]
    now_ms = int(time.time() * 1000)

    c.execute("SELECT email FROM clients")
    known_emails = {r[0] for r in c.fetchall()}

    new_found = False
    for uname, pwd in csv_clients:
        if uname not in known_emails:
            target_inbound = inbound_id

            c.execute("""
                INSERT INTO client_traffics (inbound_id, enable, email, up, down, expiry_time, total)
                VALUES (?, 1, ?, 0, 0, 0, 0)
                ON CONFLICT(email) DO UPDATE SET inbound_id=excluded.inbound_id
            """, (target_inbound, uname))

            c.execute("""
                INSERT INTO clients (email, password, enable, total_gb, expiry_time, created_at, updated_at)
                VALUES (?, ?, 1, 0, 0, ?, ?)
                ON CONFLICT(email) DO UPDATE SET password=excluded.password
            """, (uname, pwd, now_ms, now_ms))

            client_id = c.lastrowid
            c.execute("""
                INSERT OR IGNORE INTO client_inbounds (client_id, inbound_id, created_at)
                VALUES (?, ?, ?)
            """, (client_id, target_inbound, now_ms))
            known_emails.add(uname)
            new_found = True
            print(f"[Sync] Imported new client from CSV: {uname}")

    if new_found:
        conn.commit()
    conn.close()

def sync_accounts():
    """Synchronizes accounts between 3X-UI DB and both ZiVPN instances (standard & limited)"""
    # Native authentication reads 3X-UI on every login and rechecks active
    # identities periodically, without restarting other users' tunnels.
    if native_qos_enabled():
        return
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, tag FROM inbounds WHERE tag IN ('inbound-zivpn', 'inbound-zivpn-limited')")
    inbound_map = {row["tag"]: row["id"] for row in c.fetchall()}
    if not inbound_map:
        conn.close()
        return

    now_ms = int(time.time() * 1000)

    def sync_one(inbound_id, config_file, service_name, label):
        if inbound_id == "all":
            c.execute(ACCOUNT_QUERY + " WHERE ib.tag IN (?, ?)",
                      ("inbound-zivpn", "inbound-zivpn-limited"))
        else:
            c.execute(ACCOUNT_QUERY + " WHERE ct.inbound_id = ?", (inbound_id,))
        db_clients = c.fetchall()

        active_passwords = set()

        for client in db_clients:
            if access_denial(client, now_ms) is None and client["password"]:
                active_passwords.add(client["password"])

        if not os.path.exists(config_file):
            return

        cfg = read_json_config(config_file)
        current_passwords = set(cfg.get("auth", {}).get("config", []))

        if active_passwords != current_passwords:
            added = active_passwords - current_passwords
            removed = current_passwords - active_passwords

            if "auth" not in cfg:
                cfg["auth"] = {"mode": "passwords"}
            cfg["auth"]["config"] = sorted(list(active_passwords))
            write_json_config(config_file, cfg)

            subprocess.run(["systemctl", "reload-or-restart", service_name], capture_output=True, timeout=10)
            print(f"[Sync {label}] Updated passwords: +{len(added)}, -{len(removed)}")

            if added:
                msg = f"🟢 *3X-UI ➔ ZiVPN Sync ({label})*\nComptes activés/ajoutés : {len(added)}\nTotal actifs : {len(active_passwords)}"
                send_telegram(msg)
            if removed:
                msg = f"🔴 *3X-UI ➔ ZiVPN Sync ({label})*\nComptes désactivés/supprimés : {len(removed)}\nTotal actifs : {len(active_passwords)}"
                send_telegram(msg)

    # Sync standard inbound (5667) with all active accounts
    sync_one("all", CONFIG_FILE, "zivpn", "Standard 5667")

    # Sync VIP limited inbound (5668)
    if "inbound-zivpn-limited" in inbound_map:
        sync_one(inbound_map["inbound-zivpn-limited"], CONFIG_LIMITED_FILE, "zivpn-limited", "VIP 5668 (4 Mo/s)")

    conn.close()

_TRAFFIC_ROLLUP = dict(up=0, down=0, attributed_up=0, attributed_down=0)
_TRAFFIC_LOG_AT = 0

def sync_traffic():
    """Apply persistent per-flow differences and authenticated account bindings."""
    global _TRAFFIC_LOG_AT
    try:
        result = zivpn_accounting.sync_traffic(DB_PATH)
        for key in _TRAFFIC_ROLLUP:
            _TRAFFIC_ROLLUP[key] += result[key]
        now = time.monotonic()
        if now - _TRAFFIC_LOG_AT >= 600:
            print('[Traffic rollup] ' + ' '.join(f'{key}={value}' for key,value in _TRAFFIC_ROLLUP.items()), flush=True)
            for key in _TRAFFIC_ROLLUP:
                _TRAFFIC_ROLLUP[key] = 0
            _TRAFFIC_LOG_AT = now
    except Exception as e:
        print(f"[Traffic Sync Error] {type(e).__name__}")


def diagnostic_api_call(method, payload):
    try:
        return TELEGRAM.call(method, payload)
    except Exception as error:
        print(f"[Diagnostic Telegram Error] {type(error).__name__}")
        return None


def diagnostic_services(cancel):
    result = subprocess.run(['systemctl', 'is-active', 'zivpn', 'zivpn-xui-sync', 'x-ui'],
                            capture_output=True, text=True, timeout=5)
    return '\n'.join(f'{name} : {state}' for name, state in
                     zip(('VPN', 'Bot Telegram', '3X-UI'), result.stdout.splitlines()))


def diagnostic_resources(cancel):
    disk = shutil.disk_usage('/')
    return f"Charge système (1/5/15 min) : {os.getloadavg()}\nDisque utilisé : {disk.used/disk.total*100:.1f}%"


def diagnostic_sessions(cancel):
    import zivpn_native_accounting as native
    data = native.snapshot()
    rows = data['sessions'].values()
    counts = defaultdict(int)
    ips = set()
    for row in rows:
        counts[row['email']] += 1
        ips.add(row['ip'])
    return f"Tunnels authentifiés : {sum(counts.values())} ; IP publiques : {len(ips)}.\n" + '\n'.join(f'{name} : {count} tunnels' for name, count in sorted(counts.items())[:20])


def diagnostic_network(cancel):
    from pathlib import Path
    keys = ('rx_bytes', 'tx_bytes', 'rx_errors', 'tx_errors', 'rx_dropped', 'tx_dropped')
    def counters():
        return {key: int(Path(f'/sys/class/net/{NETWORK_INTERFACE}/statistics/'+key).read_text()) for key in keys}
    first = counters()
    started = time.monotonic()
    if cancel.wait(5):
        return 'Mesure réseau interrompue.'
    second = counters()
    elapsed = time.monotonic()-started
    return f"Moyenne sur {elapsed:.1f} s : réception {(second['rx_bytes']-first['rx_bytes'])/elapsed/1e6:.3f} Mo/s ; émission {(second['tx_bytes']-first['tx_bytes'])/elapsed/1e6:.3f} Mo/s.\n" + '\n'.join(f'{key} : +{second[key]-first[key]}' for key in keys[2:]) + '\nCes débits incluent les deux côtés du relais VPN et les autres services.'


from zivpn_progress import Diagnostics
DIAGNOSTICS = Diagnostics(diagnostic_api_call, send_telegram,
                          [('Services', diagnostic_services), ('Ressources', diagnostic_resources),
                           ('Sessions VPN', diagnostic_sessions), ('Mesure réseau', diagnostic_network)],
                          lambda user_id: user_id in get_admin_ids())


def main():
    print("[ZiVPN Master <-> 3X-UI Sync & Low-Level Admin Telegram Daemon Started]")

    # Start Telegram bot receiver thread
    t = threading.Thread(target=telegram_bot_worker, daemon=True)
    t.start()

    last_traffic_sync = 0
    while True:
        try:
            if not t.is_alive():
                print('[Telegram Receiver Recovery] restarting receiver', flush=True)
                t = threading.Thread(target=telegram_bot_worker, daemon=True)
                t.start()
            sync_csv_to_db()
            sync_accounts()
            now = time.time()
            if now - last_traffic_sync >= 10:
                sync_traffic()
                last_traffic_sync = now
            import zivpn_native_accounting as native
            notices = HEALTH.collect(native, TELEGRAM)
            if notices:
                from zivpn_health import PlainText
                send_telegram(PlainText('Alerte de santé — serveur\n' + '\n'.join(notices[:10]) + '\n/sante pour le bilan.'), chat_id=PRIMARY_ADMIN_ID)
        except Exception as e:
            print(f"[Main Loop Error] {type(e).__name__}")
        time.sleep(5)

if __name__ == "__main__":
    main()
