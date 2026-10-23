Unit files for a validator host (systemd). The validator, its interface and its health check run as a dedicated
user, `kokoah-subnet`, with systemd's sandboxing: no new privileges, a read-only system, no access to other users'
homes, a private `/tmp`, no devices or kernel settings, and a memory cap. They can write only to
`/var/lib/kokoah-subnet`.

| Path | What |
|---|---|
| `/opt/kokoah-subnet/venv` | the installed release (`uv sync --frozen --no-dev --no-editable` with `UV_PROJECT_ENVIRONMENT`) |
| `/var/lib/kokoah-subnet/data` | the validator's data directory (`--data-dir`) |
| `/var/lib/kokoah-subnet/wallets` | the validator's hotkey and the coldkey's public file only (`--wallet-path`) |
| `/etc/kokoah-subnet/db.env` | `KOKOAH_SUBNET_DB_URL=...` (from `scripts/db.sh start`), readable by the group |
| `/etc/kokoah-subnet/validator-<name>.env` | `KOKOAH_SUBNET_ARGS=...` for instance `<name>` |
| `/etc/kokoah-subnet/alert.env` | optional: `KOKOAH_SUBNET_ALERT_WEBHOOK=...` |
| `/var/log/kokoah-subnet/` | the logs |

The backup unit runs as root (it uses Docker to dump the database).
