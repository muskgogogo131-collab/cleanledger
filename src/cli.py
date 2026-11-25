"""CleanLedger CLI — MIT Open-Core Polymarket Data API V2 & Hyperliquid (+ HIP-3) Free Tax Auditor.
Usage:
  python src/cli.py 0xYourWalletAddress [--year 2026] [--platform both|polymarket|hyperliquid]
"""
from __future__ import annotations

import argparse
import json
import sys
import tax_engine


def main():
    p = argparse.ArgumentParser(description="CleanLedger — Free Polymarket V2 & Hyperliquid (+HIP-3) Tax Audit CLI")
    p.add_argument("address", help="Public 0x EVM wallet address (read-only, zero private keys)")
    p.add_argument("--year", type=int, default=2026, help="Tax year (default: 2026; use 0 for all-time)")
    p.add_argument("--platform", choices=["both", "polymarket", "hyperliquid"], default="both")
    p.add_argument("--json", action="store_true", help="Output raw audit JSON")
    args = p.parse_args()

    yr = args.year if args.year > 0 else None
    out = {}

    if args.platform in ("both", "polymarket"):
        pm = tax_engine.reconcile_polymarket(args.address, tax_year=yr)
        out["polymarket"] = {k: v for k, v in pm.items() if k not in ("disposals", "incomes", "koinly_rows")}

    if args.platform in ("both", "hyperliquid"):
        hl = tax_engine.reconcile_hyperliquid(args.address, tax_year=yr)
        out["hyperliquid"] = {k: v for k, v in hl.items() if k not in ("disposals", "incomes", "koinly_rows")}

    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    print(f"\n=== CleanLedger Free Tax Audit ({args.address[:10]}...{args.address[-6:]} | Year: {yr or 'ALL'}) ===\n")
    if "polymarket" in out:
        pm = out["polymarket"]
        print("[Polymarket Data API V2]")
        print(f"  Events scanned : {pm['raw_events_total']} (Trades={pm['trade_count']}, WinRedeems={pm['redeem_win_count']}, LossRedeems={pm['redeem_loss_count']}, Merges={pm['merge_count']})")
        print(f"  Volume         : ${pm['volume_usd']:,.2f}")
        print(f"  True Net PnL   : ${pm['net_capital_pnl_usd']:+,.2f} (Gains ${pm['realized_gains_usd']:,.2f} / Losses ${pm['realized_losses_usd']:,.2f})")
        print(f"  Rewards/Rebates: ${pm['ordinary_income_usd']:,.2f}")
        print(f"  Koinly Over-Tax: +${pm['koinly_overreported_income_usd']:,.2f} phantom 0-basis income + ${pm['koinly_missed_losses_usd']:,.2f} missed losses")
        print(f"  -> Tax Base Fix: ${pm['total_tax_base_saved_usd']:,.2f} overstated tax base prevented!")
        if pm["oct29_hack_flags_count"]:
            print(f"  [!] Oct-29 Incident Flag: {pm['oct29_hack_flags_count']} events in DebugBear incident window (review for double-tax!)")
        print()

    if "hyperliquid" in out:
        hl = out["hyperliquid"]
        print("[Hyperliquid + HIP-3]")
        print(f"  Fills scanned  : {hl['fills_count']} (HIP-3={hl['hip3_fills_count']}, TWAP={hl['twap_fills_count']}, Funding={hl['funding_events_count']})")
        print(f"  Volume         : ${hl['volume_usd']:,.2f}")
        print(f"  Closed PnL     : ${hl['closed_pnl_usd']:+,.2f} | Fees: ${hl['fees_usd']:,.2f} | Funding: ${hl['net_funding_usd']:+,.2f}")
        print(f"  True Net PnL   : ${hl['net_total_pnl_usd']:+,.2f}")
        if hl["truncation_warning"]:
            print("  [!] 10,000-FILL CLIFF WARNING: History near/at Hyperliquid API truncation limit! Enable continuous archiving.")
        print()


if __name__ == "__main__":
    main()
