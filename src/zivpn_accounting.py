"""Persistent UDP tunnel accounting using authenticated source sockets.

Counters include UDP/IP overhead. Unknown or migrated sockets are never
assigned to an account by guessing from its public IP.
"""
import hashlib
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import uuid

PORT_TAGS = {5667: "inbound-zivpn", 5668: "inbound-zivpn-limited"}


def native_enabled():
    return Path('/etc/zivpn/native-qos.enabled').exists()


def ensure_schema(conn):
    for statement in (
        """CREATE TABLE IF NOT EXISTS zivpn_sessions (
            epoch TEXT NOT NULL, ip TEXT NOT NULL, port INTEGER NOT NULL,
            server_port INTEGER NOT NULL, email TEXT NOT NULL,
            session_id TEXT NOT NULL, authenticated_ms INTEGER NOT NULL,
            PRIMARY KEY (epoch, ip, port, server_port))""",
        """CREATE TABLE IF NOT EXISTS zivpn_traffic_checkpoints (
            flow_key TEXT PRIMARY KEY, up_bytes INTEGER NOT NULL,
            down_bytes INTEGER NOT NULL, session_id TEXT,
            last_seen_ms INTEGER NOT NULL, ip TEXT, source_port INTEGER,
            server_port INTEGER)""",
        """CREATE TABLE IF NOT EXISTS zivpn_accounting_meta (
            key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    ):
        conn.execute(statement)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(zivpn_traffic_checkpoints)")}
    for name, kind in (("ip", "TEXT"), ("source_port", "INTEGER"), ("server_port", "INTEGER")):
        if name not in existing:
            conn.execute(f"ALTER TABLE zivpn_traffic_checkpoints ADD COLUMN {name} {kind}")


def process_epoch(pid):
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    # The comm field can contain spaces and parentheses; split after its end.
    tail = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return f"{boot}:{pid}:{tail[19]}"


def server_epochs():
    epochs = {}
    for port, service in ((5667, "zivpn"), (5668, "zivpn-limited")):
        result = subprocess.run(
            ["systemctl", "show", service, "-p", "MainPID", "--value"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        pid = int(result.stdout.strip())
        if pid > 0:
            epochs[port] = process_epoch(pid)
    return epochs


def local_addresses():
    result = subprocess.run(["ip", "-j", "address", "show"], check=True,
                            capture_output=True, text=True, timeout=5)
    return {str(ipaddress.ip_address(addr["local"]))
            for interface in json.loads(result.stdout)
            for addr in interface.get("addr_info", [])}


def socket_address(address):
    if address.startswith("["):
        host, suffix = address[1:].split("]", 1)
        port = suffix.removeprefix(":")
    else:
        host, port = address.rsplit(":", 1)
    ip = str(ipaddress.ip_address(host))
    number = int(port)
    if not 0 <= number <= 65535:
        raise ValueError("Invalid source port")
    return ip, number


def parse_flows(output, addresses, boot_id):
    flows = []
    for line in output.splitlines():
        if not re.search(r"\budp\s+17\b", line):
            continue
        fields = {}
        for key, value in re.findall(r"\b(src|dst|sport|dport|bytes|packets|id|zone)=([^\s]+)", line):
            fields.setdefault(key, []).append(value)
        if not all(len(fields.get(k, [])) == 2 for k in ("src", "dst", "sport", "dport")):
            continue
        try:
            destination = str(ipaddress.ip_address(fields["dst"][0]))
            server_port = int(fields["sport"][1])
        except ValueError:
            continue
        if destination not in addresses or server_port not in PORT_TAGS:
            continue
        # Missing counters or IDs must stop the sample, not silently lose bytes.
        if len(fields.get("bytes", [])) != 2 or len(fields.get("id", [])) != 1:
            raise ValueError("Conntrack accounting or flow IDs unavailable")
        identity = [boot_id, fields.get("zone", ["0"])[0], fields["id"][0]]
        identity += [fields[k][i] for i in (0, 1) for k in ("src", "dst", "sport", "dport")]
        up, down = (int(n) for n in fields["bytes"])
        if up < 0 or down < 0:
            raise ValueError("Negative counter")
        flows.append({"key": hashlib.sha256(json.dumps(identity).encode()).hexdigest(),
                      "ip": str(ipaddress.ip_address(fields["src"][0])),
                      "port": int(fields["sport"][0]), "server_port": server_port,
                      "up": up, "down": down,
                      "packets": sum(int(n) for n in fields.get("packets", []))})
    return flows


def read_flows(ip=None, port=None):
    families = ["ipv4", "ipv6"] if ip is None else ["ipv4" if ipaddress.ip_address(ip).version == 4 else "ipv6"]
    output = []
    for family in families:
        command = ["conntrack", "-L", "-f", family, "-p", "udp", "-o", "extended,id"]
        if ip is not None:
            command += ["--orig-src", ip, "--sport", str(port)]
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=5)
        output.append(result.stdout)
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return parse_flows("\n".join(output), local_addresses(), boot)


def apply_flow(conn, flow, epoch, now_ms, baseline=False):
    previous = conn.execute(
        "SELECT up_bytes,down_bytes,session_id FROM zivpn_traffic_checkpoints WHERE flow_key=?",
        (flow["key"],),
    ).fetchone()
    session = conn.execute(
        "SELECT email,session_id FROM zivpn_sessions "
        "WHERE epoch=? AND ip=? AND port=? AND server_port=?",
        (epoch, flow["ip"], flow["port"], flow["server_port"]),
    ).fetchone() if epoch else None
    session_id = session[1] if session else None
    up = down = 0
    if not baseline:
        for direction, index in (("up", 0), ("down", 1)):
            value = flow[direction]
            old = previous[index] if previous else 0
            delta = value - old if value >= old else value
            if direction == "up":
                up = delta
            else:
                down = delta
    credited = False
    if up or down:
        conn.execute("UPDATE inbounds SET up=COALESCE(up,0)+?, down=COALESCE(down,0)+? WHERE tag=?",
                     (up, down, PORT_TAGS[flow["server_port"]]))
        # Only continue a known session or count a genuinely new flow.
        if session and (previous is None or previous[2] == session_id):
            updated = conn.execute(
                "UPDATE client_traffics SET up=COALESCE(up,0)+?, down=COALESCE(down,0)+?, "
                "last_online=MAX(COALESCE(last_online,0),?) WHERE email=?",
                (up, down, now_ms, session[0]),
            )
            credited = updated.rowcount == 1
        if not credited:
            for key, value in (("unattributed_up", up), ("unattributed_down", down)):
                conn.execute(
                    "INSERT INTO zivpn_accounting_meta(key,value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+CAST(excluded.value AS INTEGER)",
                    (key, str(value)),
                )
    conn.execute(
        "INSERT INTO zivpn_traffic_checkpoints "
        "(flow_key,up_bytes,down_bytes,session_id,last_seen_ms,ip,source_port,server_port) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(flow_key) DO UPDATE SET up_bytes=excluded.up_bytes, "
        "down_bytes=excluded.down_bytes, session_id=excluded.session_id, last_seen_ms=excluded.last_seen_ms, "
        "ip=excluded.ip,source_port=excluded.source_port,server_port=excluded.server_port",
        (flow["key"], flow["up"], flow["down"], session_id, now_ms,
         flow["ip"], flow["port"], flow["server_port"]),
    )
    return up, down, credited


def record_session(conn, address, server_port, email, now_ms):
    """Record identity only: no conntrack, systemctl or network calls here."""
    ip, port = socket_address(address)
    epoch = process_epoch(os.getppid())
    old = conn.execute("SELECT email,session_id FROM zivpn_sessions "
                       "WHERE epoch=? AND ip=? AND port=? AND server_port=?",
                       (epoch, ip, port, server_port)).fetchone()
    session_id = old[1] if old and old[0] == email else uuid.uuid4().hex
    conn.execute(
        "INSERT INTO zivpn_sessions VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(epoch,ip,port,server_port) DO UPDATE SET email=excluded.email, "
        "session_id=excluded.session_id, authenticated_ms=excluded.authenticated_ms",
        (epoch, ip, port, server_port, email, session_id, now_ms),
    )
    if not old or old[0] == email:
        # Reuse the last persisted baseline. Never fetch kernel state while
        # authorizing a login; a changed account leaves one interval unassigned.
        conn.execute("UPDATE zivpn_traffic_checkpoints SET session_id=? "
                     "WHERE ip=? AND source_port=? AND server_port=? "
                     "AND (session_id IS NULL OR session_id=?)",
                     (session_id, ip, port, server_port, session_id))



def account_snapshot(conn, flows, epochs, now_ms, baseline=False):
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    total_up = total_down = attributed_up = attributed_down = 0
    for flow in flows:
        up, down, credited = apply_flow(conn, flow, epochs.get(flow["server_port"]), now_ms, baseline)
        total_up += up
        total_down += down
        if credited:
            attributed_up += up
            attributed_down += down
    # Prune only after a complete, successful snapshot.
    conn.execute("DELETE FROM zivpn_traffic_checkpoints WHERE last_seen_ms < ?", (now_ms,))
    if epochs:
        placeholders = ",".join("?" for _ in epochs)
        conn.execute(f"DELETE FROM zivpn_sessions WHERE epoch NOT IN ({placeholders})", tuple(epochs.values()))
    previous_sample = conn.execute("SELECT value FROM zivpn_accounting_meta WHERE key='last_sample_ms'").fetchone()
    interval_ms = max(1, now_ms - int(previous_sample[0])) if previous_sample else 0
    for key, value in (("last_delta_up", total_up), ("last_delta_down", total_down),
                       ("last_interval_ms", interval_ms)):
        conn.execute("INSERT INTO zivpn_accounting_meta VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    conn.execute(
        "INSERT INTO zivpn_accounting_meta VALUES ('last_sample_ms',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now_ms),),
    )
    return {"up": total_up, "down": total_down,
            "attributed_up": attributed_up, "attributed_down": attributed_down}


def sync_traffic(db_path="/etc/x-ui/x-ui.db"):
    if native_enabled():
        import zivpn_native_accounting
        return zivpn_native_accounting.sync_traffic(db_path)
    uri = Path(db_path).resolve().as_uri() + "?mode=rw"
    # Serialize collectors without holding the panel database during kernel reads.
    with open(db_path + ".zivpn-accounting.lock", "a+") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        flows = read_flows()
        epochs = server_epochs()
        now_ms = int(time.time() * 1000)
        conn = sqlite3.connect(uri, uri=True, timeout=20)
        try:
            with conn:
                return account_snapshot(conn, flows, epochs, now_ms)
        finally:
            conn.close()



def live_connections(db_path="/etc/x-ui/x-ui.db"):
    if native_enabled():
        import zivpn_native_accounting
        return zivpn_native_accounting.live_connections()
    flows, epochs = read_flows(), server_epochs()
    connections = {}
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        for flow in flows:
            epoch = epochs.get(flow["server_port"])
            session = conn.execute(
                "SELECT s.email,ct.inbound_id FROM zivpn_sessions s "
                "JOIN clients c ON s.email=c.email "
                "JOIN client_traffics ct ON s.email=ct.email "
                "WHERE s.epoch=? AND s.ip=? AND s.port=? AND s.server_port=?",
                (epoch, flow["ip"], flow["port"], flow["server_port"]),
            ).fetchone()
            key = f"{flow['ip']}:{flow['port']}:{flow['server_port']}"
            if key not in connections:
                connections[key] = {"ip": flow["ip"], "remote_port": flow["port"],
                                    "port": flow["server_port"], "bytes": 0, "packets": 0,
                                    "label": str(flow["server_port"]),
                                    "email": session[0] if session else None,
                                    "vip": bool(session and session[1] == 2)}
            connections[key]["bytes"] += flow["up"] + flow["down"]
            connections[key]["packets"] += flow.get("packets", 0)
    finally:
        conn.close()
    return connections
