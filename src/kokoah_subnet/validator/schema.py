"""The record of everything the validator sees and decides, as one relational schema (PostgreSQL in production,
SQLite for tests and quick local runs). Dashboards and analysis read these tables; nothing here affects pay.

Conventions: `day` is the scored UTC day (YYYY-MM-DD); money is USD; alpha is in alpha (not rao); addresses are
lowercase; `raw` columns keep the source record as JSON so nothing the sources said is lost.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

# one row per validator start (what code and settings produced the records that follow)
runs = Table(
    "runs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("network", String(64), nullable=False),
    Column("netuid", Integer, nullable=False),
    Column("validator_hotkey", String(64)),
    Column("builder", String(42)),
    Column("burn_hotkey", String(64)),
    Column("spec_version", String(16)),
    Column("code_version", String(32)),
    Column("config", JSON),
)

# one row per scored day: the headline figures and the full published artifact
days = Table(
    "days",
    metadata,
    Column("day", String(10), primary_key=True),
    Column("day_index", Integer, nullable=False),
    Column("scored_at", DateTime(timezone=True)),
    Column("run_id", Integer),
    Column("block", BigInteger),
    Column("tempo", Integer),
    Column("price_tao", Float),
    Column("tao_usd", Float),
    Column("price_usd", Float),
    Column("pool_alpha", Float),
    Column("paid_alpha", Float),
    Column("burn_alpha", Float),
    Column("burn_hotkey", String(64)),
    Column("miners", Integer),
    Column("miners_with_fills", Integer),
    Column("volume_usd", Float),
    Column("builder_fee_usd", Float),
    Column("builder_file_sha256", String(64)),
    Column("builder_fills", Integer),
    Column("hash_matches_operator", Boolean),
    Column("spec_version", String(16)),
    Column("result_json", Text),
)

builder_files = Table(
    "builder_files",
    metadata,
    Column("day", String(10), primary_key=True),
    Column("sha256", String(64), nullable=False),
    Column("bytes", Integer),
    Column("fills", Integer),
    Column("users", Integer),
    Column("downloaded_at", DateTime(timezone=True)),
    Column("url", Text),
)

# every fill in the builder file, bound or not, with why it was excluded when it was
fills = Table(
    "fills",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("day", String(10), nullable=False, index=True),
    Column("address", String(42), nullable=False, index=True),
    Column("hotkey", String(64), index=True),
    Column("time_ms", BigInteger, nullable=False),
    Column("coin", String(32), nullable=False),
    Column("side", String(8), nullable=False),
    Column("px", Float, nullable=False),
    Column("sz", Float, nullable=False),
    Column("notional", Float, nullable=False),
    Column("crossed", Boolean, nullable=False),
    Column("builder_fee", Float, nullable=False),
    Column("total_fee", Float),
    Column("counterparty", String(64)),
    Column("closed_pnl", Float),
    Column("twap_id", BigInteger),
    Column("mark_px", Float),
    Column("excluded", String(32)),  # unbound | cluster | bound_counterparty | mark_clip, or NULL when counted
    UniqueConstraint("day", "address", "time_ms", "coin", "side", "px", "sz", name="uq_fill"),
)

# every per-miner figure of the day, with the parts behind each factor
miner_days = Table(
    "miner_days",
    metadata,
    Column("day", String(10), primary_key=True),
    Column("hotkey", String(64), primary_key=True),
    Column("address", String(42), nullable=False, index=True),
    Column("uid", Integer),
    Column("credit", Float),
    Column("builder_fee", Float),
    Column("total_fee", Float),
    Column("notional", Float),
    Column("taker_notional", Float),
    Column("fills", Integer),
    Column("closed_pnl", Float),
    Column("phi_hat", Float),
    Column("fee_hat", Float),
    Column("confidence", Float),
    Column("quality", Float),
    Column("skill_raw", Float),
    Column("g", Float),
    Column("skill", Float),
    Column("holding", Float),
    Column("staked_min_alpha", Float),
    Column("paid_window_alpha", Float),
    Column("collateral_min_alpha", Float),
    Column("gates_pass", Boolean),
    Column("round_trips_window", Integer),
    Column("active_days_window", Integer),
    Column("mean_equity_usd", Float),
    Column("returns_days", Integer),
    Column("mirrored_share", Float),
    Column("counterparty_share", Float),
    Column("position_share", Float),
    Column("credit_factor", Float),
    Column("reported", Boolean),
    Column("zeroed", Boolean),
    Column("multiplier", Float),
    Column("share_alpha", Float),
    Column("cap_usd", Float),
    Column("cap_alpha", Float),
    Column("pay_alpha", Float),
    Column("pay_usd", Float),
    Column("exchange_taker_bps", Float),
    Column("exchange_maker_bps", Float),
)

# the smoothed state after each day (history, not only the latest)
state_history = Table(
    "state_history",
    metadata,
    Column("day", String(10), primary_key=True),
    Column("hotkey", String(64), primary_key=True),
    Column("phi_hat", Float),
    Column("fee_hat", Float),
    Column("g", Float),
    Column("paid", JSON),
    Column("zero_until_day", Integer),
)

states = Table(  # the latest state per hotkey (what the next day starts from)
    "states",
    metadata,
    Column("hotkey", String(64), primary_key=True),
    Column("json", Text, nullable=False),
)

equity = Table(
    "equity",
    metadata,
    Column("address", String(42), primary_key=True),
    Column("day", String(10), primary_key=True),
    Column("value", Float, nullable=False),
    Column("flows", Float, nullable=False),
    Column("fetched_at", DateTime(timezone=True)),
    Column("raw", JSON),  # the clearinghouse margin summary
)

ledger_updates = Table(  # deposits, withdrawals, transfers: the raw ledger records behind `flows`
    "ledger_updates",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("address", String(42), nullable=False, index=True),
    Column("time_ms", BigInteger, nullable=False),
    Column("hash", String(80)),
    Column("type", String(40)),
    Column("usdc", Float),
    Column("raw", JSON),
    UniqueConstraint("address", "time_ms", "hash", "type", name="uq_ledger"),
)

positions = Table(  # end-of-day positions per coin (the mirror rule reads notional)
    "positions",
    metadata,
    Column("address", String(42), primary_key=True),
    Column("day", String(10), primary_key=True),
    Column("coin", String(32), primary_key=True),
    Column("notional", Float, nullable=False),
    Column("szi", Float),
    Column("entry_px", Float),
    Column("unrealized_pnl", Float),
    Column("leverage", Float),
    Column("raw", JSON),
)

activity = Table(
    "activity",
    metadata,
    Column("address", String(42), primary_key=True),
    Column("day", String(10), primary_key=True),
    Column("round_trips", Integer, nullable=False),
    Column("fills", Integer, nullable=False),
    Column("notional", Float, nullable=False),
)

fee_rates = Table(
    "fee_rates",
    metadata,
    Column("address", String(42), primary_key=True),
    Column("day", String(10), primary_key=True),
    Column("taker_bps", Float),
    Column("maker_bps", Float),
    Column("raw", JSON),
)

price_samples = Table(
    "price_samples",
    metadata,
    Column("day", String(10), primary_key=True),
    Column("block", BigInteger, primary_key=True),
    Column("price_tao", Float, nullable=False),
)

tao_marks = Table(
    "tao_marks",
    metadata,
    Column("day", String(10), primary_key=True),
    Column("source", String(32), primary_key=True),
    Column("mark", Float, nullable=False),
)

stake_samples = Table(
    "stake_samples",
    metadata,
    Column("day", String(10), primary_key=True),
    Column("hotkey", String(64), primary_key=True),
    Column("block", BigInteger, primary_key=True),
    Column("alpha", Float, nullable=False),
)

# every tempo as the chain pays it: one row per UID per epoch (emission = alpha paid that epoch)
epochs = Table(
    "epochs",
    metadata,
    Column("block", BigInteger, primary_key=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("step", BigInteger),
    Column("tempo", Integer),
    Column("price_tao", Float),
    Column("num_uids", Integer),
)

epoch_neurons = Table(
    "epoch_neurons",
    metadata,
    Column("block", BigInteger, primary_key=True),
    Column("uid", Integer, primary_key=True),
    Column("hotkey", String(64), index=True),
    Column("coldkey", String(64)),
    Column("active", Boolean),
    Column("validator_permit", Boolean),
    Column("alpha_stake", Float),
    Column("tao_stake", Float),
    Column("incentive", Float),
    Column("emission", Float),
    Column("dividends", Float),
    Column("rank", Float),
    Column("trust", Float),
    Column("consensus", Float),
    Column("pruning_score", Float),
    Column("last_update", BigInteger),
    Column("block_at_registration", BigInteger),
    Column("collateral_locked", Float),
    Column("collateral_min", Float),
)

# one row per hotkey ever seen on the subnet: who it is, when it registered, when it was bound and paid
miners = Table(
    "miners",
    metadata,
    Column("hotkey", String(64), primary_key=True),
    Column("coldkey", String(64)),
    Column("uid", Integer),
    Column("block_at_registration", BigInteger),
    Column("registered_at", DateTime(timezone=True)),  # estimated from the block height at 12 s a block
    Column("first_seen_at", DateTime(timezone=True)),
    Column("last_seen_at", DateTime(timezone=True)),
    Column("address", String(42)),
    Column("bound_block", BigInteger),
    Column("bound_at", DateTime(timezone=True)),
    Column("first_paid_at", DateTime(timezone=True)),
    Column("last_paid_at", DateTime(timezone=True)),
    Column("total_emission_alpha", Float),  # from the epochs (what the chain paid)
    Column("total_pay_alpha", Float),  # from the scoring (what the validator allocated)
    Column("deregistered_at", DateTime(timezone=True)),
    Column("role", String(16)),  # trader | owner | validator | burn | unknown
)

neurons = Table(  # the metagraph at scoring time
    "neurons",
    metadata,
    Column("day", String(10), primary_key=True),
    Column("uid", Integer, primary_key=True),
    Column("block", BigInteger),
    Column("hotkey", String(64), index=True),
    Column("coldkey", String(64)),
    Column("active", Boolean),
    Column("validator_permit", Boolean),
    Column("alpha_stake", Float),
    Column("tao_stake", Float),
    Column("incentive", Float),
    Column("emission", Float),
    Column("dividends", Float),
    Column("rank", Float),
    Column("trust", Float),
    Column("consensus", Float),
    Column("pruning_score", Float),
    Column("last_update", BigInteger),
    Column("block_at_registration", BigInteger),
    Column("collateral_locked", Float),
    Column("collateral_min", Float),
)

commitments = Table(  # every commitment seen on the subnet each day
    "commitments",
    metadata,
    Column("day", String(10), primary_key=True),
    Column("hotkey", String(64), primary_key=True),
    Column("block", BigInteger),
    Column("data", Text),
    Column("is_binding", Boolean),
    Column("valid", Boolean),
    Column("address", String(42)),
    Column("previous_address", String(42)),
    Column("encrypted", Boolean),
)

bindings = Table(  # the accepted binding per hotkey, as it stands
    "bindings",
    metadata,
    Column("hotkey", String(64), primary_key=True),
    Column("address", String(42), nullable=False),
    Column("accepted_block", BigInteger, nullable=False),
    Column("first_block", BigInteger, nullable=False),
    Column("updated_day", String(10)),
)

binding_events = Table(  # accepted, migrated, refused (with the reason), per day
    "binding_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("day", String(10), nullable=False, index=True),
    Column("hotkey", String(64), nullable=False),
    Column("event", String(16), nullable=False),
    Column("address", String(42)),
    Column("reason", Text),
)

clusters = Table(
    "clusters",
    metadata,
    Column("day", String(10), primary_key=True),
    Column("address", String(42), primary_key=True),
    Column("cluster_id", String(42), nullable=False),
)

cluster_edges = Table(
    "cluster_edges",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("day", String(10), nullable=False, index=True),
    Column("a", String(42), nullable=False),
    Column("b", String(42), nullable=False),
    Column("source", String(16), nullable=False),  # coldkey | subaccount | transfer
)

weights_submitted = Table(
    "weights_submitted",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("submitted_at", DateTime(timezone=True), nullable=False),
    Column("day", String(10)),
    Column("block", BigInteger),
    Column("uids", JSON),
    Column("values", JSON),
    Column("ok", Boolean),
    Column("message", Text),
)

burns = Table(  # what the operator's burn process did
    "burns",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("submitted_at", DateTime(timezone=True), nullable=False),
    Column("network", String(64)),
    Column("netuid", Integer),
    Column("hotkey", String(64)),
    Column("staked_alpha", Float),
    Column("locked_alpha", Float),
    Column("burn_alpha", Float),
    Column("ok", Boolean),
    Column("message", Text),
    Column("dry_run", Boolean),
)

errors = Table(
    "errors",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("day", String(10)),
    Column("hotkey", String(64)),
    Column("address", String(42)),
    Column("stage", String(32)),
    Column("message", Text),
)

meta = Table(
    "meta",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text),
)
