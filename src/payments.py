"""Zero-KYC self-custody on-chain crypto payment verifier (Polygon USDC / USDC.e + Solana SOL).
0% third-party gateway fees. Verifies exact unique order amount suffix directly via public RPCs.
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import time
import uuid
import requests

BASE_DIR = os.path.expanduser("~/Projects/cleanledger")
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "cleanledger.db")
os.makedirs(DATA_DIR, exist_ok=True)

# Self-custody receiving wallets (read-only public addresses; private keys NEVER touched by web app!)
POLYGON_RECV_ADDR = "0x7Bf69AEC1587cf654aA0DCd111b0051032E24bC0"
SOLANA_RECV_ADDR = "3JxjPPSZgG9baNcMcR76CNXWJsgqp3JPq8iyLwXfTz7Y"

# Official Polygon USDC (native) & USDC.e (bridged) contracts (6 decimals)
POLYGON_USDC_CONTRACTS = [
    "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
]
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

POLYGON_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
]
SOLANA_RPCS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
]

PLANS = {
    "tax_bundle": {"name": "2026 Full Tax Export Bundle (Koinly + Awaken + IRS 8949)", "usd": 29.0},
    "hl_archiver_monthly": {"name": "Hyperliquid >10k Continuous Archiver (30 Days)", "usd": 9.0},
    "hl_archiver_yearly": {"name": "Hyperliquid >10k Continuous Archiver (1 Year)", "usd": 49.0},
}

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CleanLedger-Pay/1.0"})


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            created_ts INTEGER NOT NULL,
            expires_ts INTEGER NOT NULL,
            wallet_target TEXT NOT NULL,
            tax_year INTEGER,
            platforms TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            chain TEXT NOT NULL,
            expected_amount REAL NOT NULL,
            recv_address TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            paid_tx TEXT,
            paid_ts INTEGER,
            download_token TEXT
        );
        CREATE TABLE IF NOT EXISTS archived_wallets (
            address TEXT PRIMARY KEY,
            added_ts INTEGER NOT NULL,
            expires_ts INTEGER NOT NULL,
            last_sync_ts INTEGER DEFAULT 0,
            total_fills_archived INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS hl_archived_fills (
            address TEXT NOT NULL,
            tid INTEGER NOT NULL,
            fill_time INTEGER NOT NULL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (address, tid)
        );
    """)
    conn.commit()
    conn.close()


def get_sol_price_usd() -> float:
    try:
        r = SESSION.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "solana", "vs_currencies": "usd"},
            timeout=6,
        )
        if r.status_code == 200:
            p = float(r.json().get("solana", {}).get("usd") or 0)
            if p > 10.0:
                return p
    except Exception:
        pass
    try:
        r = SESSION.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "SOLUSDT"}, timeout=6)
        if r.status_code == 200:
            return float(r.json()["price"])
    except Exception:
        pass
    return 70.0


def create_order(wallet_target: str, tax_year: int | None, platforms: str, plan_id: str, chain: str) -> dict:
    init_db()
    if plan_id not in PLANS:
        raise ValueError("Invalid plan")
    base_usd = PLANS[plan_id]["usd"]
    # Unique 4-decimal micro-suffix for collision-free on-chain order matching
    suffix = random.randint(11, 989)
    now = int(time.time())
    oid = uuid.uuid4().hex[:14]

    if chain == "polygon_usdc":
        expected = round(base_usd + suffix / 10000.0, 4)
        recv = POLYGON_RECV_ADDR
        asset = "USDC (Polygon)"
    elif chain == "solana_sol":
        sol_px = get_sol_price_usd()
        base_sol = base_usd / sol_px
        expected = round(base_sol + suffix / 1000000.0, 6)
        recv = SOLANA_RECV_ADDR
        asset = "SOL (Solana)"
    else:
        raise ValueError("Unsupported chain")

    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute(
        """INSERT INTO orders
           (order_id, created_ts, expires_ts, wallet_target, tax_year, platforms, plan_id, chain, expected_amount, recv_address, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')""",
        (oid, now, now + 3600, wallet_target.strip().lower(), tax_year, platforms, plan_id, chain, expected, recv),
    )
    conn.commit()
    conn.close()
    return {
        "order_id": oid,
        "plan": PLANS[plan_id]["name"],
        "chain": chain,
        "asset": asset,
        "expected_amount": expected,
        "recv_address": recv,
        "expires_in_sec": 3600,
    }


def _poly_rpc(method: str, params: list):
    for url in POLYGON_RPCS:
        try:
            r = SESSION.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=8)
            j = r.json()
            if "result" in j:
                return j["result"]
        except Exception:
            continue
    return None


def _check_polygon_usdc_paid(expected_usdc: float, created_ts: int) -> str | None:
    latest_hex = _poly_rpc("eth_blockNumber", [])
    if not latest_hex:
        return None
    latest = int(latest_hex, 16)
    # Polygon ~2s/block; scan last 2200 blocks (~73 mins)
    from_blk = hex(max(0, latest - 2200))
    recv_topic = "0x" + "0" * 24 + POLYGON_RECV_ADDR.lower().replace("0x", "")
    target_raw = int(round(expected_usdc * 1_000_000))

    for contract in POLYGON_USDC_CONTRACTS:
        logs = _poly_rpc("eth_getLogs", [{
            "fromBlock": from_blk,
            "toBlock": "latest",
            "address": contract,
            "topics": [TRANSFER_TOPIC, None, recv_topic],
        }])
        if not isinstance(logs, list):
            continue
        for lg in reversed(logs):
            try:
                amt = int(lg["data"], 16)
                if abs(amt - target_raw) <= 50:  # within $0.00005 tolerance
                    return str(lg.get("transactionHash") or "")
            except Exception:
                continue
    return None


def _sol_rpc(method: str, params: list):
    for url in SOLANA_RPCS:
        try:
            r = SESSION.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=8)
            j = r.json()
            if "result" in j:
                return j["result"]
        except Exception:
            continue
    return None


def _check_solana_sol_paid(expected_sol: float, created_ts: int) -> str | None:
    sigs = _sol_rpc("getSignaturesForAddress", [SOLANA_RECV_ADDR, {"limit": 15}])
    if not isinstance(sigs, list):
        return None
    target_lamports = int(round(expected_sol * 1_000_000_000))
    for s in sigs:
        if s.get("err") is not None:
            continue
        bt = int(s.get("blockTime") or 0)
        if bt and bt < created_ts - 60:
            continue
        sig = s.get("signature")
        tx = _sol_rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        if not tx or not tx.get("meta"):
            continue
        keys = tx["transaction"]["message"].get("accountKeys", [])
        idx = None
        for i, k in enumerate(keys):
            pk = k.get("pubkey") if isinstance(k, dict) else k
            if pk == SOLANA_RECV_ADDR:
                idx = i
                break
        if idx is None:
            continue
        pre = tx["meta"]["preBalances"][idx]
        post = tx["meta"]["postBalances"][idx]
        diff = post - pre
        if abs(diff - target_lamports) <= 5000:  # within 0.000005 SOL
            return sig
    return None


def check_order_status(order_id: str) -> dict:
    init_db()
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    if not row:
        conn.close()
        return {"error": "Order not found"}
    if row["status"] == "PAID":
        res = {"status": "PAID", "paid_tx": row["paid_tx"], "download_token": row["download_token"], "plan_id": row["plan_id"]}
        conn.close()
        return res

    now = int(time.time())
    if now > row["expires_ts"]:
        conn.execute("UPDATE orders SET status = 'EXPIRED' WHERE order_id = ?", (order_id,))
        conn.commit()
        conn.close()
        return {"status": "EXPIRED"}

    tx_hash = None
    if row["chain"] == "polygon_usdc":
        tx_hash = _check_polygon_usdc_paid(row["expected_amount"], row["created_ts"])
    elif row["chain"] == "solana_sol":
        tx_hash = _check_solana_sol_paid(row["expected_amount"], row["created_ts"])

    if tx_hash:
        token = uuid.uuid4().hex + uuid.uuid4().hex[:16]
        conn.execute(
            "UPDATE orders SET status = 'PAID', paid_tx = ?, paid_ts = ?, download_token = ? WHERE order_id = ?",
            (tx_hash, now, token, order_id),
        )
        # If archiver plan, enroll wallet automatically
        if row["plan_id"].startswith("hl_archiver"):
            dur = 365 * 86400 if "yearly" in row["plan_id"] else 30 * 86400
            conn.execute(
                """INSERT INTO archived_wallets (address, added_ts, expires_ts)
                   VALUES (?, ?, ?)
                   ON CONFLICT(address) DO UPDATE SET expires_ts = MAX(expires_ts, excluded.expires_ts)""",
                (row["wallet_target"], now, now + dur),
            )
        conn.commit()
        conn.close()
        return {"status": "PAID", "paid_tx": tx_hash, "download_token": token, "plan_id": row["plan_id"]}

    conn.close()
    return {"status": "PENDING", "expected_amount": row["expected_amount"], "recv_address": row["recv_address"]}


def verify_download_token(token: str) -> dict | None:
    init_db()
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM orders WHERE download_token = ? AND status = 'PAID'", (token,)).fetchone()
    conn.close()
    return dict(row) if row else None
