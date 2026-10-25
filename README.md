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

## Get started with development

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

## Docker

```bash
docker build -t zivpn-stack:local .
python3 tools/docker_smoke.py zivpn-stack:local
```

The multi-stage image builds the native server with the checksum-pinned Go toolchain and installs Python dependencies from `uv.lock`. It includes the SQLite authentication helper, private-socket health checks and a final traffic sample on graceful shutdown. Build inputs exclude local databases and secrets.

The supplied Compose service runs **the native server only** on Linux with host networking, preserving client IPs for Standard limits. It requires an existing compatible 3X-UI database, a native configuration and TLS files outside the repository. **3X-UI and the Telegram/accounting daemon remain host services.** Their systemd, firewall and host-administration features are not containerized.

Read [the Docker deployment guide](docs/DOCKER.md) before starting Compose, especially the socket/accounting integration and migration steps. Do not start it beside a host server already listening on the same UDP ports. A healthy container confirms process and policy availability, not real Android connectivity or carrier interoperability.

## Install on a new server

The systemd installer remains the full-stack deployment path. See [deployment and migration](docs/DEPLOYMENT.md) for preparation, private configuration, installation and verification. That operational guide currently remains in French.

Intended targets are Ubuntu 24.04 and Debian 12 with Python 3.10+, systemd, IPv4, and x86-64 or ARM64. Open UDP 5667 in both cloud and host firewalls; open UDP 5668 only if legacy clients need it. The panel binds to `127.0.0.1:2053`; access it through an SSH tunnel. The bot uses long polling and needs no incoming webhook port.

The installer refuses existing installations. It verifies the pinned 3X-UI v3.8.5 archive, builds the server and installs the bot without cloning source-server accounts or secrets. **Never run the fresh installer as an upgrade on an existing production server.** Cloud provisioning and firewall rules remain operator responsibilities; the fresh-VM cloud and ARM64 deployment matrix is not fully validated.

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

## Validation and CI

Every pull request and push to `main` runs:

| Job | Checks |
| --- | --- |
| `python` | Lockfile/export consistency, Python 3.10 and 3.12 tests, secret scan and syntax checks. |
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
