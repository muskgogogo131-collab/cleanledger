# CleanLedger — Polymarket Data API v2 & Hyperliquid (+ HIP-3) Tax Cleaner

**Live Browser App (100% Client-Side, Zero Private Keys, Free Instant Audit):**
👉 https://muskgogogo131-collab.github.io/cleanledger/

**Stop overpaying crypto taxes because Koinly/CoinTracker misread Polymarket CTF redemptions and Hyperliquid's 10,000-fill truncation cliff.**

### Real-World Example (Verified on Polymarket Data API v2)
On a real active 2026 Polymarket wallet (`$128,805` volume, `+$1,589` true net profit):
- Naive Koinly imports misclassify **86 winning CTF redemptions** as `$0`-basis ordinary income (`+$5,470` phantom income) and miss **533 losing CTF burns** (`$30,702` missed capital losses).
- **Result**: `$36,173.10` in overstated taxable base fixed in 5 seconds.

### What CleanLedger Fixes
1. **Polymarket Data API v2 Native** — Legacy v1 scripts (`data-api.polymarket.com/activity`) now return `410 Gone`. CleanLedger paginates `/v2/activity` cleanly.
2. **True FIFO Cost-Basis Pairing on Polygon CTF** — Pairs `BUY` lots against `SELL`, winning `REDEEM`, losing `REDEEM` (`usdc_size=0`), `SPLIT`, and `MERGE`, and separates `REWARD` / `TAKER_REBATE`.
3. **2026-10-29 Incident Double-Tax Guard** — Flags anomalous forced sells & official reimbursements inside the Oct 28–Nov 2, 2026 DebugBear frontend incident window so you don't pay tax twice.
4. **Hyperliquid + HIP-3 & 10,000-Fill Cliff Detection** — Separates `closedPnl`, trading `fee`, `userFunding`, TWAP fills, and HIP-3 dex contracts (`xyz:`, `flx:`, etc.), and alerts when your wallet hits Hyperliquid's 10,000-fill API cutoff.

### Free CLI Audit (MIT License — 100% Read-Only)
```bash
pip install requests
python src/cli.py 0xYourPublicWalletAddress --year 2026
```

### Full Tax Export Pack (`$29` Flat Polygon USDC — No Volume Tax)
Unlike `Realize.tax` (`$59–$999/yr` volume tax), CleanLedger charges a flat `$29 USDC` (zero KYC, zero account) for unlimited transactions:
- `01_Koinly_Universal_Import.csv`
- `02_IRS_Form_8949_ScheduleD.csv`
- `03_Awaken_Tax_Import.csv`
- `04_Audit_Summary.json`
