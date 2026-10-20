"""Measure what burning miner emission through the owner hotkey costs (specification section 15, item 1).

Run against the localnet: uv run python scripts/measure_burn.py [--netuid 2] [--tempos 6] [--network local]

Every tempo it reads the subnet's `MinerBurned`, the TAO and alpha emission counters, the pool reserves and each
UID's incentive and emission, and prints one row. Change the validator's weights between runs (for example 85%,
50% and 0% to the owner hotkey) and compare the rows: the TAO inflow should scale with `1 − MinerBurned` while
the alpha emission to participants stays the same.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from typing import Any

import bittensor as bt
from bittensor import storage as st

SUB = st.SubtensorModule


def _num(value: Any) -> float:
    """Balances (TAO or alpha) by their raw units; fixed-point numbers by their bits; plain numbers as they are."""
    try:
        return float(value.rao) / 1e9
    except Exception:  # noqa: BLE001 - not a balance
        pass
    if isinstance(value, dict) and "bits" in value:
        return float(value["bits"]) / 2**64
    if isinstance(value, int):
        return value / 1e9  # raw balances come back as rao
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _fixed(value: Any) -> float:
    """A FixedU128 (`MinerBurned`): bits / 2^64 whether it arrives as a dict or an int."""
    if isinstance(value, dict) and "bits" in value:
        return float(value["bits"]) / 2**64
    if isinstance(value, int):
        return value / 2**64
    return _num(value)


async def row(client: Any, netuid: int) -> dict[str, Any]:
    block = int(await client.block())
    burned = _fixed(await client.query(SUB.MinerBurned, [netuid]))
    out = {
        "block": block,
        "miner_burned": burned,
        "tao_in_emission": _num(await client.query(SUB.SubnetTaoInEmission, [netuid])),
        "alpha_out_emission": _num(await client.query(SUB.SubnetAlphaOutEmission, [netuid])),
        "alpha_in_emission": _num(await client.query(SUB.SubnetAlphaInEmission, [netuid])),
        "subnet_tao": _num(await client.query(SUB.SubnetTAO, [netuid])),
        "alpha_out": _num(await client.query(SUB.SubnetAlphaOut, [netuid])),
    }
    mg = await client.subnets.metagraph(netuid, commitments=False)
    if mg is not None:
        out["uids"] = [(n.uid, round(float(n.incentive), 4), _num(n.emission)) for n in mg.neurons]
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", default="local")
    ap.add_argument("--netuid", type=int, default=2)
    ap.add_argument("--tempos", type=int, default=6, help="how many tempos to sample")
    args = ap.parse_args()
    async with bt.Subtensor(args.network) as client:
        tempo = int(await client.query(SUB.Tempo, [args.netuid]))
        print(f"netuid {args.netuid}, tempo {tempo} blocks; sampling {args.tempos} tempos")
        last_step = None
        samples = 0
        while samples < args.tempos:
            block = int(await client.block())
            step = block // tempo
            if step != last_step:
                last_step = step
                r = await row(client, args.netuid)
                samples += 1
                print(
                    f"{time.strftime('%H:%M:%S')} block {r['block']:>6} burned {r['miner_burned']:.4f} "
                    f"tao_in {r['tao_in_emission']:.6f} alpha_out {r['alpha_out_emission']:.6f} "
                    f"alpha_in {r['alpha_in_emission']:.6f} pool_tao {r['subnet_tao']:.4f} "
                    f"alpha_out_total {r['alpha_out']:.3f} uids {r.get('uids')}",
                    flush=True,
                )
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
