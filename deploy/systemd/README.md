Unit files for a validator host (systemd). `%h` is the service user's home; `<N>` the netuid. Each validator instance
reads `~/.local/share/kokoah/subnet-validator-<name>.env` (`KOKOAH_SUBNET_ARGS=...`, optionally its own
`KOKOAH_SUBNET_DB_URL`); every unit reads `~/.local/share/kokoah/subnet-db.env` written by `scripts/db.sh start`.

The owner's agent service (`kokoah-subnet-agent-service.service`) reads `~/.local/share/kokoah/subnet-agent-service.env`:
`KOKOAH_SUBNET_AGENT_DB_URL=...` (a database of its own, `scripts/db.sh create-db kokoah_subnet_agents`) and
`KOKOAH_SUBNET_AGENT_ARGS=--netuid <N> --network <net>`. It listens on 127.0.0.1:48130; the reverse proxy publishes
it (`deploy/nginx/kokoah-subnet-agents.conf`). The backup timer saves its database and stored code with the record.
