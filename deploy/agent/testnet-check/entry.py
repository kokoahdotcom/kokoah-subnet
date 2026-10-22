"""A test agent for the exchange's test network: proves the whole trading path once, then idles.

Once the account holds at least $25 it: reads the BTC mid; places a post-only buy 2% below the mid (it rests and
does not fill); confirms it is open; cancels it by its order id; buys about $12 of BTC at the market (an IOC 1%
through the mid, so it fills and pays the builder fee); sells the same size reduce-only to close; prints the fills
with their builder fees. Every step prints one line starting with CHECK.
"""

import http.client
import json
import math
import os
import socket
import time

SOCKET = os.environ["KOKOAH_AGENT_SOCKET"]


class _Unix(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(SOCKET)


def harness(path: str, body: dict) -> dict:
    conn = _Unix("harness")
    conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
    return json.loads(conn.getresponse().read())


def info(request: dict) -> object:
    out = harness("/info", {"request": request})
    if not out.get("ok"):
        raise RuntimeError(out.get("error"))
    return out["result"]


def call(method: str, params: dict) -> dict:
    out = harness("/call", {"method": method, "params": params})
    print("CHECK", method, json.dumps(out)[:400], flush=True)
    return out


def size_for(usd: float, px: float, decimals: int) -> float:
    step = 10**-decimals
    return math.ceil(usd / px / step) * step


def main() -> None:
    while True:
        state = info({"type": "clearinghouseState"})
        value = float(state.get("marginSummary", {}).get("accountValue", 0) or 0)
        if value >= 25:
            break
        print(f"CHECK waiting for funds: account value {value}", flush=True)
        time.sleep(60)
    meta = info({"type": "meta"})
    decimals = next(int(a.get("szDecimals", 5)) for a in meta["universe"] if a["name"] == "BTC")
    mid = float(info({"type": "allMids"})["BTC"])
    print(f"CHECK BTC mid {mid}, account value {value}", flush=True)

    rest_px = round(mid * 0.98)
    sz = size_for(12, rest_px, decimals)
    while True:  # the harness approves Kokoah's builder fee on the first order, once the account is funded
        placed = call(
            "order",
            {
                "orders": [
                    {
                        "coin": "BTC",
                        "is_buy": True,
                        "sz": sz,
                        "limit_px": rest_px,
                        "order_type": {"limit": {"tif": "Alo"}},
                    }
                ]
            },
        )
        if placed.get("ok"):
            break
        time.sleep(30)
    cloid = placed.get("result", {}).get("cloids", [None])[0]
    open_orders = info({"type": "openOrders"})
    print("CHECK open orders", json.dumps(open_orders)[:300], flush=True)
    call("cancel_by_cloid", {"coin": "BTC", "cloid": cloid})

    buy_px = round(mid * 1.01)
    sz = size_for(12, mid, decimals)
    call(
        "order",
        {
            "orders": [
                {"coin": "BTC", "is_buy": True, "sz": sz, "limit_px": buy_px, "order_type": {"limit": {"tif": "Ioc"}}}
            ]
        },
    )
    time.sleep(3)
    sell_px = round(mid * 0.99)
    call(
        "order",
        {
            "orders": [
                {
                    "coin": "BTC",
                    "is_buy": False,
                    "sz": sz,
                    "limit_px": sell_px,
                    "order_type": {"limit": {"tif": "Ioc"}},
                    "reduce_only": True,
                }
            ]
        },
    )
    time.sleep(3)
    fills = info({"type": "userFills"})
    for f in fills[:4]:
        print(
            "CHECK fill",
            f.get("side"),
            f.get("sz"),
            "@",
            f.get("px"),
            "fee",
            f.get("fee"),
            "builderFee",
            f.get("builderFee"),
            "cloid",
            f.get("cloid"),
            flush=True,
        )
    print("CHECK done", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
