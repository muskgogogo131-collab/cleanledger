"""Background Hyperliquid >10,000-fill continuous archiver thread.
Snapshots active subscribed wallets every hour into SQLite `hl_archived_fills`
so active/TWAP traders never lose older 2026 history past Hyperliquid's 10k API truncation cliff.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time

import payments
import tax_engine

log = logging.getLogger("cleanledger.archiver")
_STARTED = False


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
