"""Account access checks shared by ZiVPN authentication and synchronization."""
import ipaddress
from pathlib import Path
import sqlite3
from zivpn_sqlite import ClosingConnection
import subprocess
import time

from zivpn_accounting import record_session

DB_PATH = "/etc/x-ui/x-ui.db"
ZIVPN_TAGS = ("inbound-zivpn", "inbound-zivpn-limited")

ACCOUNT_QUERY = """
    SELECT c.id, c.email, c.password, c.enable AS client_enable,
           c.total_gb AS client_quota, c.expiry_time AS client_expiry,
           ct.up, ct.down, ct.total AS traffic_quota,
           ct.enable AS traffic_enable, ct.expiry_time AS traffic_expiry,
           ct.inbound_id, ib.tag AS inbound_tag,
           ib.enable AS inbound_enable, ib.expiry_time AS inbound_expiry
    FROM clients c
    JOIN client_traffics ct ON c.email = ct.email
    JOIN inbounds ib ON ct.inbound_id = ib.id
"""


def access_denial(account, now_ms):
    """Return a refusal reason, or None. Quotas are bytes, dates milliseconds."""
    if account["inbound_tag"] not in ZIVPN_TAGS:
        return "unassigned"
    for field in ("client_enable", "traffic_enable", "inbound_enable"):
        if account[field] != 1:
            return "disabled"
    for field in ("client_expiry", "traffic_expiry", "inbound_expiry"):
        expiry = int(account[field] or 0)
        # Negative client/traffic dates start at the first successful login.
        if expiry > 0 and expiry <= now_ms:
            return "expired"
    used = max(0, int(account["up"] or 0)) + max(0, int(account["down"] or 0))
    for field in ("client_quota", "traffic_quota"):
        quota = int(account[field] or 0)
        if quota > 0 and used >= quota:
            return "quota"
    return None


def client_ip(address):
    """Extract an IPv4/IPv6 address from the client's host:port."""
    try:
        if address.startswith("["):
            host = address[1:address.index("]")]
        else:
            try:
                return str(ipaddress.ip_address(address))
            except ValueError:
                host, port = address.rsplit(":", 1)
                if not port.isdigit():
                    return None
        return str(ipaddress.ip_address(host))
    except (ValueError, IndexError):
        return None


def authenticate(address, password, required_tag=None, db_path=DB_PATH, now_ms=None, server_port=None, manage_ipset=True, diagnostic=False):
    """Authorize against current SQLite state, failing closed on errors."""
    def refused(reason):
        return {"ok": False, "reason": reason} if diagnostic else None

    if not password:
        return refused("missing_credentials")
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    try:
        database_uri = Path(db_path).resolve().as_uri() + "?mode=rw"
        with sqlite3.connect(database_uri, uri=True, timeout=5.0, factory=ClosingConnection) as conn:
            conn.row_factory = sqlite3.Row
            accounts = conn.execute(ACCOUNT_QUERY + " WHERE c.password = ?", (password,)).fetchall()
            # A shared password cannot identify an individual account reliably.
            if len(accounts) != 1:
                return refused("invalid_credentials" if not accounts else "ambiguous_credentials")
            account = accounts[0]
            if required_tag and account["inbound_tag"] != required_tag:
                return refused("wrong_profile")
            denial = access_denial(account, now_ms)
            if denial:
                return refused(denial)
            if int(account["client_expiry"] or 0) < 0 or int(account["traffic_expiry"] or 0) < 0:
                # Serialize first-login activation and recheck after the lock.
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(ACCOUNT_QUERY + " WHERE c.password = ?", (password,)).fetchall()
                if len(rows) != 1:
                    return refused("invalid_credentials" if not rows else "ambiguous_credentials")
                account = rows[0]
                if required_tag and account["inbound_tag"] != required_tag:
                    return refused("wrong_profile")
                denial = access_denial(account, now_ms)
                if denial:
                    return refused(denial)
                conn.execute(
                    "UPDATE clients SET expiry_time = ? - expiry_time, updated_at = ? "
                    "WHERE id = ? AND expiry_time < 0",
                    (now_ms, now_ms, account["id"]),
                )
                conn.execute(
                    "UPDATE client_traffics SET expiry_time = ? - expiry_time "
                    "WHERE email = ? AND expiry_time < 0",
                    (now_ms, account["email"]),
                )
            email = account["email"]
            is_vip = account["inbound_tag"] == "inbound-zivpn-limited"
            if server_port is not None:
                # Access checks remain mandatory; identity telemetry is best effort.
                conn.execute("SAVEPOINT zivpn_identity")
                try:
                    record_session(conn, address, server_port, email, now_ms)
                except (sqlite3.Error, OSError, ValueError, subprocess.SubprocessError) as error:
                    conn.execute("ROLLBACK TO zivpn_identity")
                    print(f"[ZiVPN Identity Warning] {type(error).__name__}", file=__import__("sys").stderr)
                finally:
                    conn.execute("RELEASE zivpn_identity")
    except sqlite3.Error:
        return refused("database_error")
    except (OSError, ValueError, TypeError, OverflowError, subprocess.SubprocessError):
        return refused("internal_error")

    ip = client_ip(address)
    if manage_ipset and ip and ipaddress.ip_address(ip).version == 4:
        command = ["ipset", "add", "zivpn_legacy_vip", ip, "timeout", "1728000", "-exist"] if is_vip else ["ipset", "del", "zivpn_legacy_vip", ip]
        try:
            subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {"ok": True, "email": email, "reason": "accepted"} if diagnostic else email


def main(argv, required_tag=None, server_port=5667):
    if len(argv) not in (3, 4):
        return 1
    email = authenticate(argv[1], argv[2], required_tag=required_tag, server_port=server_port)
    if email is None:
        return 1
    print(email)
    return 0
