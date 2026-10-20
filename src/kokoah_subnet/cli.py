"""`kokoah-subnet`: the project's command line."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

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
    score.add_argument("--owner-hotkey", default="owner", help="hotkey that receives the burn weight")
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

    validator = sub.add_parser("validator", help="run the validator loop or serve its published results")
    vsub = validator.add_subparsers(dest="validator_command", required=True)
    vrun = vsub.add_parser("run", help="score completed days and keep the weights set (Ctrl-C to stop)")
    vrun.add_argument("--network", default="local")
    vrun.add_argument("--netuid", required=True, type=int)
    vrun.add_argument("--builder", required=True, help="the builder address whose daily fill file is scored")
    vrun.add_argument("--data-dir", required=True, type=Path)
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
    vserve = vsub.add_parser("serve", help="serve the store over HTTP (read-only)")
    vserve.add_argument("--data-dir", required=True, type=Path)
    vserve.add_argument("--host", default="127.0.0.1")
    vserve.add_argument("--port", type=int, default=48100)
    return parser


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
        text = result_json(day, args.owner_hotkey)
        if args.out:
            args.out.write_text(text)
        else:
            print(text)
        if args.state:
            args.state.write_text(dump_states(day.states))
        return 0
    if args.command == "miner":
        return _miner(args)
    if args.command == "validator":
        return _validator(args)
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
            wallet_name=args.wallet,
            wallet_hotkey=args.wallet_hotkey,
            wallet_path=args.wallet_path,
            start_day=args.start_day,
            hl_info_url=args.hl_info_url or MAINNET_INFO,
            builder_base_url=args.builder_base_url or BASE_URL,
            tao_usd_fallback=args.tao_usd_fallback,
            poll_s=args.poll_seconds,
        )
        asyncio.run(run(cfg, once=args.once, day=args.day))
        return 0
    if args.validator_command == "serve":
        import uvicorn

        from kokoah_subnet.validator.serve import create_app
        from kokoah_subnet.validator.store import Store

        app = create_app(Store(args.data_dir / "validator.sqlite"))
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0
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


if __name__ == "__main__":
    raise SystemExit(main())
