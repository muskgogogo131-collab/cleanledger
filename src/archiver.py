"""Background Hyperliquid >10,000-fill continuous archiver thread.
Snapshots active subscribed wallets every hour into SQLite `hl_archived_fills`
so active/TWAP traders never lose older 2026 history past Hyperliquid's 10k API truncation cliff.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import threading
import time

import payments
import tax_engine

log = logging.getLogger("cleanledger.archiver")
_STARTED = False
TG_SEND = os.path.expanduser("~/bin/tg-send")


def _check_incoming_sales():
    """Watch Polygon USDC/USDC.e transfers >= $25 into our self-custody wallet and notify Telegram."""
    try:
        latest_hex = payments._poly_rpc("eth_blockNumber", [])
        if not latest_hex:
            return
        latest = int(latest_hex, 16)
        from_blk = hex(max(0, latest - 1800))
        recv_topic = "0x" + "0" * 24 + payments.POLYGON_RECV_ADDR.lower().replace("0x", "")
        conn = sqlite3.connect(payments.DB_PATH, timeout=15)
        conn.execute("CREATE TABLE IF NOT EXISTS seen_sales (tx_hash TEXT PRIMARY KEY, ts INTEGER, usdc REAL)")
        for contract in payments.POLYGON_USDC_CONTRACTS:
            logs = payments._poly_rpc("eth_getLogs", [{
                "fromBlock": from_blk, "toBlock": "latest",
                "address": contract,
                "topics": [payments.TRANSFER_TOPIC, None, recv_topic],
            }])
            if not isinstance(logs, list):
                continue
            for lg in logs:
                txh = str(lg.get("transactionHash") or "")
                amt_usdc = int(lg["data"], 16) / 1_000_000.0
                if not txh or amt_usdc < 25.0:
                    continue
                cur = conn.execute("INSERT OR IGNORE INTO seen_sales (tx_hash, ts, usdc) VALUES (?, ?, ?)", (txh, int(time.time()), amt_usdc))
                if cur.rowcount > 0:
                    log.info("NEW SALE RECEIVED: +%.4f USDC (tx=%s)", amt_usdc, txh)
                    if os.path.exists(TG_SEND):
                        subprocess.run([TG_SEND, f"CleanLedger 新成交到账：+{amt_usdc:.2f} USDC（Polygon 链上实收，交易哈希 {txh[:18]}...）"], timeout=15, check=False)
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug("Sales watcher err: %s", e)


def sync_one_wallet(address: str) -> int:
    now_ms = int(time.time() * 1000)
    # Fetch last 30 days incremental fills (2,000/page)
    start_ms = now_ms - 30 * 86400 * 1000
    fills, _ = tax_engine.fetch_hyperliquid_fills(address, start_ms, now_ms, max_pages=6)
    if not fills:
        return 0
    conn = sqlite3.connect(payments.DB_PATH, timeout=15)
    inserted = 0
    for f in fills:
        tid = int(f.get("tid") or 0)
        if not tid:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO hl_archived_fills (address, tid, fill_time, raw_json) VALUES (?, ?, ?, ?)",
            (address.lower(), tid, int(f.get("time") or 0), json.dumps(f, separators=(",", ":"))),
        )
        if cur.rowcount > 0:
            inserted += 1
    total = conn.execute("SELECT COUNT(*) FROM hl_archived_fills WHERE address = ?", (address.lower(),)).fetchone()[0]
    conn.execute(
        "UPDATE archived_wallets SET last_sync_ts = ?, total_fills_archived = ? WHERE address = ?",
        (int(time.time()), total, address.lower()),
    )
    conn.commit()
    conn.close()
    return inserted


def _loop():
    while True:
        _check_incoming_sales()
        try:
            now = int(time.time())
            conn = sqlite3.connect(payments.DB_PATH, timeout=15)
            rows = conn.execute(
                "SELECT address FROM archived_wallets WHERE expires_ts > ? AND last_sync_ts < ?",
                (now, now - 3300),
            ).fetchall()
            conn.close()
            for (addr,) in rows:
                try:
                    n = sync_one_wallet(addr)
                    if n > 0:
                        log.info("Archived %d new Hyperliquid fills for %s", n, addr[:10])
                except Exception as e:
                    log.warning("Archiver sync error for %s: %s", addr[:10], e)
        except Exception as e:
            log.warning("Archiver loop error: %s", e)
        time.sleep(300)


def start_archiver_background():
    global _STARTED
    if _STARTED:
        return
    _STARTED = True
    t = threading.Thread(target=_loop, daemon=True, name="hl-archiver")
    t.start()
