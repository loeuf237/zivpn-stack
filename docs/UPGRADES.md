# Host installation, update and legacy migration

The host installer detects the existing deployment and selects a supported action instead of refusing every previous installation. Preview the action before applying it:

```bash
sudo python3 tools/install.py --check
# Equivalent standalone read-only detection:
sudo python3 tools/preflight.py
```

Checks require root and a running systemd manager on Ubuntu/Debian. At least one firewall inspection tool (`iptables-save` or `nft`) must be available. When Docker is installed, the local daemon inventory must also be accessible. Configuration and authentication contents are not printed in the report.

## Existing ZiVPN Stack

A supported stack has a compatible 3X-UI database, native configuration, per-server settings and Telegram token. The installer checks database integrity and required profile tables and verifies the existing TLS pair.

```bash
sudo python3 tools/install.py --check
sudo python3 tools/install.py
```

An update keeps:

- VPN accounts, passwords, enable flags, quotas, expiry dates and traffic counters;
- Standard/Premium defaults and per-account bandwidth overrides;
- TLS certificate/key and native listen/congestion settings;
- Telegram token, administrator identity and deployment settings;
- existing 3X-UI binary, panel credentials, panel service and network service configuration;
- systemd drop-ins and the previous active/enabled state of the panel, native server and collector. Known obsolete compatibility/forwarding services are retired after successful validation.

Passing new setup files does not replace an existing stack's settings or credentials. Credential rotation is a separate administrative operation.

## Migrate zahidbd2/udp-zivpn

The supported legacy layout uses `/etc/zivpn/config.json` with `auth.mode = "passwords"`, `listen = ":5667"` and the standard ZiVPN obfuscation, normally launched by `zivpn.service`. The AMD64 and ARM64 installer variants share this layout. The [upstream installer](https://github.com/zahidbd2/udp-zivpn/blob/main/zi.sh) creates a legacy NAT redirection; its [uninstaller](https://github.com/zahidbd2/udp-zivpn/blob/main/uninstall.sh) does not remove that rule. This installer does **not** invoke the uninstaller.

Prepare new panel/bot configuration privately as described in [DEPLOYMENT.md](DEPLOYMENT.md), then run:

```bash
sudo python3 tools/install.py --settings /root/zivpn-config/settings.json \
  --secrets /root/zivpn-config/secrets.json --check
sudo python3 tools/install.py --settings /root/zivpn-config/settings.json \
  --secrets /root/zivpn-config/secrets.json --legacy-profile standard
```

Each unique legacy password becomes one enabled account named `legacy-0001`, `legacy-0002`, etc. Duplicate password entries are deduplicated; the passwords themselves remain unchanged so existing client credentials remain usable. The original script stores no per-account quotas or traffic history, so imported accounts start with no expiry or quota and zero counters. No historical usage is invented.

Standard defaults to 1,000,000 bytes/s per public IP; `--legacy-profile premium` instead selects the 4,000,000 bytes/s per-account default. These are new stack policies, not a claim that the old unlimited server had the same limits.

The existing listen configuration and TLS certificate/key are retained. A new checksum-pinned 3X-UI panel/database and Python environment are staged privately. Only the known local UDP 6000–19999-to-5667 DNAT rule is replaced with the stack's managed port aliases. Supported obsolete compatibility/forwarding services are retired after validation. The legacy binary remains available for recovery.

An independent pre-existing 3X-UI installation is not silently reseeded or merged. HTTP/custom authentication, nonstandard legacy listeners, manually managed competing processes and Docker host replacements need an explicit migration plan. Existing native Docker deployments are updated using `docker compose up -d --build`, not by replacing their host with systemd services.

## Preparation, switch and recovery

Only one applying installer may run at a time. The preview does not acquire an installation lock or change files. Downloads, compilation, Python dependency installation and legacy database import complete before stopping the old runtime.

During the switch the installer:

1. Saves a private file journal, service states and configuration under `/var/lib/zivpn-stack/updates/<id>/backup/`. Legacy migration also snapshots the IPv4 iptables rules.
2. Stops the collector, flushes native counters where applicable, stops managed runtime writers and backs up SQLite through its backup API.
3. Installs the matched native binary/helpers and switches to a newly prepared Python environment.
4. Starts and validates the panel/native service, private socket permissions and account-policy availability before starting the collector.
5. Commits the deployment journal; on a handled switch failure, restores the previous runtime, service states and affected legacy firewall configuration.

**An existing stack's database is never restored to an older snapshot during automatic runtime rollback.** This avoids discarding traffic accumulated after the backup. Backups are retained for operator review, and no automatic cleanup removes them.

Updates interrupt active VPN tunnels briefly; clients must reconnect. Use a maintenance window and keep SSH available. Do not edit account data or firewall rules concurrently with a migration. Forced termination, host failure and failed rollback commands can leave a `rollback-failed` journal; inspect the printed private recovery directory before retrying. Never restore an old database while panel/collector writers are active.

After success, check `systemctl is-active x-ui zivpn zivpn-xui-sync`, use `/status`, `/qualite` and `/sante 24` privately, and verify a controlled Android connection. Process health and local schema tests do not prove carrier connectivity.

## Validation scope

Automated tests use temporary files/databases and simulated service failures to exercise preservation and runtime rollback. CI also initializes the real checksum-pinned 3X-UI binary in a temporary directory and checks schema compatibility, deduplicated import, retained quotas/counters and native-helper authentication. The vendor settings command runs offline; no test installs services or contacts Telegram. Full systemd migration and rollback on a disposable server remain deployment validation work.
