"""An example agent: reads the market and its account through the harness, and shows how to place an order.

The agent runs with no network at all. Everything goes through the harness's Unix socket (KOKOAH_AGENT_SOCKET):
  POST /info {"request": {...}}           read-only exchange data (account queries answer for this account)
  POST /call {"method": ..., "params": ...} order, cancel, cancel_by_cloid, update_leverage,
                                            update_isolated_margin, schedule_cancel
Orders: {"coin": "BTC", "is_buy": true, "sz": 0.001, "limit_px": 100000,
         "order_type": {"limit": {"tif": "Gtc"}}, "reduce_only": false}
Only the Python standard library is used here; vendor any package you need inside your bundle.
"""

import http.client
import json
import os
import socket
import time
from typing import Any

SOCKET = os.environ["KOKOAH_AGENT_SOCKET"]


class _Unix(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(SOCKET)


def harness(path: str, body: dict[str, Any]) -> dict[str, Any]:
    conn = _Unix("harness")
    conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
    answer: dict[str, Any] = json.loads(conn.getresponse().read())
    return answer


def main() -> None:
    while True:
        mids = harness("/info", {"request": {"type": "allMids"}})
        state = harness("/info", {"request": {"type": "clearinghouseState"}})
        if mids.get("ok") and state.get("ok"):
            value = state["result"].get("marginSummary", {}).get("accountValue")
            print(f"BTC mid {mids['result'].get('BTC')}, account value {value}", flush=True)
        # A real strategy decides here, for example (a price has at most 5 significant figures: whole dollars for BTC):
        # harness("/call", {"method": "order", "params": {"orders": [{"coin": "BTC", "is_buy": True,
        #     "sz": 0.001, "limit_px": round(float(mids["result"]["BTC"]) * 0.99),
        #     "order_type": {"limit": {"tif": "Alo"}}}]}})
        time.sleep(60)


if __name__ == "__main__":
    main()
