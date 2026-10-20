Unit files for a validator host (systemd). `%h` is the service user's home; `<N>` the netuid. Each validator instance
reads `~/.local/share/kokoah/subnet-validator-<name>.env` (`KOKOAH_SUBNET_ARGS=...`, optionally its own
`KOKOAH_SUBNET_DB_URL`); every unit reads `~/.local/share/kokoah/subnet-db.env` written by `scripts/db.sh start`.
