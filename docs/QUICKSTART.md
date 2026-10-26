# Beginner walkthrough

Start with [Get started in the README](../README.md#get-started-install-on-your-server). This page explains the SSH, Telegram and panel steps that usually need more detail. The installation commands are run on the server; Android and your personal computer are used only for the indicated connection steps.

## Connect to your server

Get the public IP address and SSH username from your cloud provider. From a terminal on your personal computer, replace both placeholders:

```bash
ssh YOUR_SSH_USER@YOUR_SERVER_IP
```

Once connected, the terminal commands run on the remote server. The README assumes a sudo-enabled user. If you are logged in as root, omit `sudo`.

Use a supported x86-64 (amd64) or ARM64 Ubuntu 24.04 or Debian 12 server with systemd and public IPv4. A Docker container or your Windows terminal itself is not a full-stack installation target. For the native-only Docker deployment, follow [DOCKER.md](DOCKER.md).

## Create the bot and prepare private files

1. In Telegram, open the official [@BotFather](https://t.me/BotFather), send `/newbot`, and follow its prompts. See [Telegram's official bot instructions](https://core.telegram.org/bots/features#botfather).
2. Keep the token private. Create a different bot for each server; two running collectors must not share a polling bot.
3. Follow README step 3 to create `/root/zivpn-config/settings.json` and `/root/zivpn-config/secrets.json`. Put the new bot token in `secrets.json` before using the ID lookup below.

The settings file should have this structure, with **your own** address and administrator ID:

```json
{
  "server_address": "YOUR_SERVER_IP_OR_DOMAIN",
  "primary_admin_id": 123456789,
  "network_interface": null
}
```

`primary_admin_id` is a number without quotation marks. The server address has quotation marks. `null` selects automatic network-interface detection. Keep the commas between properties, with no comma after the final property.

In `secrets.json`, replace all example credentials. `telegram_token` is the BotFather token; `panel_username` and `panel_password` are credentials you choose for the web panel. The panel password must be unique and at least 20 characters long. These are separate from your SSH login and VPN account password.

Edit with `sudo nano FILE`: **Ctrl+O**, then **Enter** saves; **Ctrl+X** exits. The installer checks that the secrets file has mode `0600`; README step 3 sets this for you. Keep private configuration files outside the cloned repository.

## Find your Telegram user ID

For a **new bot that is not yet running on a server**:

1. Open its private chat from the Telegram account that will administer the VPN.
2. Send `/start` to that bot.
3. On the server, run the following command after storing the token in `/root/zivpn-config/secrets.json`. It reads pending private-chat updates through the official [Telegram Bot API](https://core.telegram.org/bots/api#getupdates), and prints user IDs without printing the token.

```bash
sudo python3 - <<'PY'
import json
import urllib.request

try:
    with open('/root/zivpn-config/secrets.json', encoding='utf-8') as source:
        token = json.load(source)['telegram_token']
    request = urllib.request.Request(
        'https://api.telegram.org/bot' + token + '/getUpdates',
        data=json.dumps({'allowed_updates': ['message']}).encode(),
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.load(response)
    if result.get('ok') is not True:
        raise ValueError('Telegram rejected the request')
except Exception:
    raise SystemExit('Lookup failed. Check the private token file and server HTTPS access.') from None

users = {}
for update in result.get('result', []):
    message = update.get('message', {})
    user = message.get('from', {})
    chat = message.get('chat', {})
    if chat.get('type') == 'private' and user.get('id') == chat.get('id'):
        users[user['id']] = {
            'id': user['id'], 'username': user.get('username'),
            'first_name': user.get('first_name'),
        }
if not users:
    raise SystemExit('No private message found. Send /start to your new bot and retry.')
for user in users.values():
    print(json.dumps(user, ensure_ascii=True))
PY
```

Copy the numeric `id` belonging to **your own Telegram account** into `primary_admin_id` in `settings.json`. If more than one account appears, identify yours by its username/name before selecting it. Do not use the bot's ID or leave `123456789` from the example. If the bot is already in use, retain the configured administrator ID instead of running a second polling client.

## Choose the correct installation action

Run the README's `--check` command first. It reports `install`, `migrate` or `update`.

- **Install:** fill in both private configuration files, validate them with `--check`, then apply the installation command.
- **Migrate:** fill in the new bot/panel configuration; existing legacy VPN passwords and TLS files are retained. Read [profile selection and recovery](UPGRADES.md#migrate-zahidbd2udp-zivpn) before applying it.
- **Update:** use `sudo python3 tools/install.py` without new setup credentials; existing settings, accounts and credentials are preserved.

A preview does not configure the server. Applying a supported update/migration restarts managed services and interrupts current tunnels briefly. An unsupported layout or occupied port requires reading the diagnostic before proceeding.

## Open the VPN port

Allow incoming **UDP 5667** in your cloud firewall/security group and in any enabled host firewall. Select UDP, not TCP. Keep your current SSH access rule. UDP 5668 is optional for older Premium profiles; the panel's TCP 2053 port stays private.

For an already enabled UFW firewall, allow the VPN port on the server:

```bash
sudo ufw allow 5667/udp
```

Use this command only if the host already uses UFW. The cloud firewall is a separate setting in the provider's console; neither this command nor the installer opens it. See [provider-specific firewall notes](DEPLOYMENT.md#ouvrir-les-ports).

## Verify the installation

`sudo systemctl is-active x-ui zivpn zivpn-xui-sync` should print three `active` lines. If it prints `failed` or `inactive`, inspect the logs shown in README step 5. Repeating the installer without reading its diagnostic may not fix a partial installation.

In the primary administrator's private Telegram chat, send `/menu` and `/status`. For a new server, `/add <name> <unique-password> standard 1` creates a Standard test account; replace the bracketed values. Connect the Android client to your server's address on UDP 5667 with that account password. Test browsing, then review `/qualite` and `/sante 24`. An active service is not proof that a cloud firewall or mobile network allows the tunnel.

## Access the panel

The panel is bound to the server's loopback address. First, on the **server**, read its generated panel path:

```bash
sudo python3 - <<'PY'
from contextlib import closing
import sqlite3

with closing(sqlite3.connect('file:/etc/x-ui/x-ui.db?mode=ro', uri=True)) as db:
    row = db.execute("SELECT value FROM settings WHERE key='webBasePath'").fetchone()
    if row is None:
        raise SystemExit('Panel path was not found; inspect the x-ui service logs.')
    print(row[0])
PY
```

Then open a **second terminal on your personal computer**, replacing the SSH placeholders:

```bash
ssh -N -L 2053:127.0.0.1:2053 YOUR_SSH_USER@YOUR_SERVER_IP
```

Leave that terminal open; it is expected to wait without returning a command prompt. In your computer's browser, append the printed panel path to `http://127.0.0.1:2053` and log in using the panel username/password you configured. This access does not require opening the panel to the public Internet.

## Development commands are optional

`uv sync`, `make test`, `make build`, `make test-native` and `make check` are contributor tools. They do not create private server settings, open cloud firewall ports or install/start the full stack. Use [Development and tests](../README.md#development-and-tests) only when working on the source.
