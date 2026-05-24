#!/usr/bin/env python3
"""
Stock Dashboard Report Emailer
================================
Reads stock_data.json, detects buy/sell signal changes vs the last run,
emails a formatted HTML summary, and saves an interactive local report.

SETUP:
  1. Create a Gmail App Password:
       myaccount.google.com → Security → 2-Step Verification → App Passwords
  2. Save it:  echo "xxxx xxxx xxxx xxxx" > ~/.stock_gmail_pass
  3. Test:     GMAIL_APP_PASS="$(cat ~/.stock_gmail_pass)" venv/bin/python report_emailer.py
"""

import json
import os
import sys
import smtplib
import ssl
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

GMAIL_USER     = "ianbwhite07@gmail.com"
GMAIL_APP_PASS = os.getenv("GMAIL_APP_PASS", "")
RECIPIENT      = "ianbwhite07@gmail.com"

BUY_THRESHOLD  = 7.0
SELL_THRESHOLD = 4.0

SCRIPT_DIR       = os.path.dirname(os.path.abspath(__file__))
DATA_FILE        = os.path.join(SCRIPT_DIR, "stock_data.json")
SNAPSHOT_FILE    = os.path.join(SCRIPT_DIR, "last_report_snapshot.json")
LOCAL_REPORT_FILE = os.path.join(SCRIPT_DIR, "latest_report.html")


# ─────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────

def load_stock_data():
    if not os.path.exists(DATA_FILE):
        print(f"[Report] ERROR: {DATA_FILE} not found. Run your Flask app first.")
        sys.exit(1)
    with open(DATA_FILE) as f:
        return json.load(f)

def load_snapshot():
    if os.path.exists(SNAPSHOT_FILE):
        with open(SNAPSHOT_FILE) as f:
            return json.load(f)
    return {}

def save_snapshot(stock_data):
    snap = {
        ticker: {
            "score_overall": d.get("score_overall"),
            "score_short":   d.get("score_short"),
            "score_mid":     d.get("score_mid"),
            "score_long":    d.get("score_long"),
            "price":         d.get("price"),
        }
        for ticker, d in stock_data.items()
    }
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(snap, f, indent=2)


# ─────────────────────────────────────────────
# SIGNAL DETECTION
# ─────────────────────────────────────────────

def classify(score):
    if score is None:           return "unknown"
    if score >= BUY_THRESHOLD:  return "buy"
    if score <= SELL_THRESHOLD: return "sell"
    return "hold"

def to_entry(d):
    """Normalize a stock_data dict into a flat entry dict."""
    return {
        "ticker":      d.get("ticker", ""),
        "price":       d.get("price"),
        "change_1d":   d.get("change_1d", 0) or 0,
        "curr_score":  d.get("score_overall"),
        "score_short": d.get("score_short"),
        "score_mid":   d.get("score_mid"),
        "score_long":  d.get("score_long"),
        "rsi":         d.get("rsi"),
        "ma_trend":    d.get("ma_trend"),
        "rating":      d.get("rating"),
        "sentiment":   d.get("sentiment_label", "neutral"),
        "earnings":    d.get("earnings_trend", "N/A"),
        "updated":     d.get("updated", ""),
    }

def detect_changes(current_data, snapshot):
    new_buys, new_sells, buy_weakened, sell_cleared, notable = [], [], [], [], []

    for ticker, data in current_data.items():
        curr_score = data.get("score_overall")
        prev_score = (snapshot.get(ticker) or {}).get("score_overall")
        curr_zone  = classify(curr_score)
        prev_zone  = classify(prev_score) if prev_score is not None else None

        entry = to_entry(data)
        entry["prev_score"] = prev_score

        if prev_zone is None:
            pass
        elif prev_zone != curr_zone:
            if   curr_zone == "buy":                          new_buys.append(entry)
            elif curr_zone == "sell":                         new_sells.append(entry)
            elif curr_zone == "hold" and prev_zone == "buy":  buy_weakened.append(entry)
            elif curr_zone == "hold" and prev_zone == "sell": sell_cleared.append(entry)
        else:
            if curr_score is not None and prev_score is not None:
                delta = curr_score - prev_score
                if abs(delta) >= 1.5:
                    entry["delta"] = delta
                    notable.append(entry)

    return new_buys, new_sells, buy_weakened, sell_cleared, notable

def get_all_buys_sells(current_data):
    buys  = sorted([d for d in current_data.values() if classify(d.get("score_overall")) == "buy"],
                   key=lambda x: x.get("score_overall") or 0, reverse=True)
    sells = sorted([d for d in current_data.values() if classify(d.get("score_overall")) == "sell"],
                   key=lambda x: x.get("score_overall") or 10)
    holds = sorted([d for d in current_data.values() if classify(d.get("score_overall")) == "hold"],
                   key=lambda x: x.get("score_overall") or 0, reverse=True)
    return buys, sells, holds

def get_horizon_buckets(current_data, horizon_key):
    """Return (buys, sells, holds) sorted by a specific score horizon."""
    all_stocks = list(current_data.values())
    buys  = sorted([d for d in all_stocks if classify(d.get(horizon_key)) == "buy"],
                   key=lambda x: x.get(horizon_key) or 0, reverse=True)
    sells = sorted([d for d in all_stocks if classify(d.get(horizon_key)) == "sell"],
                   key=lambda x: x.get(horizon_key) or 10)
    holds = sorted([d for d in all_stocks if classify(d.get(horizon_key)) == "hold"],
                   key=lambda x: x.get(horizon_key) or 0, reverse=True)
    return buys, sells, holds


# ─────────────────────────────────────────────
# SHARED HTML HELPERS
# ─────────────────────────────────────────────

def score_pill(score, prev_score=None, small=False):
    if score is None:
        return '<span style="background:#555;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">N/A</span>'
    if   score >= BUY_THRESHOLD:  bg = "#22c55e"
    elif score <= SELL_THRESHOLD: bg = "#ef4444"
    else:                         bg = "#f59e0b"

    delta_html = ""
    if prev_score is not None and prev_score != score:
        arrow = "▲" if score > prev_score else "▼"
        delta_html = f' <span style="font-size:10px;opacity:0.85">{arrow}{abs(score-prev_score):.1f}</span>'

    fs = "12px" if small else "13px"
    return (f'<span style="background:{bg};color:#fff;padding:2px 9px;'
            f'border-radius:12px;font-size:{fs};font-weight:600;">'
            f'{score:.1f}{delta_html}</span>')

def change_badge(change_1d):
    if change_1d is None: return ""
    color = "#22c55e" if change_1d >= 0 else "#ef4444"
    sign  = "+" if change_1d >= 0 else ""
    return f'<span style="color:{color};font-size:12px;font-weight:600;">{sign}{change_1d:.2f}%</span>'

def ma_color(ma):
    return "#22c55e" if ma == "Uptrend" else "#ef4444" if ma == "Downtrend" else "#888"

def sent_color(s):
    return "#22c55e" if s == "positive" else "#ef4444" if s == "negative" else "#888"


# ─────────────────────────────────────────────
# EMAIL BUILDER
# ─────────────────────────────────────────────

def email_row(d, score_key="curr_score", show_prev=False, highlight=None):
    ticker    = d.get("ticker", "")
    price     = d.get("price")
    chg       = d.get("change_1d", 0)
    score     = d.get(score_key) if score_key != "curr_score" else (d.get("curr_score") or d.get("score_overall"))
    prev      = d.get("prev_score") if show_prev else None
    short_s   = d.get("score_short")
    mid_s     = d.get("score_mid")
    long_s    = d.get("score_long")
    rsi       = d.get("rsi")
    ma        = d.get("ma_trend", "N/A") or "N/A"
    rating    = d.get("rating", "N/A") or "N/A"
    sentiment = d.get("sentiment", "neutral") or "neutral"

    price_str = f"${price:.2f}" if isinstance(price, (int, float)) else "N/A"
    bg_style  = f"background:{highlight};" if highlight else ""

    return f"""
    <tr style="{bg_style}border-bottom:1px solid #2a2a2a;">
      <td style="padding:9px 12px;font-weight:700;font-size:14px;color:#e2e8f0;">{ticker}</td>
      <td style="padding:9px 12px;color:#cbd5e1;">{price_str} {change_badge(chg)}</td>
      <td style="padding:9px 12px;">{score_pill(score, prev)}</td>
      <td style="padding:9px 8px;font-size:12px;color:#94a3b8;">{short_s:.0f} / {mid_s:.0f} / {long_s:.0f}</td>
      <td style="padding:9px 8px;font-size:12px;color:{ma_color(ma)};">{ma}</td>
      <td style="padding:9px 8px;font-size:12px;color:#94a3b8;">{f'{rsi:.0f}' if rsi else 'N/A'}</td>
      <td style="padding:9px 8px;font-size:12px;color:#94a3b8;">{rating}</td>
      <td style="padding:9px 8px;font-size:12px;color:{sent_color(sentiment)};">{sentiment.title()}</td>
    </tr>"""

def email_section(title, color):
    return f"""
    <tr>
      <td colspan="8" style="padding:12px 12px 5px;background:{color};
          font-size:12px;font-weight:700;letter-spacing:0.06em;color:#f1f5f9;text-transform:uppercase;">
        {title}
      </td>
    </tr>"""

TH = "padding:7px 12px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;color:#64748b;white-space:nowrap;"

TABLE_HEADER = f"""
<tr style="border-bottom:2px solid #334155;">
  <th style="{TH}">Ticker</th><th style="{TH}">Price</th><th style="{TH}">Score</th>
  <th style="{TH}">S / M / L</th><th style="{TH}">MA</th><th style="{TH}">RSI</th>
  <th style="{TH}">Analyst</th><th style="{TH}">Sentiment</th>
</tr>"""

def horizon_email_block(current_data, horizon_key, label, emoji):
    """Email rows for a single time horizon (short/mid/long)."""
    buys, sells, _ = get_horizon_buckets(current_data, horizon_key)
    rows = ""
    if buys:
        rows += email_section(f"{emoji} {label} Buys — score ≥7 ({len(buys)})", "#0f2d1f")
        for d in buys:
            e = to_entry(d)
            e["curr_score"] = d.get(horizon_key)
            rows += email_row(e)
    if sells:
        rows += email_section(f"🔴 {label} Sells — score ≤4 ({len(sells)})", "#2d0f0f")
        for d in sells:
            e = to_entry(d)
            e["curr_score"] = d.get(horizon_key)
            rows += email_row(e)
    if not buys and not sells:
        rows += f"""
        <tr><td colspan="8" style="padding:10px 12px;color:#64748b;font-size:12px;">
          No {label.lower()} buy or sell signals right now.
        </td></tr>"""
    return rows

def build_html_email(current_data, new_buys, new_sells, buy_weakened,
                     sell_cleared, notable, all_buys, all_sells, all_holds,
                     is_morning, data_updated):
    period    = "🌅 Morning" if is_morning else "🌙 Evening"
    now_str   = datetime.now().strftime("%B %d, %Y — %I:%M %p")
    total_chg = len(new_buys) + len(new_sells) + len(buy_weakened) + len(sell_cleared)

    summary_html = "".join(f"""
      <td style="text-align:center;padding:0 20px;">
        <div style="font-size:26px;font-weight:800;color:#f1f5f9;">{icon} {count}</div>
        <div style="font-size:11px;color:#94a3b8;margin-top:2px;text-transform:uppercase;letter-spacing:0.06em;">{label}</div>
      </td>""" for icon, count, label in [
        ("🟢", len(all_buys),  "Active Buys"),
        ("🔴", len(all_sells), "Active Sells"),
        ("🔔", total_chg,      "New Changes"),
    ])

    # ── Signal change rows ──────────────────────────────────
    change_rows = ""
    if new_buys:
        change_rows += email_section("🚨 New Buy Signals — Just crossed ≥7", "#14532d")
        for e in sorted(new_buys, key=lambda x: x.get("curr_score") or 0, reverse=True):
            change_rows += email_row(e, show_prev=True, highlight="rgba(34,197,94,0.07)")
    if new_sells:
        change_rows += email_section("🚨 New Sell Signals — Just crossed ≤4", "#7f1d1d")
        for e in sorted(new_sells, key=lambda x: x.get("curr_score") or 10):
            change_rows += email_row(e, show_prev=True, highlight="rgba(239,68,68,0.07)")
    if buy_weakened:
        change_rows += email_section("⚠️ Buy Zone Exited — Slipped below 7", "#422006")
        for e in sorted(buy_weakened, key=lambda x: x.get("curr_score") or 0, reverse=True):
            change_rows += email_row(e, show_prev=True, highlight="rgba(245,158,11,0.06)")
    if sell_cleared:
        change_rows += email_section("✅ Sell Zone Cleared — Climbed above 4", "#1e3a5f")
        for e in sorted(sell_cleared, key=lambda x: x.get("curr_score") or 0, reverse=True):
            change_rows += email_row(e, show_prev=True, highlight="rgba(59,130,246,0.07)")
    if notable:
        change_rows += email_section("📈 Notable Score Moves (≥1.5 pts)", "#1e1b4b")
        for e in sorted(notable, key=lambda x: abs(x.get("delta", 0)), reverse=True):
            change_rows += email_row(e, show_prev=True)

    if not change_rows:
        change_rows = f"""
        <tr><td colspan="8" style="padding:14px 12px;color:#64748b;font-size:13px;text-align:center;">
          ✓ No signal changes since the last report.
        </td></tr>"""

    # ── Per-horizon breakdowns ──────────────────────────────
    short_rows = horizon_email_block(current_data, "score_short", "Short-Term", "⚡")
    mid_rows   = horizon_email_block(current_data, "score_mid",   "Mid-Term",   "📊")
    long_rows  = horizon_email_block(current_data, "score_long",  "Long-Term",  "🏦")

    # ── Overall active signals ──────────────────────────────
    overall_rows = ""
    if all_buys:
        overall_rows += email_section(f"🟢 Overall Buy Signals ({len(all_buys)})", "#0f2d1f")
        for d in all_buys:
            overall_rows += email_row(to_entry(d))
    if all_sells:
        overall_rows += email_section(f"🔴 Overall Sell Signals ({len(all_sells)})", "#2d0f0f")
        for d in all_sells:
            overall_rows += email_row(to_entry(d))
    overall_rows += email_section(f"📋 Hold ({len(all_holds)})", "#1a1a2e")
    for d in all_holds:
        overall_rows += email_row(to_entry(d))

    # ── Staleness warning ────────────────────────────────────
    stale_html = ""
    if data_updated:
        try:
            hours_old = (datetime.now() - datetime.strptime(data_updated, "%Y-%m-%d %H:%M")).total_seconds() / 3600
            if hours_old > 3:
                stale_html = f"""
                <tr><td colspan="8" style="padding:8px 12px;background:#451a03;color:#fed7aa;font-size:12px;">
                  ⚠️ Data last refreshed {hours_old:.1f}h ago ({data_updated}). Open your dashboard to refresh.
                </td></tr>"""
        except Exception:
            pass

    change_summary = f"{total_chg} signal change{'s' if total_chg != 1 else ''}" if total_chg else "No signal changes since last report"

    # ── Divider between sections ─────────────────────────────
    def divider(label):
        return f"""
        <tr><td colspan="8" style="padding:18px 12px 4px;background:#0f172a;">
          <div style="border-top:1px solid #334155;padding-top:14px;font-size:11px;
               font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:#475569;">
            {label}
          </div>
        </td></tr>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#0f172a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#e2e8f0;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:800px;margin:0 auto;">

  <tr>
    <td style="background:linear-gradient(135deg,#1e3a5f,#0f2d1f);padding:26px 24px 18px;">
      <div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:5px;">{now_str}</div>
      <div style="font-size:24px;font-weight:800;color:#f1f5f9;">{period} Stock Report</div>
      <div style="font-size:13px;color:#94a3b8;margin-top:5px;">{change_summary}</div>
    </td>
  </tr>

  <tr>
    <td style="background:#1e293b;padding:14px 0;">
      <table width="100%" cellpadding="0" cellspacing="0"><tr>{summary_html}</tr></table>
    </td>
  </tr>

  <tr>
    <td style="padding:0;">
      <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:#0f172a;">
        {stale_html}

        <!-- SIGNAL CHANGES -->
        {divider("🔔 Signal Changes")}
        {TABLE_HEADER}
        {change_rows}

        <!-- SHORT-TERM -->
        {divider("⚡ Short-Term Signals (trades, days–weeks)")}
        {TABLE_HEADER}
        {short_rows}

        <!-- MID-TERM -->
        {divider("📊 Mid-Term Signals (swings, weeks–months)")}
        {TABLE_HEADER}
        {mid_rows}

        <!-- LONG-TERM -->
        {divider("🏦 Long-Term Signals (holds, months+)")}
        {TABLE_HEADER}
        {long_rows}

        <!-- OVERALL WATCHLIST -->
        {divider("📋 Full Watchlist — Overall Score")}
        {TABLE_HEADER}
        {overall_rows}

      </table>
    </td>
  </tr>

  <tr>
    <td style="padding:14px 24px;background:#0a0f1e;border-top:1px solid #1e293b;">
      <div style="font-size:11px;color:#475569;">
        <span style="background:#22c55e;color:#fff;padding:1px 7px;border-radius:8px;font-size:11px;">≥7 Buy</span>&nbsp;
        <span style="background:#f59e0b;color:#fff;padding:1px 7px;border-radius:8px;font-size:11px;">5–6.9 Hold</span>&nbsp;
        <span style="background:#ef4444;color:#fff;padding:1px 7px;border-radius:8px;font-size:11px;">≤4 Sell</span>
        &nbsp;·&nbsp; S/M/L = Short/Mid/Long scores &nbsp;·&nbsp; Data: {data_updated or 'unknown'}
      </div>
    </td>
  </tr>

</table>
</body></html>"""


# ─────────────────────────────────────────────
# LOCAL INTERACTIVE HTML REPORT
# ─────────────────────────────────────────────

def local_table_rows(stocks, score_key):
    """Rows for the local report table, highlighted by the given score key."""
    rows = ""
    for d in stocks:
        ticker    = d.get("ticker", "")
        price     = d.get("price")
        chg       = d.get("change_1d", 0) or 0
        score     = d.get(score_key)
        short_s   = d.get("score_short")
        mid_s     = d.get("score_mid")
        long_s    = d.get("score_long")
        rsi       = d.get("rsi")
        ma        = d.get("ma_trend", "N/A") or "N/A"
        rating    = d.get("rating", "N/A") or "N/A"
        sentiment = d.get("sentiment_label", "neutral") or "neutral"
        earnings  = d.get("earnings_trend", "N/A") or "N/A"

        if   score is None:           zone_cls = "zone-unknown"
        elif score >= BUY_THRESHOLD:  zone_cls = "zone-buy"
        elif score <= SELL_THRESHOLD: zone_cls = "zone-sell"
        else:                         zone_cls = "zone-hold"

        price_str = f"${price:.2f}" if isinstance(price, (int, float)) else "N/A"
        chg_cls   = "pos" if chg >= 0 else "neg"
        chg_str   = f"{'+'if chg>=0 else ''}{chg:.2f}%"
        ma_cls    = "pos" if ma == "Uptrend" else "neg" if ma == "Downtrend" else "neu"
        sent_cls  = "pos" if sentiment == "positive" else "neg" if sentiment == "negative" else "neu"
        score_str = f"{score:.1f}" if score is not None else "N/A"
        sml       = f"{short_s:.0f} / {mid_s:.0f} / {long_s:.0f}" if all(x is not None for x in [short_s, mid_s, long_s]) else "N/A"

        rows += f"""
        <tr class="{zone_cls}">
          <td class="ticker">{ticker}</td>
          <td>{price_str} <span class="{chg_cls}">{chg_str}</span></td>
          <td><span class="badge {zone_cls.replace('zone-','')}">{score_str}</span></td>
          <td class="sml">{sml}</td>
          <td class="{ma_cls}">{ma}</td>
          <td>{f'{rsi:.0f}' if rsi else 'N/A'}</td>
          <td>{rating}</td>
          <td class="{sent_cls}">{sentiment.title()}</td>
          <td>{earnings}</td>
        </tr>"""
    return rows

def build_local_html(current_data, data_updated, is_morning):
    period  = "🌅 Morning" if is_morning else "🌙 Evening"
    now_str = datetime.now().strftime("%B %d, %Y — %I:%M %p")

    all_stocks = sorted(current_data.values(), key=lambda x: x.get("score_overall") or 0, reverse=True)

    overall_rows = local_table_rows(all_stocks, "score_overall")

    short_sorted = sorted(current_data.values(), key=lambda x: x.get("score_short") or 0, reverse=True)
    mid_sorted   = sorted(current_data.values(), key=lambda x: x.get("score_mid")   or 0, reverse=True)
    long_sorted  = sorted(current_data.values(), key=lambda x: x.get("score_long")  or 0, reverse=True)

    short_rows = local_table_rows(short_sorted, "score_short")
    mid_rows   = local_table_rows(mid_sorted,   "score_mid")
    long_rows  = local_table_rows(long_sorted,  "score_long")

    stale_banner = ""
    if data_updated:
        try:
            hours_old = (datetime.now() - datetime.strptime(data_updated, "%Y-%m-%d %H:%M")).total_seconds() / 3600
            if hours_old > 3:
                stale_banner = f'<div class="stale-banner">⚠️ Data last refreshed {hours_old:.1f}h ago ({data_updated}). Open your dashboard to refresh.</div>'
        except Exception:
            pass

    th_row = """
    <tr>
      <th>Ticker</th><th>Price</th><th>Score</th><th>S / M / L</th>
      <th>MA Trend</th><th>RSI</th><th>Analyst</th><th>Sentiment</th><th>Earnings</th>
    </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stock Report — {now_str}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0f172a; color: #e2e8f0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }}

  header {{ background: linear-gradient(135deg,#1e3a5f,#0f2d1f); padding: 24px 32px 20px; }}
  header .label {{ font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 4px; }}
  header h1 {{ font-size: 22px; font-weight: 800; color: #f1f5f9; }}
  header .sub {{ font-size: 13px; color: #94a3b8; margin-top: 4px; }}

  .stale-banner {{ background: #451a03; color: #fed7aa; padding: 8px 32px; font-size: 12px; }}

  .tabs {{ display: flex; gap: 4px; padding: 16px 32px 0; background: #1e293b; border-bottom: 2px solid #334155; }}
  .tab  {{ padding: 8px 20px; border-radius: 6px 6px 0 0; cursor: pointer; font-size: 13px; font-weight: 600;
           color: #94a3b8; background: transparent; border: none; transition: all 0.15s; }}
  .tab:hover  {{ color: #e2e8f0; background: rgba(255,255,255,0.05); }}
  .tab.active {{ color: #f1f5f9; background: #0f172a; border-bottom: 2px solid #0f172a; margin-bottom: -2px; }}

  .panel {{ display: none; padding: 24px 32px; }}
  .panel.active {{ display: block; }}

  .legend {{ display: flex; gap: 12px; margin-bottom: 16px; align-items: center; flex-wrap: wrap; }}
  .legend .badge {{ padding: 3px 10px; border-radius: 10px; font-size: 12px; font-weight: 600; }}

  table {{ width: 100%; border-collapse: collapse; background: #0f172a; }}
  th {{ padding: 8px 12px; font-size: 11px; font-weight: 700; text-transform: uppercase;
        letter-spacing: 0.05em; color: #64748b; text-align: left; white-space: nowrap; border-bottom: 2px solid #334155; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #1e293b; }}

  tr.zone-buy  {{ background: rgba(34,197,94,0.05); }}
  tr.zone-sell {{ background: rgba(239,68,68,0.05); }}
  tr:hover {{ background: rgba(255,255,255,0.04) !important; }}

  td.ticker {{ font-weight: 700; font-size: 14px; color: #f1f5f9; }}
  td.sml    {{ font-size: 12px; color: #94a3b8; }}

  .badge        {{ display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 13px; font-weight: 700; }}
  .badge.buy    {{ background: #22c55e; color: #fff; }}
  .badge.sell   {{ background: #ef4444; color: #fff; }}
  .badge.hold   {{ background: #f59e0b; color: #fff; }}
  .badge.unknown{{ background: #555; color: #fff; }}

  .pos {{ color: #22c55e; font-weight: 600; }}
  .neg {{ color: #ef4444; font-weight: 600; }}
  .neu {{ color: #94a3b8; }}

  footer {{ padding: 14px 32px; background: #0a0f1e; border-top: 1px solid #1e293b;
            font-size: 11px; color: #475569; margin-top: 32px; }}
</style>
</head>
<body>

<header>
  <div class="label">{now_str}</div>
  <h1>{period} Stock Report</h1>
  <div class="sub">Data updated: {data_updated or 'unknown'}</div>
</header>

{stale_banner}

<div class="tabs">
  <button class="tab active" onclick="showTab('overall')">📊 Overall</button>
  <button class="tab"        onclick="showTab('short')">⚡ Short-Term</button>
  <button class="tab"        onclick="showTab('mid')">📈 Mid-Term</button>
  <button class="tab"        onclick="showTab('long')">🏦 Long-Term</button>
</div>

<div id="overall" class="panel active">
  <div class="legend">
    <span class="badge buy">≥7 Buy</span>
    <span class="badge hold">5–6.9 Hold</span>
    <span class="badge sell">≤4 Sell</span>
    <span style="color:#64748b;font-size:12px;">· Sorted by Overall score · Score shown = Overall</span>
  </div>
  <table>{th_row}{overall_rows}</table>
</div>

<div id="short" class="panel">
  <div class="legend">
    <span class="badge buy">≥7 Short Buy</span>
    <span class="badge sell">≤4 Short Sell</span>
    <span style="color:#64748b;font-size:12px;">· Sorted by Short-term score · Best for trades (days to weeks) · RSI + momentum-driven</span>
  </div>
  <table>{th_row}{short_rows}</table>
</div>

<div id="mid" class="panel">
  <div class="legend">
    <span class="badge buy">≥7 Mid Buy</span>
    <span class="badge sell">≤4 Mid Sell</span>
    <span style="color:#64748b;font-size:12px;">· Sorted by Mid-term score · Best for swings (weeks to months) · MA trend + earnings-driven</span>
  </div>
  <table>{th_row}{mid_rows}</table>
</div>

<div id="long" class="panel">
  <div class="legend">
    <span class="badge buy">≥7 Long Buy</span>
    <span class="badge sell">≤4 Long Sell</span>
    <span style="color:#64748b;font-size:12px;">· Sorted by Long-term score · Best for holds (months+) · Analyst ratings + fundamentals-driven</span>
  </div>
  <table>{th_row}{long_rows}</table>
</div>

<footer>
  S/M/L = Short / Mid / Long scores &nbsp;·&nbsp;
  Generated: {now_str} &nbsp;·&nbsp;
  Data: {data_updated or 'unknown'}
</footer>

<script>
function showTab(id) {{
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  event.target.classList.add('active');
}}
</script>

</body>
</html>"""

def save_local_report(html):
    with open(LOCAL_REPORT_FILE, "w") as f:
        f.write(html)
    print(f"[Report] 📄 Local report saved → {LOCAL_REPORT_FILE}")


# ─────────────────────────────────────────────
# EMAIL SENDER
# ─────────────────────────────────────────────

def send_email(subject, html_body):
    if not GMAIL_APP_PASS:
        print("[Report] ERROR: GMAIL_APP_PASS is not set.")
        print("  Run:  GMAIL_APP_PASS=\"$(cat ~/.stock_gmail_pass)\" venv/bin/python report_emailer.py")
        sys.exit(1)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = RECIPIENT
    msg.attach(MIMEText(html_body, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASS)
        server.sendmail(GMAIL_USER, RECIPIENT, msg.as_string())

    print(f"[Report] ✅ Email sent to {RECIPIENT}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print(f"\n[Report] Starting at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    stock_data   = load_stock_data()
    snapshot     = load_snapshot()
    data_updated = next((v.get("updated") for v in stock_data.values() if v.get("updated")), None)

    print(f"[Report] Loaded {len(stock_data)} tickers. Data updated: {data_updated}")

    new_buys, new_sells, buy_weakened, sell_cleared, notable = detect_changes(stock_data, snapshot)
    all_buys, all_sells, all_holds = get_all_buys_sells(stock_data)

    print(f"[Report] Changes → new buys: {len(new_buys)}, new sells: {len(new_sells)}, "
          f"exits: {len(buy_weakened)+len(sell_cleared)}, notable: {len(notable)}")
    print(f"[Report] Active  → {len(all_buys)} buys, {len(all_sells)} sells, {len(all_holds)} holds")

    hour       = datetime.now().hour
    is_morning = hour < 14
    total_chg  = len(new_buys) + len(new_sells) + len(buy_weakened) + len(sell_cleared)
    period     = "🌅 Morning" if is_morning else "🌙 Evening"
    date_str   = datetime.now().strftime("%b %d")
    change_part = f" · {total_chg} change{'s' if total_chg != 1 else ''}" if total_chg else " · No changes"
    subject    = f"{period} Report · {date_str}{change_part} · {len(all_buys)}🟢 {len(all_sells)}🔴"

    # Build & send email
    email_html = build_html_email(
        stock_data, new_buys, new_sells, buy_weakened,
        sell_cleared, notable, all_buys, all_sells, all_holds,
        is_morning, data_updated
    )
    send_email(subject, email_html)

    # Build & save local interactive report
    local_html = build_local_html(stock_data, data_updated, is_morning)
    save_local_report(local_html)

    save_snapshot(stock_data)
    print("[Report] Done.\n")


if __name__ == "__main__":
    main()
