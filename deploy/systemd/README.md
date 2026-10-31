Unit files for a validator host (systemd). The validator, its interface and its health check run as a dedicated
user, `kokoah-subnet`, with systemd's sandboxing: no new privileges, a read-only system, no access to other users'
homes, a private `/tmp`, no devices or kernel settings, and a memory cap. They can write only to
`/var/lib/kokoah-subnet`. The wallet folder in it is read-only to the validator and hidden from the interface and
the health check, which never need the hotkey.
Everything runs from the pinned install in `/opt`, never from a working copy of the repository.

The owner's services, where the owner runs them, have users of their own and the same sandboxing:

- `kokoah-subnet-agent-service` (user `kokoah-subnet-agents`): the agent service, connecting with a role that owns
  only its database (`scripts/db.sh owner-role kokoah_agents kokoah_subnet_agents`), never as the superuser.
- `kokoah-subnet-burn.timer` (user `kokoah-subnet-burn`): the burn every 6 hours, only where a burn hotkey is
  registered. Its wallet stays in `/etc/kokoah-subnet/burn-wallet` (root's alone; a link to the wallet folder will
  do), and systemd hands its key files to each run in a private place (`LoadCredential`), so the user never owns
  them.
- `kokoah-subnet-operator.timer` (user `kokoah-subnet-operator`): the daily file-hash commitment from the owner
  hotkey, every hour until it is made. The hotkey file stays in `/etc/kokoah-subnet/operator-wallet/hotkey` (root's
  alone: the file 0600, its folder 0700) and reaches each run the same way, so the validator never reads it.
  On a host set up before, take the `--wallet` flags out of `operator.env` and move the owner hotkey's file from the
  validator's wallet folder to that place; copy it instead only if the validator also signs with that hotkey.

| Path | What |
|---|---|
| `/opt/kokoah-subnet/venv` | the installed release (the validator guide's `uv sync` command, with `UV_PROJECT_ENVIRONMENT`) |
| `/opt/kokoah-subnet/python` | the Python it runs on (`UV_PYTHON_INSTALL_DIR`), readable by the service user |
| `/opt/kokoah-subnet/scripts` | `db.sh` and `backup.sh` from the release, run by the backup unit |
| `/var/lib/kokoah-subnet/data` | the validator's data directory (`--data-dir`) |
| `/var/lib/kokoah-subnet/wallets` | the validator's hotkey and the coldkey's public file only (`--wallet-path`) |
| `/etc/kokoah-subnet/db.env` | `KOKOAH_SUBNET_DB_URL=...` (from `scripts/db.sh start`), readable by the group; the validator's connection, hidden from the interface and the health check |
| `/etc/kokoah-subnet/db-reader.env` | `KOKOAH_SUBNET_DB_READER_URL=...`: the read-only role the interface and the health check connect with |
| `/etc/kokoah-subnet/validator-<name>.env` | `KOKOAH_SUBNET_ARGS=...` for instance `<name>` |
| `/etc/kokoah-subnet/alert.env` | optional: `KOKOAH_SUBNET_ALERT_WEBHOOK=...` |
| `/etc/kokoah-subnet/agent-service.env` | `KOKOAH_SUBNET_AGENT_DB_URL=...` (the `kokoah_agents` role) and `KOKOAH_SUBNET_AGENT_ARGS=...`, readable by `kokoah-subnet-agents` |
| `/etc/kokoah-subnet/burn.env`, `burn-wallet/` | `KOKOAH_SUBNET_BURN_ARGS=--network ... --netuid ...`; the burn wallet (coldkey, `coldkeypub.txt`, `hotkeys/default`) |
| `/etc/kokoah-subnet/operator.env`, `operator-wallet/hotkey` | `KOKOAH_SUBNET_OPERATOR_ARGS=--network ... --netuid ... --builder ...`; the owner hotkey's file |
| `/var/log/kokoah-subnet/` | the logs |
| `/var/backups/kokoah-subnet/` | the daily backups of the record (14 kept), readable by root only |

The backup unit runs as root (it uses Docker to dump the database).
