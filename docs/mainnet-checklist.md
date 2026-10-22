# Mainnet go-live checklist

Every item here is a decision or a transaction of the owner. Nothing in this list is executed without the owner's
go-ahead for that specific step; the commands are given so that each step is one copy-paste when the time comes.
Costs are read live (`btcli query subnet-registration-cost --network finney`) and were τ482.5 on 2026-10-19.

## Before the first mainnet transaction

- [ ] Legal review of paying tokens for trading activity, in the operator's jurisdictions.
- [ ] Owner coldkey custody decided (hardware or multisig; an operations proxy for day-to-day owner calls).
- [ ] The real burn wallet created on its own coldkey, its password known to the operator's burn process host.
- [ ] Validator hosts decided (this server, the node servers, or rented machines), each with the record database.
- [x] The builder fee switched on in the application (0.025%, live since 2026-10-20); the subnet pays nothing while it is zero.
- [ ] The test-network period's measurements published: a month of burns directed vs executed, the holding effect,
      real-trader replays, total-fee matching.
- [ ] The whitepaper's parameters frozen for launch (specification version noted in every artifact).

## Registration day

1. Fund the owner coldkey with the registration cost plus stake for the validator and burn registrations.
2. `btcli tx register-subnet -w <owner> --network finney` (note the netuid).
3. `btcli tx start-call --netuid <N> -w <owner> --network finney`.
4. Hyperparameters from the owner coldkey: `owner_cut_auto_lock_enabled true`; `commit_reveal_weights_enabled` as
   wanted (on is fine; the validator handles both paths); `weights_rate_limit` left at the default;
   `min_collateral` as decided.
5. Register the burn hotkey: `btcli tx burned-register --netuid <N> -w <burn wallet> --network finney`.
6. Register and stake the validator hotkey(s): `btcli tx burned-register --netuid <N> -w <wallet> -H <hotkey>` and
   `btcli tx add-stake --netuid <N> --amount-tao <x> -w <wallet> -H <hotkey>`.
7. Root enabling emission is the network's step (`subnet_emission_enabled`); until then the subnet runs without TAO
   emission.

## Operations from day one

- `scripts/db.sh start`, then the systemd units from `deploy/systemd/` with `--network finney --netuid <N>
  --burn-hotkey <burn hotkey ss58> --builder <builder address>` in each validator's env file.
- `kokoah-subnet-operator.timer` (daily file-hash commitment) and `kokoah-subnet-burn` (burn loop) with the mainnet
  values; `kokoah-subnet-check.timer` with the alert webhook set.
- Daily backups (`kokoah-subnet-backup.timer`), Grafana on the record (`scripts/dashboard.sh start`).
- Public leaderboard, if wanted: `deploy/nginx/kokoah-subnet.conf` in front of the interface.
- Announce the subnet with the whitepaper, the binding instructions (`docs/app-binding.md`) and the interface URL.

## First week

- Watch `kokoah-subnet validator check`, the dashboard and the burns table; reconcile alpha directed to the burn
  hotkey with `TokensBurned` events.
- Confirm two or more independent validators agree on every day's weights (compare `days.builder_file_sha256` and
  the weights in their records).
