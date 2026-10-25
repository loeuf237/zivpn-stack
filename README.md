# ZiVPN Stack

[![Validation](https://github.com/loeuf237/zivpn-stack/actions/workflows/ci.yml/badge.svg)](https://github.com/loeuf237/zivpn-stack/actions/workflows/ci.yml)

A native UDP server compatible with the ZiVPN Android client, with 3X-UI account management and private Telegram administration. This repository contains the server, deployment tools and tests. User databases, private keys and credentials stay on each server.

## Features

- **Standard:** a default combined upload/download limit of **1,000,000 bytes/s per public IP**, shared across that IP's accounts and tunnels.
- **Premium:** a default combined limit of **4,000,000 bytes/s per account**, shared across its IPs and tunnels.
- Explicit account profiles and persistent per-account speed overrides. When several Standard accounts share an IP, the lowest connected account limit applies.
- Quotas and expiry dates enforced against the private 3X-UI SQLite database.
- A private Unix socket for native sessions, accounting and health metrics; no public administration API.
- Telegram account management, reports, cancellable diagnostics and task tracking.
- BBR, Reno and CUBIC congestion control, plus configurable QUIC packet sizing.
- Locked Python dependencies with **uv**, a native-server **Docker image**, and GitHub Actions validation.

Both profiles use **UDP 5667**. UDP 5668 is an optional compatibility listener for older Premium profiles. Bandwidth values are shared ceilings, not guaranteed throughput; 1 MB/s means 1,000,000 bytes/s.

## Get started: install on your server

**Use this section to run the VPN stack.** The [development commands](#development-and-tests) are for building and testing the source code. A working installation also needs your server address, Telegram configuration and firewall access; copying build commands alone does not provide these.

### 1. Prepare your server and Telegram bot

Use an x86-64 (amd64) or ARM64 Ubuntu 24.04 or Debian 12 **server VM**, with systemd, a public IPv4 address, and a sudo-enabled SSH account. Run the commands below **on that server**, not on your personal computer. If you log in as root, omit `sudo`.

For a new installation or legacy migration, have these ready. If your ZiVPN Stack is already configured, keep its current bot/credentials and proceed to step 2:

- Your server's public IPv4 address or a DNS name pointing to it.
- A **new Telegram bot token**, created with `/newbot` in the official [@BotFather](https://t.me/BotFather) chat. Use a separate bot for each server.
- Your **numeric Telegram user ID**, not your `@username`. See [how to find it using your own bot](docs/QUICKSTART.md#find-your-telegram-user-id).
- A panel administrator username and a unique password of at least 20 characters.

In your cloud provider's firewall/security group, allow **UDP 5667**. If a host firewall is enabled, allow it there too. Keep your existing SSH access rule. UDP 5668 is optional for older Premium profiles. The installer does not configure your cloud firewall.

### 2. Download the project and inspect the server

On a new server, install the small set of tools needed to start:

```bash
sudo apt-get update
sudo apt-get install -y git python3 iptables nano ca-certificates
git clone https://github.com/loeuf237/zivpn-stack.git
cd zivpn-stack
sudo python3 tools/install.py --check
```

If the repository is already cloned, use its existing directory and run `git switch main` followed by `git pull --ff-only` instead of cloning it again.

Read the `Planned action` message:

| Result | Next step |
| --- | --- |
| `install` | New server: continue with step 3. |
| `migrate` | Legacy `udp-zivpn` server: continue with step 3 to add the panel and bot. Existing passwords and TLS files are retained. |
| `update` | Existing ZiVPN Stack: run `sudo python3 tools/install.py`, then go to step 5. Its current settings and credentials are preserved. |
| An error or unsupported layout | Follow the diagnostic and [migration guide](docs/UPGRADES.md) before applying changes. |

The inspection does not install or restart services. Applying an update or migration briefly interrupts VPN connections and retains a private recovery backup.

### 3. Enter your private settings (new install or legacy migration)

Create the private files outside the repository. These commands retain files that already exist:

```bash
sudo install -d -m 700 /root/zivpn-config
sudo test -e /root/zivpn-config/settings.json || sudo install -m 600 config/settings.example.json /root/zivpn-config/settings.json
sudo test -e /root/zivpn-config/secrets.json || sudo install -m 600 config/secrets.example.json /root/zivpn-config/secrets.json
sudo nano /root/zivpn-config/settings.json
```

In `settings.json`, replace `vpn.example.com` with your server address and `123456789` with **your own numeric Telegram ID**. Leave `network_interface` as `null` for automatic detection. Keep JSON quotation marks and commas intact. In nano, save with **Ctrl+O**, press **Enter**, then exit with **Ctrl+X**.

Now edit the credentials:

```bash
sudo nano /root/zivpn-config/secrets.json
sudo chmod 600 /root/zivpn-config/settings.json /root/zivpn-config/secrets.json
```

Replace `REPLACE_ON_TARGET` with your bot token, choose the panel username, and replace `REPLACE_WITH_UNIQUE_PASSWORD` with your unique panel password. Do not leave example values in these files. Keep the token and password in these private files and your password manager.

### 4. Check your configuration, then install

```bash
sudo python3 tools/install.py --settings /root/zivpn-config/settings.json --secrets /root/zivpn-config/secrets.json --check
```

If the check succeeds and the planned action matches your server, apply it:

```bash
sudo python3 tools/install.py --settings /root/zivpn-config/settings.json --secrets /root/zivpn-config/secrets.json
```

The installer downloads verified artifacts, builds the native server, installs Python dependencies and starts the services. **You do not need to install uv, Go or Make manually for this deployment path.** The legacy migration imports Standard accounts by default (1 MB/s per public IP); see [profile selection and recovery](docs/UPGRADES.md) before migrating if you need Premium accounts.

### 5. Verify the services and connect your first client

```bash
sudo systemctl is-active x-ui zivpn zivpn-xui-sync
```

Expect **three `active` lines**. If a service is not active, read its recent logs and resolve the error before retrying the installer:

```bash
sudo journalctl -u x-ui -u zivpn -u zivpn-xui-sync -n 80 --no-pager
```

Open a private chat with your Telegram bot from the administrator account, send `/start`, then `/menu` and `/status`. For a new installation, create a test account with `/add <name> <unique-password> standard 1`, replacing the bracketed values. On Android, enter the server address, UDP port **5667** and that account password, then verify browsing through the tunnel. Existing migrated accounts keep their original passwords.

The 3X-UI panel listens on `127.0.0.1:2053`. See [accessing the panel through SSH](docs/QUICKSTART.md#access-the-panel) and the [full beginner walkthrough](docs/QUICKSTART.md). The intended platform/cloud matrix and full systemd migration still need validation on disposable servers; successful service checks alone do not prove client connectivity.

## Docker

```bash
docker build -t zivpn-stack:local .
python3 tools/docker_smoke.py zivpn-stack:local
```

The multi-stage image builds the native server with the checksum-pinned Go toolchain and installs Python dependencies from `uv.lock`. It includes the SQLite authentication helper, private-socket health checks and a final traffic sample on graceful shutdown. Build inputs exclude local databases and secrets.

The supplied Compose service runs **the native server only** on Linux with host networking, preserving client IPs for Standard limits. It requires an existing compatible 3X-UI database, a native configuration and TLS files outside the repository. **3X-UI and the Telegram/accounting daemon remain host services.** Their systemd, firewall and host-administration features are not containerized.

Read [the Docker deployment guide](docs/DOCKER.md) before starting Compose, especially the socket/accounting integration and migration steps. Do not start it beside a host server already listening on the same UDP ports. A healthy container confirms process and policy availability, not real Android connectivity or carrier interoperability.

## Manage accounts from Telegram

Administrative commands belong in the primary administrator's private chat with the bot. The bot's command names and interface remain in French. Use unique passwords in place of these fictitious examples:

```text
/add alice unique-example-password standard 1
/add bob another-example-password premium 4
/vitesse alice 0.75
/profil alice premium 6
/vitesse alice defaut
/limit standard 1
/limit premium 4
```

| Command | Purpose |
| --- | --- |
| `/add <name> <password> <standard\|premium> [MB/s]` | Create an account with an explicit profile. |
| `/vitesse <name> <MB/s\|defaut>` | Set an account limit or restore inheritance. |
| `/profil <name> <standard\|premium> [MB/s]` | Change profile while preserving password, quotas, expiry and counters; reconnect the account afterward. |
| `/limit <standard\|premium> <MB/s>` | Change a profile default without replacing custom account limits. |
| `/sante 24` or `/sante 48` | Read the available private health history. |
| `/qualite` | Sample shared throughput and limiter waits for five seconds. |
| `/taches` | Inspect background tasks. |

Changing a profile without a speed argument preserves an existing custom limit. Settings persist in the private 3X-UI database.

## QUIC congestion control and packet sizing

The native configuration accepts `congestionControl: "bbr"`, `"reno"` or `"cubic"`; BBR is the default. Congestion control does not change Standard or Premium policy limits. Merge this fragment into a complete native configuration:

```json
{
  "congestionControl": "cubic",
  "quic": {
    "initialPacketSize": 1200,
    "disablePathMTUDiscovery": true
  }
}
```

`initialPacketSize` is the QUIC payload size before obfuscation, between 1,200 and 1,452 bytes. Zero or omission retains initial defaults of 1,252 bytes for IPv4 and 1,232 for IPv6. Salamander adds 8 bytes to the UDP payload. Disabling path MTU discovery keeps the selected size and may help constrained paths; it does not eliminate every source of loss.

See [QUIC fixes and limitations](docs/BBR_FIX.md) for BBR pacing, ACK aggregation, CUBIC provenance and Salamander concurrency fixes. Local tests cover saturated queues, slow links and MTU constraints. Telemetry distinguishes QUIC loss reasons and some late acknowledgements; it does not directly measure physical packet loss.

## Development and tests

These commands are for contributors. They build and test the project; they do not deploy the services.

Use Linux, Python 3.10 or newer, Git, Make and [uv 0.13.1](https://docs.astral.sh/uv/getting-started/installation/). Native race tests also require a C compiler.

```bash
git clone https://github.com/loeuf237/zivpn-stack.git
cd zivpn-stack
uv sync --locked --python 3.12
uv run --locked make test
uv run --locked make build
uv run --locked make test-native
uv run --locked make check
```

`pyproject.toml` declares the Python dependencies; `uv.lock` locks their full resolution. `uv sync --locked` fails if the manifest and lock disagree. `requirements.txt` is a generated compatibility export for the existing systemd installer, not a separate dependency source.

To update dependencies deliberately:

```bash
uv lock --upgrade
uv sync --locked
uv export --locked --no-dev --no-hashes --no-emit-project --output-file requirements.txt
uv run --locked make test
```

The build downloads the Go version and verifies the archive SHA-256 from `config/artifacts.json`. The QUIC fork currently requires **Go 1.21.13**, an old toolchain; upgrading it requires a coordinated fork migration. `make test-native` runs race-enabled QoS, server, congestion, integration, QUIC and obfuscation tests, excluding stress tests. `.venv/`, `.cache/` and `build/` are local artifacts.

## Validation and CI

Every pull request and push to `main` runs:

| Job | Checks |
| --- | --- |
| `python` | Lockfile/export consistency, Python 3.10 and 3.12 tests, pinned-panel migration smoke check, secret scan and syntax checks. |
| `native` | Checksum-pinned native build and race-enabled Go tests. |
| `docker` | Compose validation, image build, policy acceptance/rejection, private API health and shutdown accounting smoke test. |

The workflow can also be started manually. Its token has read-only repository permissions, and pull requests do not receive deployment credentials or publish images. The `python` and `native` job names remain stable for branch protection; require `docker` as well once the job has run successfully.

Tests use fictitious identities and temporary databases. They do not send Telegram messages or restart production services. Review [validation status](docs/VALIDATION.md) and [observability](docs/OBSERVABILITY.md) before deployment, and follow the existing [upgrade checks](docs/OBSERVABILITY.md#contrôles-de-mise-à-jour) for production changes.

## Repository layout

| Path | Contents |
| --- | --- |
| `native/` | Go server fork and module manifests. |
| `src/` | Telegram daemon, account policy, accounting and Python tests. |
| `scripts/` | Host administration and authentication helpers. |
| `config/` | Public configuration templates and artifact checksums. |
| `deploy/` | systemd units and Docker runtime support. |
| `tools/` | Build, installation, secret scanning and Docker smoke checks. |
| `tests/` | Deployment and integration checks. |
| `docs/` | Operational guides and known validation limits. |

## Security and licenses

Never commit account databases, client exports, tokens, private keys, backups or traffic logs. Run `uv run --locked python tools/secret_scan.py --staged` before committing. The Docker service needs database writes for first-login activation and accounting; do not expose its socket publicly or mount the Docker daemon socket into it.

Destination and DNS observations are retained in memory for at most 30 minutes. They do not decrypt HTTPS and do not prove that an application was used. Sensitive reports are reserved for the primary administrator's private chat.

Read [SECURITY.md](SECURITY.md) for secret handling and private migrations, and [THIRD_PARTY.md](THIRD_PARTY.md) for upstream notices and licenses.
