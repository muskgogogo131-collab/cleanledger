"""CleanLedger Web Server (FastAPI + crisp zero-dependency dark fintech UI + zero-KYC USDC/SOL checkout)."""
from __future__ import annotations

import io
import json
import os
import re
import zipfile
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

import tax_engine
import payments
import archiver

app = FastAPI(title="CleanLedger", version="1.0.0")
payments.init_db()
archiver.start_archiver_background()

ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


class AuditReq(BaseModel):
    address: str
    tax_year: int | None = 2026
    platform: str = "both"


class OrderReq(BaseModel):
    address: str
    tax_year: int | None = 2026
    platform: str = "both"
    plan_id: str = "tax_bundle"
    chain: str = "polygon_usdc"


@app.get("/health")
def health():
    return {"ok": True, "service": "cleanledger"}


@app.post("/api/audit")
def api_audit(req: AuditReq):
    addr = req.address.strip()
    if not ADDR_RE.match(addr):
        raise HTTPException(400, "Please enter a valid 0x EVM wallet address.")
    yr = req.tax_year if (req.tax_year and req.tax_year > 0) else None
    result = {"address": addr, "tax_year": yr or "ALL"}
    preview_rows = []
    if req.platform in ("both", "polymarket"):
        pm = tax_engine.reconcile_polymarket(addr, tax_year=yr)
        preview_rows.extend(pm["disposals"][:4])
        result["polymarket"] = {k: v for k, v in pm.items() if k not in ("disposals", "incomes", "koinly_rows")}
    if req.platform in ("both", "hyperliquid"):
        hl = tax_engine.reconcile_hyperliquid(addr, tax_year=yr)
        preview_rows.extend(hl["disposals"][:4])
        result["hyperliquid"] = {k: v for k, v in hl.items() if k not in ("disposals", "incomes", "koinly_rows")}
    result["preview_disposals"] = preview_rows[:6]
    return result


@app.post("/api/order")
def api_create_order(req: OrderReq):
    addr = req.address.strip()
    if not ADDR_RE.match(addr):
        raise HTTPException(400, "Invalid wallet address.")
    try:
        return payments.create_order(addr, req.tax_year, req.platform, req.plan_id, req.chain)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/order/{order_id}")
def api_check_order(order_id: str):
    return payments.check_order_status(order_id)


@app.get("/api/download")
def api_download(token: str = Query(...)):
    order = payments.verify_download_token(token)
    if not order:
        raise HTTPException(403, "Invalid or unpaid download token.")
    addr = order["wallet_target"]
    yr = order["tax_year"] if (order["tax_year"] and order["tax_year"] > 0) else None
    plat = order["platforms"]

    disposals, incomes, koinly_rows = [], [], []
    summary = {"address": addr, "tax_year": yr or "ALL", "order_id": order["order_id"], "paid_tx": order["paid_tx"]}

    if plat in ("both", "polymarket"):
        pm = tax_engine.reconcile_polymarket(addr, tax_year=yr)
        disposals.extend(pm["disposals"])
        incomes.extend(pm["incomes"])
        koinly_rows.extend(pm["koinly_rows"])
        summary["polymarket"] = {k: v for k, v in pm.items() if k not in ("disposals", "incomes", "koinly_rows")}
    if plat in ("both", "hyperliquid"):
        hl = tax_engine.reconcile_hyperliquid(addr, tax_year=yr)
        disposals.extend(hl["disposals"])
        incomes.extend(hl["incomes"])
        koinly_rows.extend(hl["koinly_rows"])
        summary["hyperliquid"] = {k: v for k, v in hl.items() if k not in ("disposals", "incomes", "koinly_rows")}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("01_Koinly_Universal_Import.csv", tax_engine.build_koinly_csv(koinly_rows))
        zf.writestr("02_IRS_Form_8949_ScheduleD.csv", tax_engine.build_form_8949_csv(disposals))
        zf.writestr("03_Awaken_Tax_Import.csv", tax_engine.build_awaken_csv(disposals, incomes))
        zf.writestr("04_Audit_Summary.json", json.dumps(summary, indent=2, ensure_ascii=False))
    buf.seek(0)
    fname = f"CleanLedger_{addr[:8]}_{yr or 'ALL'}.zip"
    return StreamingResponse(buf, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{fname}"'})


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CleanLedger — Polymarket V2 & Hyperliquid Tax Cleaner</title>
<style>
  :root{--bg:#0b0f17;--card:#131a26;--border:#233044;--text:#e6edf7;--muted:#8fa3bf;--accent:#2dd4bf;--warn:#f59e0b;--danger:#ef4444;}
  *{box-sizing:border-box} body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;background:var(--bg);color:var(--text);line-height:1.55}
  .wrap{max-width:940px;margin:0 auto;padding:42px 20px 80px}
  .badge{display:inline-block;padding:4px 12px;border-radius:999px;background:rgba(45,212,191,.12);color:var(--accent);font-size:12px;font-weight:600;border:1px solid rgba(45,212,191,.3)}
  h1{font-size:36px;line-height:1.18;margin:14px 0 10px;letter-spacing:-.02em}
  .sub{color:var(--muted);font-size:16px;max-width:720px;margin:0 0 28px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:22px;margin-bottom:18px}
  .row{display:flex;gap:10px;flex-wrap:wrap}
  input,select{background:#0d131e;border:1px solid var(--border);color:var(--text);padding:12px 14px;border-radius:10px;font-size:15px}
  input{flex:1;min-width:260px}
  button{background:var(--accent);color:#06231f;border:none;padding:12px 20px;border-radius:10px;font-weight:700;font-size:15px;cursor:pointer}
  button.sec{background:#1b2537;color:var(--text);border:1px solid var(--border)}
  button:disabled{opacity:.5;cursor:not-allowed}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:16px}
  .stat{background:#0e1522;border:1px solid var(--border);border-radius:10px;padding:14px}
  .stat .k{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
  .stat .v{font-size:22px;font-weight:700;margin-top:4px}
  .alert{padding:14px 16px;border-radius:10px;margin-top:14px;font-size:14px}
  .alert.warn{background:rgba(245,158,11,.12);border:1px solid rgba(245,158,11,.4);color:#fde68a}
  .alert.ok{background:rgba(45,212,191,.1);border:1px solid rgba(45,212,191,.35);color:#99f6e4}
  table{width:100%;border-collapse:collapse;margin-top:14px;font-size:13px}
  th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--border)}
  th{color:var(--muted);font-weight:600}
  .paybox{background:#0c1420;border:1px solid rgba(45,212,191,.4);border-radius:12px;padding:18px;margin-top:16px}
  code{background:#080d15;padding:3px 7px;border-radius:6px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#7dd3fc;word-break:break-all}
  .hide{display:none}
</style>
</head>
<body>
<div class="wrap">
  <span class="badge">Read-Only &bull; Zero Private Keys &bull; Zero KYC &bull; Flat $29 USDC/SOL</span>
  <h1>Stop Overpaying Taxes on Polymarket &amp; Hyperliquid.</h1>
  <p class="sub">Generic tax tools treat Polymarket CTF redemptions as 0-cost income, double-tax the Oct-29 incident reimbursement, and break on Hyperliquid's 10,000-fill API cliff &amp; HIP-3 perps. Paste any public 0x address for an instant free audit.</p>

  <div class="card">
    <div class="row">
      <input id="addr" placeholder="Paste public 0x wallet / Polymarket proxy address..." />
      <select id="plat">
        <option value="both">Polymarket + Hyperliquid</option>
        <option value="polymarket">Polymarket Only</option>
        <option value="hyperliquid">Hyperliquid + HIP-3 Only</option>
      </select>
      <select id="yr">
        <option value="2026">Tax Year 2026</option>
        <option value="2025">Tax Year 2025</option>
        <option value="0">All Time</option>
      </select>
      <button id="runBtn" onclick="runAudit()">Run Free Audit</button>
    </div>
    <p style="font-size:12px;color:var(--muted);margin:10px 0 0">100% read-only. Uses official Polymarket Data API v2 &amp; Hyperliquid Info API. Never asks for wallet signature or seed phrase.</p>
  </div>

  <div id="resCard" class="card hide"></div>

  <div id="payCard" class="card hide">
    <h3 style="margin-top:0">Export Ready: Koinly Universal + Awaken + IRS Form 8949 (.zip)</h3>
    <p style="color:var(--muted);font-size:14px">No volume tax (unlike Realize.tax $59-$999/yr). Pay once in Polygon USDC or Solana SOL, unlock instant download in &lt;10 seconds.</p>
    <div class="row">
      <button onclick="startOrder('tax_bundle','polygon_usdc')">Unlock Full ZIP — $29 Polygon USDC</button>
      <button class="sec" onclick="startOrder('tax_bundle','solana_sol')">Unlock Full ZIP — $29 Solana SOL</button>
      <button class="sec" onclick="startOrder('hl_archiver_yearly','polygon_usdc')">HL &gt;10k Auto-Archiver — $49/yr USDC</button>
    </div>
    <div id="orderBox" class="paybox hide"></div>
  </div>
</div>
<script>
let lastReq = null, pollTimer = null;
async function runAudit(){
  const btn = document.getElementById('runBtn');
  const address = document.getElementById('addr').value.trim();
  const platform = document.getElementById('plat').value;
  const tax_year = parseInt(document.getElementById('yr').value, 10);
  if(!/^0x[a-fA-F0-9]{40}$/.test(address)){ alert('Enter a valid 0x address'); return; }
  btn.disabled = true; btn.textContent = 'Auditing on-chain...';
  lastReq = {address, platform, tax_year};
  try{
    const r = await fetch('/api/audit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(lastReq)});
    const d = await r.json();
    if(!r.ok) throw new Error(d.detail || 'Audit failed');
    renderAudit(d);
  }catch(e){ alert(e.message); }
  finally{ btn.disabled = false; btn.textContent = 'Run Free Audit'; }
}
function renderAudit(d){
  const el = document.getElementById('resCard');
  let html = '<h3 style="margin-top:0">Free Diagnostic Report ('+d.tax_year+')</h3>';
  if(d.polymarket){
    const p = d.polymarket;
    html += '<h4 style="margin:14px 0 6px;color:var(--accent)">Polymarket Data API v2</h4><div class="grid">'
      + stat('Events Scanned', p.raw_events_total)
      + stat('True Net Capital PnL', '$'+(p.net_capital_pnl_usd>=0?'+':'')+p.net_capital_pnl_usd.toFixed(2))
      + stat('Official Rewards', '$'+p.ordinary_income_usd.toFixed(2))
      + stat('Koinly Over-Tax Fixed', '$'+p.total_tax_base_saved_usd.toFixed(2)) + '</div>';
    if(p.total_tax_base_saved_usd > 0) html += '<div class="alert warn">Detected '+p.redeem_win_count+' winning &amp; '+p.redeem_loss_count+' losing CTF redemptions. Naive CSV imports overstate your taxable base by <b>$'+p.total_tax_base_saved_usd.toFixed(2)+'</b>.</div>';
    if(p.oct29_hack_flags_count > 0) html += '<div class="alert warn">Detected '+p.oct29_hack_flags_count+' event(s) during the Oct-29 DebugBear incident window — flagged for double-tax protection.</div>';
  }
  if(d.hyperliquid){
    const h = d.hyperliquid;
    html += '<h4 style="margin:18px 0 6px;color:var(--accent)">Hyperliquid + HIP-3</h4><div class="grid">'
      + stat('Fills Scanned', h.fills_count + ' (HIP-3: '+h.hip3_fills_count+')')
      + stat('Closed PnL', '$'+(h.closed_pnl_usd>=0?'+':'')+h.closed_pnl_usd.toFixed(2))
      + stat('Net Funding', '$'+(h.net_funding_usd>=0?'+':'')+h.net_funding_usd.toFixed(2))
      + stat('True Net PnL', '$'+(h.net_total_pnl_usd>=0?'+':'')+h.net_total_pnl_usd.toFixed(2)) + '</div>';
    if(h.truncation_warning) html += '<div class="alert warn">10,000-Fill API Truncation Risk Detected! Enable continuous hourly archiving so older 2026 fills are never lost.</div>';
  }
  if(d.preview_disposals && d.preview_disposals.length){
    html += '<h4 style="margin:18px 0 6px">Sample Reconciled Disposals (Preview)</h4><table><tr><th>Platform</th><th>Date</th><th>Contract</th><th>Proceeds</th><th>Basis</th><th>Net PnL</th></tr>';
    for(const row of d.preview_disposals){
      html += '<tr><td>'+row.platform+'</td><td>'+row.date_sold+'</td><td>'+row.description.slice(0,48)+'</td><td>$'+row.proceeds_usd.toFixed(2)+'</td><td>$'+row.cost_basis_usd.toFixed(2)+'</td><td>$'+(row.gain_loss_usd>=0?'+':'')+row.gain_loss_usd.toFixed(2)+'</td></tr>';
    }
    html += '</table>';
  }
  el.innerHTML = html; el.classList.remove('hide');
  document.getElementById('payCard').classList.remove('hide');
}
function stat(k,v){ return '<div class="stat"><div class="k">'+k+'</div><div class="v">'+v+'</div></div>'; }
async function startOrder(plan_id, chain){
  if(!lastReq) return;
  const r = await fetch('/api/order',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...lastReq, plan_id, chain})});
  const o = await r.json();
  if(!r.ok){ alert(o.detail||'Order failed'); return; }
  const box = document.getElementById('orderBox');
  box.classList.remove('hide');
  box.innerHTML = '<div><b>Send EXACT amount:</b> <code>'+o.expected_amount+' '+o.asset+'</code></div>'
    + '<div style="margin-top:8px"><b>To address:</b> <code>'+o.recv_address+'</code></div>'
    + '<div id="payStat" style="margin-top:10px;color:var(--warn)">Waiting for on-chain confirmation (auto-checking every 5s)...</div>';
  if(pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async ()=>{
    const sr = await fetch('/api/order/'+o.order_id);
    const sd = await sr.json();
    if(sd.status === 'PAID'){
      clearInterval(pollTimer);
      document.getElementById('payStat').innerHTML = '<span style="color:var(--accent)"><b>Payment Confirmed!</b> Tx: <code>'+sd.paid_tx.slice(0,18)+'...</code></span><br><br><a href="/api/download?token='+sd.download_token+'"><button>Download CleanLedger Tax ZIP</button></a>';
    }
  }, 5000);
}
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML
