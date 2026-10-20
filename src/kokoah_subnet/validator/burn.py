"""The operator's burn process (specification section 6): whatever alpha the burn hotkey received as the unawarded
remainder is destroyed with the chain's `burn_alpha` call, signed by the burn hotkey's own coldkey. Each run is one
transaction that emits a `TokensBurned` event, so every burn is verifiable on chain."""

from __future__ import annotations

from typing import Any

from bittensor import calls

RAO = 10**9


def _alpha(value: Any) -> float:
    try:
        return float(value.rao) / RAO
    except Exception:  # noqa: BLE001 - not a balance
        return float(value) / RAO if isinstance(value, int) else float(value)


async def burn_received(
    client: Any,
    wallet: Any,
    netuid: int,
    keep_alpha: float = 0.01,
    min_alpha: float = 1.0,
    password: str | None = None,
    dry_run: bool = False,
    store: Any = None,
    network: str = "",
) -> dict[str, Any]:
    """Burn the burn hotkey's unlocked stake above `keep_alpha`; do nothing below `min_alpha`."""
    hotkey = wallet.hotkey.ss58_address
    coldkey = wallet.coldkeypub.ss58_address
    staked = _alpha(await client.staking.stake(coldkey, hotkey, netuid))
    locked = 0.0
    mg = await client.subnets.metagraph(netuid, commitments=False)
    if mg is not None:
        for n in mg.neurons:
            if n.hotkey == hotkey and getattr(n, "collateral_locked", None) is not None:
                locked = _alpha(n.collateral_locked)
    amount = staked - locked - keep_alpha
    report: dict[str, Any] = {
        "hotkey": hotkey,
        "staked_alpha": staked,
        "locked_alpha": locked,
        "burn_alpha": max(0.0, amount),
    }
    if amount < min_alpha:
        report.update({"ok": True, "skipped": f"below the minimum of {min_alpha} alpha"})
        return report
    if dry_run:
        report.update({"ok": True, "dry_run": True})
    else:
        signer = wallet.get_coldkey(password) if password is not None else wallet
        call = calls.SubtensorModule.burn_alpha(hotkey=hotkey, amount=int(amount * RAO), netuid=netuid)
        result = await client.submit_call(call, signer, signer="coldkey")
        report.update({"ok": bool(getattr(result, "success", False)), "message": str(getattr(result, "message", ""))})
    if store is not None:
        store.put_burn(
            network=network,
            netuid=netuid,
            hotkey=hotkey,
            staked_alpha=staked,
            locked_alpha=locked,
            burn_alpha=max(0.0, amount),
            ok=bool(report.get("ok")),
            message=str(report.get("message", "")),
            dry_run=dry_run,
        )
    return report
