"""`kokoah-subnet`: the project's command line."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from kokoah_subnet import __version__
from kokoah_subnet.chain import NETWORKS, chain_info


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kokoah-subnet", description="Kokoah's Bittensor subnet tools.")
    parser.add_argument("--version", action="version", version=f"kokoah-subnet {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    info = sub.add_parser("info", help="read block height, block mode, subnet count and the registration cost")
    info.add_argument("--network", default="local", help=f"one of {', '.join(NETWORKS)} or a ws:// endpoint")
    info.add_argument("--json", action="store_true", help="print JSON")

    score = sub.add_parser("score-day", help="score one day offline from a builder fills file and miner inputs")
    score.add_argument("--fills", required=True, type=Path, help="builder fills file (.csv or .csv.lz4)")
    score.add_argument("--miners", required=True, type=Path, help="JSON list of miner inputs")
    score.add_argument("--state", type=Path, help="per-miner state JSON (read, then written back)")
    score.add_argument("--price-usd", required=True, type=float, help="the day's mean alpha price in USD")
    score.add_argument("--day-index", required=True, type=int, help="scoring day index (days since the subnet start)")
    score.add_argument(
        "--burn-hotkey", default="burn", help="hotkey that receives a day on which no miner has any weight"
    )
    score.add_argument("--out", type=Path, help="write the day's result JSON here (default: stdout)")

    verify = sub.add_parser("verify", help="run the mechanism's numerical verification")
    verify.set_defaults(command="verify")

    miner = sub.add_parser("miner", help="bind a Hyperliquid address to a hotkey, or show the bindings")
    msub = miner.add_subparsers(dest="miner_command", required=True)
    message = msub.add_parser("message", help="print the exact text the Hyperliquid address must sign")
    message.add_argument("--netuid", required=True, type=int)
    message.add_argument("--hotkey", required=True, help="the hotkey's ss58 address")
    message.add_argument("--address", required=True, help="the Hyperliquid (EVM) address")
    bind = msub.add_parser("bind", help="publish the binding as the hotkey's on-chain commitment")
    bind.add_argument("--netuid", required=True, type=int)
    bind.add_argument("--address", required=True)
    bind.add_argument("--signature", required=True, help="0x-hex EIP-191 signature of the message by the address")
    bind.add_argument("--previous-address", help="on a migration: the address the records come from")
    bind.add_argument("--previous-signature", help="its signature of the same message")
    bind.add_argument("--network", default="local")
    bind.add_argument("--wallet", required=True, help="wallet name")
    bind.add_argument("--wallet-hotkey", default="default")
    bind.add_argument("--wallet-path", default=None)
    show = msub.add_parser("show", help="list the bindings published on a subnet and whether they verify")
    show.add_argument("--netuid", required=True, type=int)
    show.add_argument("--network", default="local")

    agent = sub.add_parser("agent", help="sealed-machine agents: package, submit, bind, sign, run the owner service")
    asub = agent.add_subparsers(dest="agent_command", required=True)
    apack = asub.add_parser("package", help="zip an agent directory (needs entry.py); --encrypt for private code")
    apack.add_argument("directory", type=Path)
    apack.add_argument("--out", type=Path, required=True)
    apack.add_argument("--encrypt", action="store_true")
    asubmit = asub.add_parser("submit", help="submit a bundle to the owner's agent service, signed by the hotkey")
    asubmit.add_argument("--service", required=True)
    asubmit.add_argument("--bundle", type=Path, required=True)
    asubmit.add_argument("--wallet", required=True)
    asubmit.add_argument("--wallet-hotkey", default="default")
    asubmit.add_argument("--wallet-path", default=None)
    abind = asub.add_parser("bind", help="publish the sealed machine's account and code fingerprint as the binding")
    abind.add_argument("--netuid", required=True, type=int)
    abind.add_argument("--endpoint", required=True, help="the machine's public address, http://host:port")
    abind.add_argument("--network", default="local")
    abind.add_argument("--wallet", required=True)
    abind.add_argument("--wallet-hotkey", default="default")
    abind.add_argument("--wallet-path", default=None)
    amsg = asub.add_parser("message", help="print the text the miner's own address signs (withdraw, stop, release)")
    amsg.add_argument("kind", choices=["withdraw", "stop", "release"])
    amsg.add_argument("--account", required=True, help="the agent account address")
    amsg.add_argument("--amount", default="", help="withdraw: USDC amount")
    arel = asub.add_parser(
        "release", help="take the account key out of the service (ends the agent bonus for the account, for good)"
    )
    arel.add_argument("--service", required=True)
    arel.add_argument("--account", required=True)
    arel.add_argument("--time-ms", required=True, type=int, help="from `agent message release`")
    arel.add_argument("--signature", required=True, help="the master address's signature of that message")
    arel_secret = arel.add_mutually_exclusive_group(required=True)
    arel_secret.add_argument(
        "--secret-file", type=Path, help="the file `agent secret` wrote (the secret given at start)"
    )
    arel_secret.add_argument(
        "--master", help="your master address: asks for the password the secret was made from (`agent secret`)"
    )
    arel.add_argument("--out", required=True, type=Path, help="where to write the private key (created, mode 600)")
    asec = asub.add_parser(
        "secret",
        help="make the secret for a sealed machine: from a strong password (checked) or at random (--generate)",
    )
    asec.add_argument("--master", required=True, help="your own Hyperliquid address (the machine's master)")
    asec.add_argument("--out", required=True, type=Path, help="where to write the secret (created, mode 600)")
    asec.add_argument("--generate", action="store_true", help="a random secret instead of one made from a password")
    asec.add_argument("--hotkey", default="", help="your hotkey, so the password check refuses passwords built on it")
    asec.add_argument("--password-stdin", action="store_true", help="read the password from stdin (scripts)")
    aauth = asub.add_parser(
        "authorize", help="sign the start permission for one sealed machine with your hotkey (one per machine start)"
    )
    aauth.add_argument("--netuid", required=True, type=int)
    aauth.add_argument("--master", required=True, help="your own Hyperliquid address (the machine's master)")
    aauth.add_argument("--submission", required=True, help="the submission id the machine runs")
    aauth.add_argument("--out", required=True, type=Path, help="where to write the permission (created)")
    aauth.add_argument("--wallet", required=True)
    aauth.add_argument("--wallet-hotkey", default="default")
    aauth.add_argument("--wallet-path", default=None)
    akey = asub.add_parser("service-key", help="owner: create the agent service's own encryption key (once)")
    akey.add_argument("--out", required=True, type=Path, help="the key file (created, mode 600); keep a copy offline")
    aserv = asub.add_parser("service", help="run the owner's agent service")
    aserv.add_argument("--data-dir", type=Path, required=True)
    aserv.add_argument(
        "--db-url",
        default=os.environ.get("KOKOAH_SUBNET_AGENT_DB_URL", ""),
        help="the service's database (SQLAlchemy URL); default $KOKOAH_SUBNET_AGENT_DB_URL, else SQLite in --data-dir",
    )
    aserv.add_argument("--host", default="127.0.0.1")
    aserv.add_argument("--port", type=int, default=48130)
    aserv.add_argument(
        "--netuid", type=int, required=True, help="accept submissions and keys only from hotkeys registered here"
    )
    aserv.add_argument("--network", default="finney", help="the chain to read registrations from")
    aserv.add_argument(
        "--wrap-key-file",
        type=Path,
        default=None,
        help="the service's own key (`agent service-key`): a second encryption over every stored key copy",
    )
    aserv.add_argument(
        "--dev-accept-any-machine",
        action="store_true",
        help="local development only: accept keys from machines without checking their attestation",
    )
    aserv.add_argument(
        "--image",
        action="append",
        default=[],
        help="also accept this harness image digest (sha256:...), to test a build before its release; repeatable",
    )

    validator = sub.add_parser("validator", help="run the validator loop or serve its published results")
    vsub = validator.add_subparsers(dest="validator_command", required=True)
    vrun = vsub.add_parser("run", help="score completed days and keep the weights set (Ctrl-C to stop)")
    vrun.add_argument("--network", default="local")
    vrun.add_argument("--netuid", required=True, type=int)
    vrun.add_argument("--builder", required=True, help="the builder address whose daily fill file is scored")
    vrun.add_argument(
        "--burn-hotkey",
        required=True,
        help="registered hotkey (own coldkey) that receives a day on which no miner has any weight",
    )
    vrun.add_argument("--data-dir", required=True, type=Path)
    vrun.add_argument(
        "--db-url",
        default=None,
        help="the record (SQLAlchemy URL); default $KOKOAH_SUBNET_DB_URL, else SQLite in --data-dir",
    )
    vrun.add_argument("--wallet", default="validator")
    vrun.add_argument("--wallet-hotkey", default="default")
    vrun.add_argument("--wallet-path", default=None)
    vrun.add_argument("--start-day", default="2026-10-20", help="day index 0, YYYY-MM-DD")
    vrun.add_argument("--hl-info-url", default=None)
    vrun.add_argument("--builder-base-url", default=None)
    vrun.add_argument("--tao-usd-fallback", type=float, default=0.0, help="TAO/USD when no TAO perp is listed")
    vrun.add_argument("--poll-seconds", type=int, default=60)
    vrun.add_argument("--once", action="store_true", help="one pass, then exit")
    vrun.add_argument("--day", default=None, help="score this day instead of yesterday")
    vrun.add_argument(
        "--agent-service",
        default=os.environ.get("KOKOAH_AGENT_SERVICE", ""),
        help="the owner's agent service URL (its released list); without it no account counts as an agent",
    )
    owner = sub.add_parser("owner", help="operator tools")
    osub = owner.add_subparsers(dest="owner_command", required=True)
    ocommit = osub.add_parser("commit-file", help="commit the day's builder file hash from the owner hotkey")
    ocommit.add_argument("--netuid", required=True, type=int)
    ocommit.add_argument("--day", required=True, help="YYYY-MM-DD")
    ocommit.add_argument("--file", type=Path, default=None, help="the builder file (.csv.lz4) as downloaded")
    ocommit.add_argument(
        "--builder", default=None, help="download the day's file for this builder address instead of --file"
    )
    ocommit.add_argument("--network", default="local")
    ocommit.add_argument("--wallet", default="owner")
    ocommit.add_argument("--wallet-hotkey", default="default")
    ocommit.add_argument("--wallet-path", default=None)
    oburn = osub.add_parser("burn", help="burn the alpha that the burn hotkey received (signed by its coldkey)")
    oburn.add_argument("--netuid", required=True, type=int)
    oburn.add_argument("--network", default="local")
    oburn.add_argument("--wallet", default="burn", help="the burn hotkey's wallet (its own coldkey)")
    oburn.add_argument("--wallet-hotkey", default="default")
    oburn.add_argument("--wallet-path", default=None)
    oburn.add_argument("--keep-alpha", type=float, default=0.01, help="alpha left on the hotkey (dust)")
    oburn.add_argument("--min-alpha", type=float, default=1.0, help="do nothing below this amount")
    oburn.add_argument("--password-stdin", action="store_true", help="read the coldkey password from stdin")
    oburn.add_argument("--dry-run", action="store_true", help="show what would be burned, submit nothing")
    oburn.add_argument("--db-url", default=None, help="record the burn in the database (default $KOKOAH_SUBNET_DB_URL)")
    vcheck = vsub.add_parser("check", help="health check over the record (exit 1 on a problem)")
    vcheck.add_argument("--data-dir", type=Path, default=None)
    vcheck.add_argument("--db-url", default=None)
    vcheck.add_argument(
        "--webhook", default=None, help="post the report to this JSON webhook (default $KOKOAH_SUBNET_ALERT_WEBHOOK)"
    )
    vcheck.add_argument("--only-problems", action="store_true", help="post only when there is a problem")
    vskip = vsub.add_parser(
        "skip-day", help="close a day the exchange never published (announced; every validator must do the same)"
    )
    vskip.add_argument("--day", required=True)
    vskip.add_argument("--data-dir", type=Path, default=None)
    vskip.add_argument("--db-url", default=None)
    vskip.add_argument("--burn-hotkey", required=True)
    vskip.add_argument("--reason", default="builder file never published")
    vserve = vsub.add_parser("serve", help="serve the store over HTTP (read-only)")
    vserve.add_argument("--data-dir", required=True, type=Path)
    vserve.add_argument(
        "--db-url",
        default=None,
        help="the record (SQLAlchemy URL); default $KOKOAH_SUBNET_DB_URL, else SQLite in --data-dir",
    )
    vserve.add_argument("--host", default="127.0.0.1")
    vserve.add_argument("--port", type=int, default=48100)
    return parser


def _agent_secret(args: argparse.Namespace) -> int:
    """`agent secret`: the machine secret, from a password that passes the strength check, or at random."""
    from kokoah_subnet.miner_secret import derive_secret, password_problems, random_secret

    if args.generate:
        secret = random_secret()
        how = "random: it exists only in this file, so keep a copy offline"
    else:
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = getpass.getpass("password for the agent's key copy: ")
            if getpass.getpass("the same password again: ") != password:
                print("the two passwords differ")
                return 1
        context = tuple(x for x in (args.hotkey, args.master, args.master.lower().removeprefix("0x")) if x)
        problems = password_problems(password, context)
        if problems:
            print("this password is too weak to protect the account's money:")
            for p in problems:
                print(f"  - {p}")
            return 1
        secret = derive_secret(password, args.master)
        how = "made from your password and your master address: the same password makes it again any time"
    try:
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"{args.out} already exists: choose another --out (an existing secret is never overwritten)")
        return 1
    with os.fdopen(fd, "w") as f:
        f.write(secret.hex())  # no newline: the file is passed as is to the machine's start settings
    print(f"the secret is in {args.out} (mode 600), {how}.")
    print(f"start the machine with --metadata-from-file=tee-env-KOKOAH_AGENT_SECRET={args.out}")
    print("anyone with this secret and the stored copy could take the account: never share it or commit it")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "info":
        result = asyncio.run(chain_info(args.network))
        if args.json:
            print(json.dumps(asdict(result)))
        else:
            print(f"network {result.network}: block {result.block}, fast blocks {result.fast_blocks}")
            print(f"subnets {result.subnet_count}, subnet registration cost {result.subnet_registration_cost_tao}")
        return 0
    if args.command == "score-day":
        from kokoah_subnet.mechanism import DEFAULT
        from kokoah_subnet.validator.builder_feed import load_file
        from kokoah_subnet.validator.run import dump_states, load_miners, load_states, result_json, run_day

        fills = load_file(args.fills)
        states = load_states(args.state)
        miners = load_miners(args.miners)
        day = run_day(args.day_index, args.price_usd, fills, miners, states, DEFAULT)
        text = result_json(day, args.burn_hotkey)
        if args.out:
            args.out.write_text(text)
        else:
            print(text)
        if args.state:
            args.state.write_text(dump_states(day.states))
        return 0
    if args.command == "miner":
        return _miner(args)
    if args.command == "agent":
        return _agent(args)
    if args.command == "validator":
        return _validator(args)
    if args.command == "owner":
        return _owner(args)
    if args.command == "verify":
        import runpy

        runpy.run_path(
            str(Path(__file__).resolve().parents[2] / "scripts" / "verify_mechanism.py"), run_name="__main__"
        )
        return 0
    return 2


def _validator(args: argparse.Namespace) -> int:
    from kokoah_subnet.validator.collect import ValidatorConfig

    if args.validator_command == "run":
        from kokoah_subnet.validator.builder_feed import BASE_URL
        from kokoah_subnet.validator.hyperliquid import MAINNET_INFO
        from kokoah_subnet.validator.loop import run

        cfg = ValidatorConfig(
            network=args.network,
            netuid=args.netuid,
            builder=args.builder,
            data_dir=args.data_dir,
            burn_hotkey=args.burn_hotkey,
            wallet_name=args.wallet,
            wallet_hotkey=args.wallet_hotkey,
            wallet_path=args.wallet_path,
            start_day=args.start_day,
            hl_info_url=args.hl_info_url or MAINNET_INFO,
            builder_base_url=args.builder_base_url or BASE_URL,
            tao_usd_fallback=args.tao_usd_fallback,
            poll_s=args.poll_seconds,
            db_url=args.db_url or os.environ.get("KOKOAH_SUBNET_DB_URL", ""),
            agent_service=args.agent_service,
        )
        asyncio.run(run(cfg, once=args.once, day=args.day))
        return 0
    if args.validator_command == "skip-day":
        from kokoah_subnet.mechanism import DEFAULT
        from kokoah_subnet.validator.collect import day_index
        from kokoah_subnet.validator.skip import skip_day
        from kokoah_subnet.validator.store import Store

        url = args.db_url or os.environ.get("KOKOAH_SUBNET_DB_URL", "")
        if not url and args.data_dir is None:
            print("give --db-url or --data-dir")
            return 2
        store = Store(url or (args.data_dir / "validator.sqlite"))
        cfg = ValidatorConfig(
            network="", netuid=0, builder="", data_dir=args.data_dir or Path("."), burn_hotkey=args.burn_hotkey
        )
        print(skip_day(store, args.day, day_index(cfg, args.day, DEFAULT), args.burn_hotkey, args.reason, DEFAULT))
        return 0
    if args.validator_command == "check":
        from kokoah_subnet.validator.health import main_report
        from kokoah_subnet.validator.store import Store

        url = args.db_url or os.environ.get("KOKOAH_SUBNET_DB_URL", "")
        if not url and args.data_dir is None:
            print("give --db-url or --data-dir")
            return 2
        store = Store(url or (args.data_dir / "validator.sqlite"))
        return main_report(store, args.webhook or os.environ.get("KOKOAH_SUBNET_ALERT_WEBHOOK"), args.only_problems)
    if args.validator_command == "serve":
        import uvicorn

        from kokoah_subnet.validator.serve import create_app
        from kokoah_subnet.validator.store import Store

        url = args.db_url or os.environ.get("KOKOAH_SUBNET_DB_URL", "")
        app = create_app(Store(url or (args.data_dir / "validator.sqlite")))
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0
    return 2


def _owner(args: argparse.Namespace) -> int:
    import bittensor as bt

    from kokoah_subnet.binding import file_commitment_text, publish_text
    from kokoah_subnet.validator.builder_feed import sha256

    if args.owner_command == "commit-file":
        if args.file is not None:
            data = args.file.read_bytes()
        elif args.builder:
            from kokoah_subnet.validator.builder_feed import download

            data = asyncio.run(download(args.builder, args.day))
        else:
            print("give --file or --builder")
            return 2
        digest = sha256(data)
        text = file_commitment_text(args.day, digest)
        wallet = (
            bt.Wallet(name=args.wallet, hotkey=args.wallet_hotkey, path=args.wallet_path)
            if args.wallet_path
            else bt.Wallet(name=args.wallet, hotkey=args.wallet_hotkey)
        )

        async def _publish() -> int:
            async with bt.Subtensor(args.network) as client:
                result = await publish_text(client, wallet, args.netuid, text)
            print(f"{'committed' if result.success else 'failed'}: {text} ({result.message})")
            return 0 if result.success else 1

        return asyncio.run(_publish())
    if args.owner_command == "burn":
        import sys

        from kokoah_subnet.validator.burn import burn_received

        password = sys.stdin.readline().rstrip("\n") if args.password_stdin else None
        wallet = (
            bt.Wallet(name=args.wallet, hotkey=args.wallet_hotkey, path=args.wallet_path)
            if args.wallet_path
            else bt.Wallet(name=args.wallet, hotkey=args.wallet_hotkey)
        )

        from kokoah_subnet.validator.store import Store

        db_url = args.db_url or os.environ.get("KOKOAH_SUBNET_DB_URL", "")
        store = Store(db_url) if db_url else None

        async def _burn() -> int:
            async with bt.Subtensor(args.network) as client:
                report = await burn_received(
                    client,
                    wallet,
                    args.netuid,
                    keep_alpha=args.keep_alpha,
                    min_alpha=args.min_alpha,
                    password=password,
                    dry_run=args.dry_run,
                    store=store,
                    network=args.network,
                )
            print(json.dumps(report, default=str))
            return 0 if report.get("ok", True) else 1

        return asyncio.run(_burn())
    return 2


def _miner(args: argparse.Namespace) -> int:
    import bittensor as bt

    from kokoah_subnet.binding import Binding, binding_message, publish_binding, read_bindings, verify_binding

    if args.miner_command == "message":
        print(binding_message(args.netuid, args.hotkey, args.address))
        return 0
    if args.miner_command == "bind":
        wallet = (
            bt.Wallet(name=args.wallet, hotkey=args.wallet_hotkey, path=args.wallet_path)
            if args.wallet_path
            else bt.Wallet(name=args.wallet, hotkey=args.wallet_hotkey)
        )
        hotkey = wallet.hotkey.ss58_address
        binding = Binding(args.address, args.signature, args.previous_address, args.previous_signature)
        if not verify_binding(args.netuid, hotkey, binding):
            print(f"the signature does not match the message for hotkey {hotkey}; sign exactly:")
            print(binding_message(args.netuid, hotkey, args.address))
            return 1

        async def _publish() -> int:
            async with bt.Subtensor(args.network) as client:
                result = await publish_binding(client, wallet, args.netuid, binding)
            print(f"{'published' if result.success else 'failed'}: {result.message}")
            return 0 if result.success else 1

        return asyncio.run(_publish())
    if args.miner_command == "show":

        async def _show() -> int:
            async with bt.Subtensor(args.network) as client:
                published = await read_bindings(client, args.netuid)
            for p in published:
                status = "valid" if p.valid else "INVALID"
                prev = f" (migrated from {p.binding.previous_address})" if p.binding.previous_address else ""
                print(f"{p.hotkey}  {p.binding.address}  block {p.block}  {status}{prev}")
            if not published:
                print("no bindings")
            return 0

        return asyncio.run(_show())
    return 2


def _wallet(args: argparse.Namespace) -> Any:
    import bittensor as bt

    if args.wallet_path:
        return bt.Wallet(name=args.wallet, hotkey=args.wallet_hotkey, path=args.wallet_path)
    return bt.Wallet(name=args.wallet, hotkey=args.wallet_hotkey)


def _agent(args: argparse.Namespace) -> int:
    import base64
    import time
    from urllib.parse import urlparse

    import httpx

    from kokoah_subnet.agent.bundle import encrypt, new_key, package, sha256_hex

    if args.agent_command == "package":
        plain = package(args.directory)
        data = plain
        if args.encrypt:
            key = new_key()
            data = encrypt(plain, key)
            print(f"bundle key (keep it; the sealed machine needs it, nobody else has it): {key.hex()}")
        args.out.write_bytes(data)
        print(f"code fingerprint (commit this): {sha256_hex(plain)}")
        print(f"submission id: {sha256_hex(data)}  ({len(data)} bytes, {'encrypted' if args.encrypt else 'plain'})")
        return 0
    if args.agent_command == "submit":
        from kokoah_subnet.agent.service import submit_message

        blob = args.bundle.read_bytes()
        wallet = _wallet(args)
        hotkey = wallet.hotkey.ss58_address
        signature = bytes(wallet.hotkey.sign(submit_message(sha256_hex(blob), hotkey).encode())).hex()
        r = httpx.post(
            f"{args.service.rstrip('/')}/agents",
            json={"hotkey": hotkey, "bundle": base64.b64encode(blob).decode(), "signature": signature},
            timeout=120,
        )
        print(r.status_code, r.text)
        return 0 if r.status_code == 200 else 1
    if args.agent_command == "bind":
        import bittensor as bt

        from kokoah_subnet.binding import AgentRef, Binding, publish_binding, verify_binding

        wallet = _wallet(args)
        hotkey = wallet.hotkey.ss58_address
        status = httpx.get(f"{args.endpoint.rstrip('/')}/status", timeout=20).json()
        signed = httpx.get(f"{args.endpoint.rstrip('/')}/binding", timeout=20).json()
        if status["hotkey"] != hotkey or int(status["netuid"]) != args.netuid:
            print(f"the machine is set up for hotkey {status['hotkey']} on netuid {status['netuid']}")
            return 1
        url = urlparse(args.endpoint)
        ref = AgentRef(status["code_sha256"], url.hostname or "", url.port or 80)
        binding = Binding(signed["address"], signed["signature"], agent=ref)
        if not verify_binding(args.netuid, hotkey, binding):
            print("the machine's binding signature does not verify")
            return 1

        async def _publish() -> int:
            async with bt.Subtensor(args.network) as client:
                result = await publish_binding(client, wallet, args.netuid, binding)
            print(f"{'published' if result.success else 'failed'}: {result.message}")
            print(f"agent account {binding.address}, code {ref.code_sha256}, endpoint {ref.url}")
            return 0 if result.success else 1

        return asyncio.run(_publish())
    if args.agent_command == "message":
        from kokoah_subnet.agent.harness import stop_message, withdraw_message
        from kokoah_subnet.agent.keystore import release_message

        t = int(time.time() * 1000)
        if args.kind == "withdraw":
            text = withdraw_message(args.account, args.amount, t)
        elif args.kind == "stop":
            text = stop_message(args.account, t)
        else:
            text = release_message(args.account, t)
        print(json.dumps({"message": text, "time_ms": t}))
        if args.kind == "release":
            print("taking the key out ends the agent bonus for this account for good, and the release is public;")
            print("sign it with your master address (EIP-191), then within 5 minutes run `kokoah-subnet agent release`")
            return 0
        fields = "time_ms, signature, amount_usd" if args.kind == "withdraw" else "time_ms, signature"
        print(f"sign the message with your own address (EIP-191) and POST {{{fields}}} to <endpoint>/{args.kind}")
        print("within 5 minutes")
        return 0
    if args.agent_command == "secret":
        return _agent_secret(args)
    if args.agent_command == "authorize":
        import time

        from kokoah_subnet.agent.keystore import PERMIT_DAYS, start_message

        wallet = _wallet(args)
        hotkey = wallet.hotkey.ss58_address
        t = int(time.time() * 1000)
        message = start_message(args.netuid, hotkey, args.master, args.submission, t)
        permit = f"{t}:{bytes(wallet.hotkey.sign(message.encode())).hex()}"
        try:
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            print(f"{args.out} already exists: a permission is good for one machine start, write a new file")
            return 1
        with os.fdopen(fd, "w") as f:
            f.write(permit)
        print(
            f"start permission for hotkey {hotkey} in {args.out}: good for one machine start within {PERMIT_DAYS} days"
        )
        print(f"start the machine with --metadata-from-file=tee-env-KOKOAH_AGENT_PERMIT={args.out}")
        return 0
    if args.agent_command == "service-key":
        try:
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            print(f"{args.out} already exists: the service key is never replaced (every stored copy depends on it)")
            return 1
        with os.fdopen(fd, "w") as f:
            f.write(os.urandom(32).hex() + "\n")
        print(f"the agent service's key is in {args.out} (mode 600). Keep a copy offline, away from the backups:")
        print("without it no stored key copy can be restored or released, and with the backups it opens one layer.")
        return 0
    if args.agent_command == "release":
        from kokoah_subnet.agent.keystore import KeyService, decrypt_key

        if args.secret_file is not None:
            secret = bytes.fromhex(args.secret_file.read_text().strip())
        else:
            from kokoah_subnet.miner_secret import derive_secret

            secret = derive_secret(getpass.getpass("password the secret was made from: "), args.master)
        blob = KeyService(args.service).release(args.account, args.time_ms, args.signature)
        key = decrypt_key(blob, args.account, secret)
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("0x" + key.hex() + "\n")
        print(f"the private key of {args.account.lower()} is in {args.out} (mode 600); the agent bonus has ended")
        return 0
    if args.agent_command == "service":
        import uvicorn

        from kokoah_subnet.agent.images import APPROVED_HARNESS_IMAGES
        from kokoah_subnet.agent.service import create_app

        if args.dev_accept_any_machine:
            print("development mode: keys are accepted from any machine (never use this in production)")
        from kokoah_subnet.chain import RegisteredHotkeys, registered_hotkeys

        network, netuid = args.network, args.netuid
        registered = RegisteredHotkeys(lambda: registered_hotkeys(network, netuid))
        print(f"submissions and keys only from hotkeys registered on subnet {netuid} ({network})")
        wrap_key = None
        if args.wrap_key_file is not None:
            wrap_key = bytes.fromhex(args.wrap_key_file.read_text().strip())
            print("stored key copies get the service's own encryption too")
        else:
            print("no --wrap-key-file: stored key copies have only the miners' encryption")
        app = create_app(
            args.data_dir,
            args.db_url,
            images=[*APPROVED_HARNESS_IMAGES, *args.image],
            is_registered=registered.check,
            wrap_key=wrap_key,
            netuid=netuid,
            accept_unverified_machines=args.dev_accept_any_machine,
        )
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
