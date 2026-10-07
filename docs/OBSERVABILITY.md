# Connection health and operational history

The root-only native Unix socket exposes `/health` alongside `/snapshot`. No public HTTP endpoint is added. Numeric QUIC metrics include smoothed RTT, sent/acknowledged/declared-lost packets and the current consecutive PTO count. Payload activity records only timestamps and byte counters. Frames, passwords, DNS names and contents are excluded from this operational history.

`/sante [24|48]` and `/qualite` and their menu buttons require the primary administrator in their private chat. `/sante` displays recorded coverage, close reasons, short tunnels, useful activity before idle expiration, auth outcomes and Telegram error counts. `/qualite` samples for five seconds, displaying shared group rates and limiter wait frequency. Defaults are 1,000,000 bytes/s per public IP for Standard and 4,000,000 bytes/s per account for Premium, both directions combined. A mobile carrier may put many users behind one public IP. Bursts can momentarily exceed a ceiling over a short interval.

The native process retains a fixed 8,192-event ring, with a 48-hour age limit. The bot samples once per minute into `/var/lib/zivpn-telegram/health.db`, mode 0600, retaining seven days or at most 100,000 closures. A restart changes the native epoch; comparisons across epochs and recreated buckets are discarded. Missing ring events are counted explicitly. Native auth counters cover the current process; reports count differences between recorded samples, not unrecoverable events before collection. This history starts at deployment and cannot reconstruct prior days. Kernel error counts are compared between samples; they describe this VM, not the mobile network.

Alerts are grouped, at most one message every 30 minutes, sent only to the primary administrator: unavailable telemetry/services, at least three internal auth errors between samples, increasing UDP/NIC error counters, conntrack at 80%, missing close events and uncertain Telegram sends. Frequent idle expiry alone does not generate an outage alert. Telegram messages with uncertain delivery are not resent blindly. Logs use fixed error categories, never raw Telegram descriptions. Successful auth and ordinary closes are counted rather than repeatedly logged; traffic logs roll up every ten minutes.

For Android diagnosis, compare five minutes of active browsing, background/locked screen, and Wi-Fi versus mobile data. Note operator, time and whether browsing actually failed. Compare `/qualite` RTT/rate and `/sante` closure/activity data; do not paste passwords into reports. Adjust MTU or keepalive only after this controlled comparison; server metrics cannot establish the handset cause alone.

## Upgrade checks

Back up changed binaries/scripts, the service logging drop-in and SQLite using its backup API. Preserve site-specific bot constants, primary admin and legacy aliases when applying source changes. Install the structured helper and native server together: the old helper protocol is incompatible with the new authenticator. Run `make test`, `make test-native`, secret scan and syntax checks. Flush counters before stopping the VPN; restart once to activate the binary. Verify services, socket permissions, unchanged ceilings and monotonic account counters. Roll back helper/policy/server together if health checks fail. Never run the fresh installer against a live installation.

## Account profiles and adjustable ceilings

Primary administrator, private chat only:

- `/add alice unique-password standard 1` creates a Standard account with a 1 Mo/s ceiling per public IP.
- `/add bob another-password premium 4` creates a Premium account with a 4 Mo/s ceiling per account across IPs.
- `/vitesse alice 0.75` changes the account's configured ceiling; `/vitesse alice defaut` restores inheritance.
- `/profil alice premium 6` changes profile and optionally speed, preserving counters, quotas, expiry and password. The affected account reconnects; other accounts remain connected. Without a speed, an existing custom speed is preserved, otherwise the new profile default applies.
- `/limit standard 1` or `/limit premium 4` changes a default; existing custom ceilings are preserved.

Profile is explicit, never inferred from speed or port. Mo/s means 1,000,000 bytes/s. Positive values from one byte/s up to 1,000,000 Mo/s are accepted; this is a ceiling, not a throughput guarantee. `zivpn_qos_defaults` and `zivpn_qos_accounts` in the private 3X-UI database persist settings. Existing accounts without overrides inherit their profile default. Duplicate names/passwords are refused at creation.

All Standard accounts sharing a public IP share one bucket. When their configured ceilings differ, the lowest ceiling among connected Standard accounts applies to that IP. A departing account releases its restriction. Premium uses one bucket per account, independent of Standard on the same IP. Rate changes update existing limiters, without resetting burst credit. Pending chunk reservations can briefly retain their prior scheduling before the new rate settles. A profile change revokes old-profile tunnels and requires reconnection. Policy updates are pushed through the root-only socket and rechecked every 15 seconds, including with no connected users. `/qualite` reports effective ceilings.
