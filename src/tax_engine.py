"""Core Polymarket Data API V2 + Hyperliquid (+ HIP-3) fetcher and FIFO tax reconciliation engine.
100% read-only public API calls — NEVER requests or handles private keys.
"""
from __future__ import annotations

import csv
import datetime
import io
import json
import math
from collections import defaultdict, deque
from typing import Any
import requests

POLY_DATA_V2 = "https://data-api.polymarket.com/v2"
HL_INFO_URL = "https://api.hyperliquid.xyz/info"

# 2026-10-29 Polymarket DebugBear supply-chain incident window (UTC)
OCT29_HACK_START = int(datetime.datetime(2026, 10, 28, 12, 0, tzinfo=datetime.timezone.utc).timestamp())
OCT29_HACK_END = int(datetime.datetime(2026, 11, 2, 0, 0, tzinfo=datetime.timezone.utc).timestamp())

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CleanLedger/1.0 (+https://github.com/muskgogogo131-collab/cleanledger)"})


def _ts_to_iso(ts: int | float) -> str:
    if ts > 1e12:
        ts = ts / 1000.0
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _ts_to_date(ts: int | float) -> str:
    if ts > 1e12:
        ts = ts / 1000.0
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d")


def _year_bounds_sec(tax_year: int | None) -> tuple[int, int]:
    if not tax_year:
        return 0, int(datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc).timestamp())
    start = int(datetime.datetime(tax_year, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp())
    end = int(datetime.datetime(tax_year + 1, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp()) - 1
    return start, end


# ---------------------------------------------------------------------------
# 1. POLYMARKET DATA API V2 FETCHER & CTF FIFO ENGINE
# ---------------------------------------------------------------------------

def fetch_polymarket_activity(address: str, max_pages: int = 30) -> list[dict]:
    addr = address.strip().lower()
    all_rows: list[dict] = []
    cursor = None
    offset = 0
    for _ in range(max_pages):
        params: dict[str, Any] = {"user": addr, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        elif offset > 0:
            params["offset"] = offset
        r = SESSION.get(f"{POLY_DATA_V2}/activity", params=params, timeout=12)
        if r.status_code != 200:
            break
        payload = r.json()
        batch = payload.get("data", []) if isinstance(payload, dict) else []
        if not batch:
            break
        all_rows.extend(batch)
        pag = payload.get("pagination", {}) if isinstance(payload, dict) else {}
        if not pag.get("has_more"):
            break
        if pag.get("next_cursor"):
            cursor = pag["next_cursor"]
        else:
            offset += len(batch)
    # Sort chronologically ascending for FIFO basis pairing
    all_rows.sort(key=lambda x: (int(x.get("timestamp") or 0), str(x.get("transaction_hash") or "")))
    return all_rows


def reconcile_polymarket(address: str, tax_year: int | None = 2026) -> dict:
    raw = fetch_polymarket_activity(address)
    yr_start, yr_end = _year_bounds_sec(tax_year)

    # FIFO lot queues per token_id: deque of [qty_remaining, unit_cost_usd, acquired_ts, label]
    lots: dict[str, deque] = defaultdict(deque)
    # Track condition_id -> list of token_ids for fallback pairing on REDEEM if token_id empty
    cid_tokens: dict[str, set[str]] = defaultdict(set)

    disposals: list[dict] = []
    incomes: list[dict] = []
    koinly_rows: list[dict] = []
    oct29_flags: list[dict] = []

    naive_koinly_overreported_usd = 0.0
    naive_missed_losses_usd = 0.0
    redeem_win_count = 0
    redeem_loss_count = 0
    merge_count = 0
    split_count = 0
    trade_count = 0

    for ev in raw:
        ts = int(ev.get("timestamp") or 0)
        etype = str(ev.get("type") or "").upper()
        side = str(ev.get("side") or "").upper()
        sz = float(ev.get("size") or 0.0)
        usdc = float(ev.get("usdc_size") or 0.0)
        px = float(ev.get("price") or (usdc / sz if sz > 0 else 0.0))
        tid = str(ev.get("token_id") or "")
        cid = str(ev.get("condition_id") or "")
        title = str(ev.get("title") or cid[:16] or "Polymarket Contract")
        outcome = str(ev.get("outcome") or "")
        txh = str(ev.get("transaction_hash") or "")
        in_year = (yr_start <= ts <= yr_end)
        label = f"{title[:70]} [{outcome}]" if outcome else title[:75]

        if cid and tid:
            cid_tokens[cid].add(tid)

        # Detect Oct-29 DebugBear hack anomalous forced sell or reimbursement
        if OCT29_HACK_START <= ts <= OCT29_HACK_END:
            if etype in ("TRADE", "REWARD", "REDEEM") and (usdc >= 50.0 or (etype == "TRADE" and side == "SELL")):
                oct29_flags.append({
                    "ts": _ts_to_iso(ts), "type": etype, "side": side,
                    "usdc": round(usdc, 2), "title": label, "tx_hash": txh,
                })

        if etype in ("REWARD", "TAKER_REBATE", "YIELD"):
            if in_year and usdc > 0:
                incomes.append({
                    "platform": "Polymarket",
                    "timestamp": _ts_to_iso(ts),
                    "date": _ts_to_date(ts),
                    "income_type": etype,
                    "amount_usd": round(usdc, 6),
                    "asset": "USDC",
                    "tx_hash": txh,
                    "notes": f"Polymarket {etype}",
                })
                koinly_rows.append({
                    "Date": _ts_to_iso(ts),
                    "Sent Amount": "", "Sent Currency": "",
                    "Received Amount": f"{usdc:.6f}", "Received Currency": "USDC",
                    "Fee Amount": "", "Fee Currency": "",
                    "Net Worth Amount": f"{usdc:.2f}", "Net Worth Currency": "USD",
                    "Label": "reward",
                    "Description": f"Polymarket {etype}",
                    "TxHash": txh,
                })
            continue

        if etype == "SPLIT":
            split_count += 1
            # Splitting $usdc creates sz YES + sz NO at $0.50 basis each (allocated cleanly)
            if tid and sz > 0:
                lots[tid].append([sz, (usdc / sz) if usdc > 0 else 0.5, ts, label])
            continue

        if etype == "TRADE" and side == "BUY":
            trade_count += 1
            unit_cost = (usdc / sz) if sz > 0 else px
            if tid and sz > 0:
                lots[tid].append([sz, unit_cost, ts, label])
            if in_year and sz > 0:
                koinly_rows.append({
                    "Date": _ts_to_iso(ts),
                    "Sent Amount": f"{usdc:.6f}", "Sent Currency": "USDC",
                    "Received Amount": f"{sz:.6f}", "Received Currency": f"PM-{tid[-8:] if tid else 'CTF'}",
                    "Fee Amount": "", "Fee Currency": "",
                    "Net Worth Amount": f"{usdc:.2f}", "Net Worth Currency": "USD",
                    "Label": "",
                    "Description": f"Polymarket BUY {label}",
                    "TxHash": txh,
                })
            continue

        if (etype == "TRADE" and side == "SELL") or etype in ("REDEEM", "MERGE"):
            if etype == "TRADE":
                trade_count += 1
            elif etype == "MERGE":
                merge_count += 1
            elif etype == "REDEEM":
                if usdc > 0.0001:
                    redeem_win_count += 1
                else:
                    redeem_loss_count += 1

            # Determine target token queue(s)
            q_tid = tid
            if not q_tid and cid and cid_tokens.get(cid):
                # Pair against any open lot under the same condition_id
                for cand_tid in cid_tokens[cid]:
                    if lots[cand_tid]:
                        q_tid = cand_tid
                        break

            qty_to_match = sz if sz > 0 else (usdc if usdc > 0 else 0.0)
            cost_basis = 0.0
            earliest_acq_ts = ts
            matched_qty = 0.0
            q = lots[q_tid] if q_tid else deque()
            rem = qty_to_match
            while rem > 1e-8 and q:
                lot = q[0]
                take = min(rem, lot[0])
                cost_basis += take * lot[1]
                earliest_acq_ts = min(earliest_acq_ts, lot[2])
                matched_qty += take
                lot[0] -= take
                rem -= take
                if lot[0] <= 1e-8:
                    q.popleft()

            proceeds = usdc
            gain = proceeds - cost_basis
            hold_days = max(0, int((ts - earliest_acq_ts) / 86400))
            term = "Long-Term" if hold_days > 365 else "Short-Term"

            if in_year:
                # Track exact Koinly error magnitude on REDEEM events!
                if etype == "REDEEM":
                    if proceeds > 0.0001:
                        # Koinly treats entire proceeds as 0-cost income -> overreports by cost_basis!
                        naive_koinly_overreported_usd += cost_basis
                    elif cost_basis > 0.0001:
                        # Koinly ignores $0 losing redeem -> misses capital loss of cost_basis!
                        naive_missed_losses_usd += cost_basis

                disposals.append({
                    "platform": "Polymarket",
                    "event_type": etype,
                    "description": label,
                    "date_acquired": _ts_to_date(earliest_acq_ts),
                    "date_sold": _ts_to_date(ts),
                    "timestamp": _ts_to_iso(ts),
                    "quantity": round(qty_to_match, 6),
                    "proceeds_usd": round(proceeds, 4),
                    "cost_basis_usd": round(cost_basis, 4),
                    "gain_loss_usd": round(gain, 4),
                    "term": term,
                    "unmatched_qty": round(rem, 6),
                    "tx_hash": txh,
                })
                koinly_rows.append({
                    "Date": _ts_to_iso(ts),
                    "Sent Amount": f"{qty_to_match:.6f}", "Sent Currency": f"PM-{q_tid[-8:] if q_tid else 'CTF'}",
                    "Received Amount": f"{proceeds:.6f}" if proceeds > 0 else "0.000001", "Received Currency": "USDC",
                    "Fee Amount": "", "Fee Currency": "",
                    "Net Worth Amount": f"{proceeds:.2f}", "Net Worth Currency": "USD",
                    "Label": "realized gain" if etype == "REDEEM" and proceeds == 0 else "",
                    "Description": f"Polymarket {etype} {label} (Basis ${cost_basis:.2f}, PnL ${gain:+.2f})",
                    "TxHash": txh,
                })

    realized_gains = sum(d["gain_loss_usd"] for d in disposals if d["gain_loss_usd"] > 0)
    realized_losses = sum(d["gain_loss_usd"] for d in disposals if d["gain_loss_usd"] < 0)
    net_capital_pnl = realized_gains + realized_losses
    total_rewards = sum(i["amount_usd"] for i in incomes)
    total_volume = sum(float(ev.get("usdc_size") or 0.0) for ev in raw if yr_start <= int(ev.get("timestamp") or 0) <= yr_end)

    return {
        "platform": "Polymarket",
        "address": address,
        "tax_year": tax_year,
        "raw_events_total": len(raw),
        "trade_count": trade_count,
        "redeem_win_count": redeem_win_count,
        "redeem_loss_count": redeem_loss_count,
        "merge_count": merge_count,
        "split_count": split_count,
        "volume_usd": round(total_volume, 2),
        "realized_gains_usd": round(realized_gains, 2),
        "realized_losses_usd": round(realized_losses, 2),
        "net_capital_pnl_usd": round(net_capital_pnl, 2),
        "ordinary_income_usd": round(total_rewards, 2),
        "koinly_overreported_income_usd": round(naive_koinly_overreported_usd, 2),
        "koinly_missed_losses_usd": round(naive_missed_losses_usd, 2),
        "total_tax_base_saved_usd": round(naive_koinly_overreported_usd + naive_missed_losses_usd, 2),
        "oct29_hack_flags_count": len(oct29_flags),
        "oct29_hack_flags": oct29_flags[:10],
        "disposals": disposals,
        "incomes": incomes,
        "koinly_rows": koinly_rows,
    }


# ---------------------------------------------------------------------------
# 2. HYPERLIQUID (+ HIP-3) FETCHER & RECONCILIATION ENGINE
# ---------------------------------------------------------------------------

def fetch_hyperliquid_fills(address: str, start_ms: int, end_ms: int, max_pages: int = 8) -> tuple[list[dict], bool]:
    """Paginate userFillsByTime (2,000 fills/page). Returns (fills, hit_10k_cliff)."""
    addr = address.strip()
    all_fills: list[dict] = []
    seen_tids: set[int] = set()
    cur_start = start_ms
    for _ in range(max_pages):
        r = SESSION.post(
            HL_INFO_URL,
            json={"type": "userFillsByTime", "user": addr, "startTime": cur_start, "endTime": end_ms, "aggregateByTime": False},
            timeout=15,
        )
        if r.status_code != 200:
            break
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        added = 0
        max_t = cur_start
        for f in batch:
            tid = int(f.get("tid") or 0)
            if tid and tid in seen_tids:
                continue
            if tid:
                seen_tids.add(tid)
            all_fills.append(f)
            added += 1
            t = int(f.get("time") or 0)
            if t > max_t:
                max_t = t
        if len(batch) < 2000 or added == 0 or max_t >= end_ms:
            break
        cur_start = max_t + 1
    hit_10k = len(all_fills) >= 9800
    all_fills.sort(key=lambda x: int(x.get("time") or 0))
    return all_fills, hit_10k


def fetch_hyperliquid_funding(address: str, start_ms: int, end_ms: int) -> list[dict]:
    try:
        r = SESSION.post(
            HL_INFO_URL,
            json={"type": "userFunding", "user": address.strip(), "startTime": start_ms, "endTime": end_ms},
            timeout=15,
        )
        return r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
    except Exception:
        return []


def fetch_hyperliquid_ledger(address: str, start_ms: int, end_ms: int) -> list[dict]:
    try:
        r = SESSION.post(
            HL_INFO_URL,
            json={"type": "userNonFundingLedgerUpdates", "user": address.strip(), "startTime": start_ms, "endTime": end_ms},
            timeout=15,
        )
        return r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
    except Exception:
        return []


def reconcile_hyperliquid(address: str, tax_year: int | None = 2026) -> dict:
    yr_start, yr_end = _year_bounds_sec(tax_year)
    start_ms = yr_start * 1000
    end_ms = yr_end * 1000

    fills, hit_10k = fetch_hyperliquid_fills(address, start_ms, end_ms)
    fundings = fetch_hyperliquid_funding(address, start_ms, end_ms)
    ledgers = fetch_hyperliquid_ledger(address, start_ms, end_ms)

    disposals: list[dict] = []
    incomes: list[dict] = []
    koinly_rows: list[dict] = []

    hip3_fills = 0
    twap_fills = 0
    total_closed_pnl = 0.0
    total_fees = 0.0
    total_funding = 0.0
    total_volume = 0.0

    for f in fills:
        coin = str(f.get("coin") or "")
        px = float(f.get("px") or 0.0)
        sz = float(f.get("sz") or 0.0)
        closed_pnl = float(f.get("closedPnl") or 0.0)
        fee = float(f.get("fee") or 0.0)
        fee_tok = str(f.get("feeToken") or "USDC")
        ts_ms = int(f.get("time") or 0)
        txh = str(f.get("hash") or "")
        direction = str(f.get("dir") or f.get("side") or "")
        is_hip3 = ":" in coin
        if is_hip3:
            hip3_fills += 1
        if f.get("twapId") is not None:
            twap_fills += 1

        notional = px * sz
        total_volume += notional
        total_closed_pnl += closed_pnl
        total_fees += fee

        if abs(closed_pnl) > 1e-8 or abs(fee) > 1e-8:
            net_pnl = closed_pnl - fee
            disposals.append({
                "platform": "Hyperliquid-HIP3" if is_hip3 else "Hyperliquid",
                "event_type": direction,
                "description": f"{coin} {direction} ({sz:.4f} @ ${px:.4f})",
                "date_acquired": _ts_to_date(ts_ms),
                "date_sold": _ts_to_date(ts_ms),
                "timestamp": _ts_to_iso(ts_ms),
                "quantity": round(sz, 6),
                "proceeds_usd": round(max(0.0, net_pnl), 4),
                "cost_basis_usd": round(max(0.0, -net_pnl), 4),
                "gain_loss_usd": round(net_pnl, 4),
                "fee_usd": round(fee, 6),
                "term": "Short-Term",
                "tx_hash": txh,
            })
            # Koinly Futures PnL standard format
            if net_pnl >= 0:
                koinly_rows.append({
                    "Date": _ts_to_iso(ts_ms),
                    "Sent Amount": "", "Sent Currency": "",
                    "Received Amount": f"{net_pnl:.6f}", "Received Currency": "USDC",
                    "Fee Amount": f"{max(0.0, fee):.6f}" if fee > 0 else "", "Fee Currency": fee_tok if fee > 0 else "",
                    "Net Worth Amount": f"{net_pnl:.2f}", "Net Worth Currency": "USD",
                    "Label": "realized gain",
                    "Description": f"Hyperliquid {coin} {direction}",
                    "TxHash": txh,
                })
            else:
                koinly_rows.append({
                    "Date": _ts_to_iso(ts_ms),
                    "Sent Amount": f"{abs(net_pnl):.6f}", "Sent Currency": "USDC",
                    "Received Amount": "", "Received Currency": "",
                    "Fee Amount": f"{max(0.0, fee):.6f}" if fee > 0 else "", "Fee Currency": fee_tok if fee > 0 else "",
                    "Net Worth Amount": f"{abs(net_pnl):.2f}", "Net Worth Currency": "USD",
                    "Label": "realized gain",
                    "Description": f"Hyperliquid {coin} {direction}",
                    "TxHash": txh,
                })

    for fd in fundings:
        ts_ms = int(fd.get("time") or 0)
        txh = str(fd.get("hash") or "")
        delta = fd.get("delta") or {}
        usdc = float(delta.get("usdc") or 0.0)
        coin = str(delta.get("coin") or "")
        total_funding += usdc
        if abs(usdc) > 1e-8:
            incomes.append({
                "platform": "Hyperliquid",
                "timestamp": _ts_to_iso(ts_ms),
                "date": _ts_to_date(ts_ms),
                "income_type": "FUNDING_RECEIVED" if usdc > 0 else "FUNDING_PAID",
                "amount_usd": round(usdc, 6),
                "asset": "USDC",
                "tx_hash": txh,
                "notes": f"Hyperliquid {coin} perp funding",
            })
            koinly_rows.append({
                "Date": _ts_to_iso(ts_ms),
                "Sent Amount": f"{abs(usdc):.6f}" if usdc < 0 else "", "Sent Currency": "USDC" if usdc < 0 else "",
                "Received Amount": f"{usdc:.6f}" if usdc > 0 else "", "Received Currency": "USDC" if usdc > 0 else "",
                "Fee Amount": "", "Fee Currency": "",
                "Net Worth Amount": f"{abs(usdc):.2f}", "Net Worth Currency": "USD",
                "Label": "realized gain",
                "Description": f"Hyperliquid {coin} funding",
                "TxHash": txh,
            })

    # Detect potential silent truncation if first deposit predates earliest returned fill by >3 days & fill count > 5000
    earliest_fill_ms = int(fills[0]["time"]) if fills else 0
    earliest_ledger_ms = min((int(x.get("time") or 0) for x in ledgers), default=0)
    truncation_warning = hit_10k or (len(fills) >= 8000 and earliest_ledger_ms > 0 and earliest_fill_ms - earliest_ledger_ms > 86400 * 3 * 1000)

    return {
        "platform": "Hyperliquid",
        "address": address,
        "tax_year": tax_year,
        "fills_count": len(fills),
        "hip3_fills_count": hip3_fills,
        "twap_fills_count": twap_fills,
        "funding_events_count": len(fundings),
        "ledger_events_count": len(ledgers),
        "hit_10k_cliff": hit_10k,
        "truncation_warning": truncation_warning,
        "volume_usd": round(total_volume, 2),
        "closed_pnl_usd": round(total_closed_pnl, 2),
        "fees_usd": round(total_fees, 2),
        "net_funding_usd": round(total_funding, 2),
        "net_total_pnl_usd": round(total_closed_pnl - total_fees + total_funding, 2),
        "disposals": disposals,
        "incomes": incomes,
        "koinly_rows": koinly_rows,
    }


# ---------------------------------------------------------------------------
# 3. CSV EXPORTERS (KOINLY UNIVERSAL, AWAKEN TAX, IRS FORM 8949)
# ---------------------------------------------------------------------------

def build_koinly_csv(rows: list[dict]) -> str:
    cols = [
        "Date", "Sent Amount", "Sent Currency", "Received Amount", "Received Currency",
        "Fee Amount", "Fee Currency", "Net Worth Amount", "Net Worth Currency",
        "Label", "Description", "TxHash",
    ]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in cols})
    return buf.getvalue()


def build_form_8949_csv(disposals: list[dict]) -> str:
    cols = [
        "Description of Property", "Date Acquired", "Date Sold or Disposed",
        "Proceeds (USD)", "Cost or Other Basis (USD)", "Gain or (Loss) (USD)",
        "Term", "Platform", "Event Type", "TxHash",
    ]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for d in disposals:
        w.writerow({
            "Description of Property": f"{d['quantity']} {d['description']}",
            "Date Acquired": d["date_acquired"],
            "Date Sold or Disposed": d["date_sold"],
            "Proceeds (USD)": f"{d['proceeds_usd']:.2f}",
            "Cost or Other Basis (USD)": f"{d['cost_basis_usd']:.2f}",
            "Gain or (Loss) (USD)": f"{d['gain_loss_usd']:.2f}",
            "Term": d["term"],
            "Platform": d["platform"],
            "Event Type": d["event_type"],
            "TxHash": d.get("tx_hash", ""),
        })
    return buf.getvalue()


def build_awaken_csv(disposals: list[dict], incomes: list[dict]) -> str:
    cols = ["Date", "Asset", "Amount", "Fiat Value (USD)", "Tag", "Platform", "Notes", "TxHash"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for d in disposals:
        w.writerow({
            "Date": d["timestamp"],
            "Asset": "USDC",
            "Amount": f"{d['gain_loss_usd']:.6f}",
            "Fiat Value (USD)": f"{d['gain_loss_usd']:.2f}",
            "Tag": "realized_pnl",
            "Platform": d["platform"],
            "Notes": d["description"],
            "TxHash": d.get("tx_hash", ""),
        })
    for inc in incomes:
        w.writerow({
            "Date": inc["timestamp"],
            "Asset": inc["asset"],
            "Amount": f"{inc['amount_usd']:.6f}",
            "Fiat Value (USD)": f"{inc['amount_usd']:.2f}",
            "Tag": inc["income_type"].lower(),
            "Platform": inc["platform"],
            "Notes": inc["notes"],
            "TxHash": inc.get("tx_hash", ""),
        })
    return buf.getvalue()
