# Repository Guidelines

## Project Structure & Module Organization

`native/core/`, `native/extras/`, and `native/app/` contain the Go fork and its module manifests. The deployed server entry point is `native/core/cmd/zivpn-native`. `src/` contains the Python Telegram daemon, account policy, accounting, and report modules, alongside their existing tests. `tests/` covers deployment configuration and downloads. `scripts/` holds root administration tools; `deploy/systemd/` contains units. Configuration templates live in `config/`; operational instructions are in `docs/`.

## Build, Test, and Development Commands

Use uv 0.13.1 and `uv sync --locked --python 3.12` before Python development. Run `uv run --locked make test` for Python policy, accounting, Telegram, and deployment checks. `pyproject.toml` and `uv.lock` are authoritative; regenerate the installer compatibility file with `uv export --locked --no-dev --no-hashes --no-emit-project --output-file requirements.txt` after dependency changes. `make build` downloads the checksum-pinned Go toolchain and builds `build/zivpn-native`. `make test-native` runs race-enabled QoS and tunnel integration tests. `make check` checks staged secrets, Python syntax, and shell syntax. Build the native-only Docker image with `docker build -t zivpn-stack:local .` and verify it with `python3 tools/docker_smoke.py zivpn-stack:local`. The host installer now selects install, update or legacy migration; use `--check` for a read-only plan. Never execute an actual production deployment as part of repository tests. Exercise updates with temporary roots and mocked services; `tools/panel_migration_smoke.py` validates the pinned panel offline in a temporary directory.

## Coding Style & Naming Conventions

Use four spaces for Python indentation and descriptive `snake_case` functions. Retain the surrounding style when changing older modules. Format Go changes with `gofmt`; name packages briefly and use exported identifiers only when needed. Keep module files and checksums consistent. Shell scripts use Bash with strict error handling. No automatic Python formatter is currently configured.

## Testing Guidelines

Python tests use `unittest` and `test_*.py`; Go tests use `*_test.go`. Prefer behavioral checks for shared bandwidth, authorization, cancellation, and uncertain delivery. Mock Telegram transport; do not send test messages or restart production VPN services. Use temporary SQLite databases and fictitious identities. No numerical coverage threshold is configured.

## Commit & Pull Request Guidelines

This repository begins with an imported implementation, so there is no established historical commit convention. Use concise imperative subjects, such as `Fix Telegram retry handling`. PR descriptions should explain behavior, validation, and deployment implications. Link relevant issues and attach screenshots when changing Telegram presentation.

## Security & Configuration

Read `SECURITY.md`. Never commit real credentials, databases, connection audits, or private keys. Run the staged secret scan before committing. Preserve upstream licensing and the Standard per-IP and Premium per-account bandwidth semantics.
