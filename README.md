# CleanLedger — Polymarket Data API v2 & Hyperliquid (+ HIP-3) Tax Cleaner

**Stop overpaying crypto taxes because Koinly/CoinTracker misread Polymarket CTF redemptions and Hyperliquid's 10,000-fill truncation cliff.**

### What CleanLedger Fixes (Open-Core MIT Audit CLI)
1. **Polymarket Data API v2 Native** — Old v1 scripts (`data-api.polymarket.com/activity`) now return `410 Gone`. CleanLedger paginates `/v2/activity` cleanly.
2. **True FIFO Cost-Basis Pairing on Polygon CTF** — Pairs `BUY` lots against `SELL`, winning `REDEEM`, losing `REDEEM` (`usdc_size=0`), `SPLIT`, and `MERGE`. Prevents Koinly from treating winning redemptions as `$0`-cost ordinary income or ignoring losing burns.
3. **2026-10-29 Incident Double-Tax Guard** — Flags anomalous forced sells & official reimbursements around the Oct-29 DebugBear frontend incident so you don't pay tax twice.
4. **Hyperliquid + HIP-3 & 10,000-Fill Cliff Detection** — Separates `closedPnl`, trading `fee`, `userFunding`, TWAP fills, and HIP-3 dex contracts (`xyz:`, `flx:`, etc.), and alerts when your wallet approaches Hyperliquid's 10,000-fill API history cutoff.

### Quickstart (100% Read-Only — Zero Private Keys)
```bash
pip install requests
python src/cli.py 0xYourPublicWalletAddress --year 2026
```

### Exports Included in Full Web Bundle (`$29` flat — Polygon USDC / Solana SOL, No Volume Tax)
- `01_Koinly_Universal_Import.csv`
- `02_IRS_Form_8949_ScheduleD.csv`
- `03_Awaken_Tax_Import.csv`
- `04_Audit_Summary.json`
