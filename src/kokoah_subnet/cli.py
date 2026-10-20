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
    if args.command == "verify":
        import runpy

        runpy.run_path(
            str(Path(__file__).resolve().parents[2] / "scripts" / "verify_mechanism.py"), run_name="__main__"
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
