# Docker deployment: native server

This image packages the native UDP server and Python authentication/accounting helpers. It does not install 3X-UI, run systemd or start the Telegram bot. Use the full [host deployment](DEPLOYMENT.md) to provision those components first, or supply an equivalent compatible database and a continuously running accounting collector.

## Build and validate

Requirements: Linux, Docker Engine with BuildKit, Docker Compose v2, and Python 3.10+ for the smoke-test driver.

```bash
docker build -t zivpn-stack:local .
python3 tools/docker_smoke.py zivpn-stack:local
```

The smoke check creates temporary fictitious accounts and a short-lived TLS certificate, starts the actual image without publishing ports, checks the policy helper's acceptance and rejection paths, probes the private socket, then verifies the final accounting sample after shutdown. It removes its container and temporary data. This is not an Android end-to-end connection test.

The image uses the project's pinned Go 1.21.13 toolchain. This legacy dependency remains a known limitation. CI validates the Linux amd64 image; ARM64 runtime deployment still needs independent validation.

## Prepare private inputs

Keep these directories outside the checkout. The Compose file requires explicit configuration and database paths and refuses to create missing bind sources.

- `ZIVPN_CONFIG_DIR`: a private directory containing **only** native `config.json`, `zivpn.crt` and `zivpn.key`. Never put the Telegram token or panel credentials in this directory.
- `ZIVPN_DB_DIR`: the existing 3X-UI database directory containing `x-ui.db`. Mount the directory, not only the database file: SQLite must see its WAL/SHM and accounting lock files. The mount is writable for first-login expiry activation and accounting.
- `ZIVPN_RUN_DIR`: a root-owned directory for the private statistics socket, default `/run/zivpn-container`. It must be recreated after a host reboot before Compose starts.

Example native configuration, to save privately as `config.json`:

```json
{
  "listen": ":5667",
  "cert": "/etc/zivpn/zivpn.crt",
  "key": "/etc/zivpn/zivpn.key",
  "congestionControl": "bbr",
  "quic": {"disablePathMTUDiscovery": true}
}
```

Supply your certificate and private key separately; no certificate or account is generated automatically in production. Restrict private directories to root (`0700`) and key/config/database files to `0600`. The compatibility mode of the existing installer uses a self-signed certificate; client configuration must match the certificate policy you choose.

Set the public directory paths in your shell, for example:

```bash
export ZIVPN_CONFIG_DIR=/srv/zivpn-native/config
export ZIVPN_DB_DIR=/etc/x-ui
export ZIVPN_RUN_DIR=/run/zivpn-container
sudo install -d -m 700 "$ZIVPN_RUN_DIR"
docker compose config --quiet
```

Do not run a second native server on UDP 5667/5668. Compose uses Linux host networking so the limiter sees the original client IP; no privileged mode, extra Linux capabilities, public management port or host Docker socket is required. The container runs as root to access root-owned database and key files, with capabilities dropped, a read-only root filesystem and no new privileges.

## Connect host accounting and Telegram

The host collector must read the container's socket. Install the updated Python modules using your normal reviewed update procedure, retain `/etc/zivpn/native-qos.enabled`, and add this override to `zivpn-xui-sync.service`:

```ini
[Service]
Environment=ZIVPN_NATIVE_SOCKET=/run/zivpn-container/zivpn-native.sock
```

The value must match `ZIVPN_RUN_DIR`. Inside the container, the socket remains `/run/zivpn-native.sock`; outside it, the shared directory exposes the same socket at the configured host path. Do not change socket permissions from `0600` or expose it via TCP.

Migration affects active tunnels. Take a consistent SQLite backup, flush the host server's last traffic sample, stop the host `zivpn` service and start Compose during a planned maintenance window. Reload systemd and restart only the collector after saving the override. Verify its accounting against a controlled client before admitting users. The existing UDP alias service and cloud firewall remain host responsibilities.

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 native
docker compose exec native python3 /usr/local/lib/zivpn_container_healthcheck.py
```

Keep the host collector running: without periodic samples, byte quotas can lag until shutdown. Telegram commands that explicitly control the host `zivpn.service` do not control this container; operate the container with Compose. The panel and bot are not claimed to be fully container-compatible.

## Stop and roll back

```bash
docker compose stop native
```

The entrypoint takes a final traffic sample before forwarding SIGTERM to the native server. Compose allows 45 seconds; `docker kill`, host crashes and forced termination cannot guarantee that final sample. Inspect logs for accounting failures. Never restore a database while the panel or collector is writing to it.

To return to the host service, stop Compose first, remove the collector's socket override, reload systemd, and start the host native server and collector following the existing upgrade procedure. Keep only one server per listening port and one polling bot per Telegram token.
