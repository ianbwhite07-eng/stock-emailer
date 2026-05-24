#!/usr/bin/env python3
"""
Cloud Stock Report Emailer
===========================
Fully standalone — fetches its own data from yfinance, Finnhub, and FRED.
Designed to run on GitHub Actions twice a day with no local dependencies.

All secrets come from environment variables (set as GitHub Secrets):
  GMAIL_APP_PASS  — Gmail App Password
  FINNHUB_KEY     — Finnhub API key
  FRED_KEY        — FRED API key

The watchlist is read from watchlist.json in the same folder.
A snapshot.json is committed back to the repo after each run for change detection.
"""

import json
import os
import sys
import time
import smtplib
import ssl
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─────────────────────────────────────────────
# CONFIG  (all secrets from environment)
# ─────────────────────────────────────────────

GMAIL_USER    = "ianbwhite07@gmail.com"
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS", "")
RECIPIENT     = "ianbwhite07@gmail.com"
FINNHUB_KEY   = os.environ.get("FINNHUB_KEY", "")
FRED_KEY      = os.environ.get("FRED_KEY", "")

BUY_THRESHOLD  = 7.0
SELL_THRESHOLD = 4.0

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(SCRIPT_DIR, "watchlist.json")
SNAPSHOT_FILE  = os.path.join(SCRIPT_DIR, "snapshot.json")
REPORT_FILE    = os.path.join(SCRIPT_DIR, "latest_report.html")


# ─────────────────────────────────────────────
# WATCHLIST
# ─────────────────────────────────────────────

def load_watchlist():
    with open(WATCHLIST_FILE) as f:
        return json.load(f)


# ─────────────────────────────────────────────
# PRICE + TECHNICALS (yfinance)
# ─────────────────────────────────────────────

def calculate_rsi(series, period=14):
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs       = avg_gain / avg_loss
    rsi      = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 1) if not rsi.empty else None

def get_technicals(series):
    if series is None or len(series) < 16:
        return {"rsi": None, "rsi_label": "N/A", "ma20": None, "ma50": None, "ma_trend": "N/A"}
    rsi   = calculate_rsi(series)
    label = "Bullish" if rsi and rsi >= 60 else "Bearish" if rsi and rsi <= 40 else "Neutral"
    ma20  = round(float(series.rolling(20).mean().iloc[-1]), 2) if len(series) >= 20 else None
    ma50  = round(float(series.rolling(50).mean().iloc[-1]), 2) if len(series) >= 50 else None
    if ma20 and ma50:
        trend = "Uptrend" if ma20 > ma50 else "Downtrend" if ma20 < ma50 else "Flat"
    else:
        trend = "N/A"
    return {"rsi": rsi, "rsi_label": label, "ma20": ma20, "ma50": ma50, "ma_trend": trend}

def fetch_prices_and_series(watchlist):
    print(f"[Data] Fetching prices for {len(watchlist)} tickers...")
    try:
        raw   = yf.download(watchlist, period="3mo", progress=False, auto_adjust=True)
        close = raw["Close"]
        if isinstance(close.columns, pd.MultiIndex):
            close.columns = close.columns.get_level_values(-1)
        if isinstance(close, pd.Series):
            close = close.to_frame(name=watchlist[0])
        clean = close.dropna(how="all")
    except Exception as e:
        print(f"[Data] yfinance error: {e}")
        return {}, {}

    prices, series_map = {}, {}
    for ticker in watchlist:
        try:
            s        = clean[ticker].dropna()
            current  = float(s.iloc[-1])
            prev_1d  = float(s.iloc[-2])  if len(s) >= 2  else current
            prev_5d  = float(s.iloc[-6])  if len(s) >= 6  else float(s.iloc[0])
            prev_1mo = float(s.iloc[-22]) if len(s) >= 22 else float(s.iloc[0])
            prices[ticker] = {
                "price":      round(current, 2),
                "change_1d":  round(((current - prev_1d)  / prev_1d)  * 100, 2) if prev_1d  else 0,
                "change_5d":  round(((current - prev_5d)  / prev_5d)  * 100, 2) if prev_5d  else 0,
                "change_1mo": round(((current - prev_1mo) / prev_1mo) * 100, 2) if prev_1mo else 0,
            }
            series_map[ticker] = s
        except Exception:
            prices[ticker] = {"price": "N/A", "change_1d": 0, "change_5d": 0, "change_1mo": 0}
    return prices, series_map


# ─────────────────────────────────────────────
# FUNDAMENTALS (yfinance)
# ─────────────────────────────────────────────

def fetch_fundamentals(ticker):
    try:
        info = yf.Ticker(ticker).info
        return {
            "short_percent":         round(info.get("shortPercentOfFloat") or 0, 4),
            "revenue_growth":        info.get("revenueGrowth"),
            "earnings_growth":       info.get("earningsGrowth"),
            "forward_pe":            info.get("forwardPE"),
            "trailing_pe":           info.get("trailingPE"),
            "held_pct_institutions": info.get("heldPercentInstitutions"),
            "beta":                  info.get("beta"),
            "is_etf":                info.get("quoteType") == "ETF",
        }
    except Exception as e:
        print(f"[Fundamentals] {ticker}: {e}")
        return {}


# ─────────────────────────────────────────────
# FINNHUB DATA
# ─────────────────────────────────────────────

def finnhub_get(path, params):
    if not FINNHUB_KEY:
        return None
    try:
        r = requests.get(f"https://finnhub.io/api/v1/{path}",
                         params={**params, "token": FINNHUB_KEY}, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None

def fetch_earnings(ticker):
    data = finnhub_get("stock/earnings", {"symbol": ticker})
    if not data: return "N/A"
    beats, misses = 0, 0
    for q in data[:4]:
        actual, estimate = q.get("actual"), q.get("estimate")
        if actual is not None and estimate is not None:
            if actual >= estimate: beats += 1
            else: misses += 1
    total = beats + misses
    if total == 0: return "N/A"
    if beats == total:  return f"Beat x{beats}"
    if misses == total: return f"Missed x{misses}"
    return f"Mixed ({beats}B/{misses}M)"

def fetch_rating(ticker):
    data = finnhub_get("stock/recommendation", {"symbol": ticker})
    if not data: return {"rating": "N/A", "breakdown": {}, "total_analysts": 0}
    latest = data[0]
    counts = {
        "Strong Buy": latest.get("strongBuy", 0), "Buy": latest.get("buy", 0),
        "Hold": latest.get("hold", 0), "Sell": latest.get("sell", 0),
        "Strong Sell": latest.get("strongSell", 0),
    }
    best  = max(counts, key=counts.get)
    total = sum(counts.values())
    return {"rating": best if counts[best] > 0 else "N/A", "breakdown": counts, "total_analysts": total}

def fetch_insider(ticker):
    from_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    to_date   = datetime.now().strftime("%Y-%m-%d")
    data      = finnhub_get("stock/insider-transactions",
                            {"symbol": ticker, "from": from_date, "to": to_date})
    if not data or not data.get("data"):
        return {"signal": "neutral", "net": 0, "buys": 0, "sells": 0}
    net_shares = sum(
        t.get("share", 0) if t.get("transactionCode") in ("P", "A") else -t.get("share", 0)
        for t in data["data"] if t.get("transactionCode") in ("P", "A", "S", "D")
    )
    buys  = sum(1 for t in data["data"] if t.get("transactionCode") in ("P", "A"))
    sells = sum(1 for t in data["data"] if t.get("transactionCode") in ("S", "D"))
    signal = "buying" if net_shares > 1000 else "selling" if net_shares < -1000 else "neutral"
    return {"signal": signal, "net": net_shares, "buys": buys, "sells": sells}

def fetch_sentiment(ticker):
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        analyzer = SentimentIntensityAnalyzer()
    except ImportError:
        return {"sentiment_score": 0.0, "sentiment_label": "neutral", "headlines": []}

    from_date = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    to_date   = datetime.now().strftime("%Y-%m-%d")
    data      = finnhub_get("company-news",
                            {"symbol": ticker, "from": from_date, "to": to_date})
    if not data:
        return {"sentiment_score": 0.0, "sentiment_label": "neutral", "headlines": []}

    scores    = [analyzer.polarity_scores(a.get("headline", ""))["compound"] for a in data[:20]]
    avg_score = round(sum(scores) / len(scores), 3) if scores else 0.0
    label     = "positive" if avg_score > 0.05 else "negative" if avg_score < -0.05 else "neutral"
    headlines = [{"title": a.get("headline", ""), "url": a.get("url", "")} for a in data[:5]]
    return {"sentiment_score": avg_score, "sentiment_label": label, "headlines": headlines}


# ─────────────────────────────────────────────
# MACRO (FRED)
# ─────────────────────────────────────────────

FRED_SERIES = {
    "VIXCLS":       "VIX",
    "BAMLH0A0HYM2": "HY Credit Spread",
    "T10Y2Y":       "10Y-2Y Spread",
    "FEDFUNDS":     "Fed Funds Rate",
}

def fetch_macro():
    if not FRED_KEY:
        return {}
    result = {}
    for series_id in FRED_SERIES:
        try:
            r = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={"series_id": series_id, "api_key": FRED_KEY,
                        "sort_order": "desc", "limit": 2, "file_type": "json"},
                timeout=10,
            )
            if r.status_code == 200:
                obs = [o for o in r.json().get("observations", []) if o.get("value") != "."]
                if obs:
                    result[series_id] = {"value": float(obs[0]["value"]),
                                         "prev":  float(obs[1]["value"]) if len(obs) > 1 else float(obs[0]["value"])}
        except Exception:
            pass
    return result

def get_macro_adjustments(macro):
    vix_val = (macro.get("VIXCLS") or {}).get("value")
    hy_val  = (macro.get("BAMLH0A0HYM2") or {}).get("value")
    t10y2y  = (macro.get("T10Y2Y") or {}).get("value")
    vix_adj = -1 if (vix_val and vix_val > 30) else 1 if (vix_val and vix_val < 15) else 0
    credit_adj = -1 if (hy_val and hy_val > 500) or (t10y2y and t10y2y < -0.5) else 0
    return vix_adj, credit_adj


# ─────────────────────────────────────────────
# SCORING  (mirrors app.py exactly)
# ─────────────────────────────────────────────

def score_short(s):
    score = 5
    rsi   = s.get("rsi")
    if rsi is not None:
        if   rsi >  80: score += 2
        elif rsi >  70: score += 1
        elif rsi <= 25: score -= 3
        elif rsi <= 30: score -= 2
        elif rsi <= 40: score -= 1
    c5 = s.get("change_5d", 0) or 0
    if   c5 >  8: score += 2
    elif c5 >  3: score += 1
    elif c5 < -8: score -= 2
    elif c5 < -3: score -= 1
    short_pct = s.get("short_percent")
    if short_pct and short_pct > 0.25: score -= 1
    beta = s.get("beta")
    if beta and beta > 2.5: score -= 1
    sentiment = s.get("sentiment_label")
    if   sentiment == "positive" and (rsi or 50) > 50: score += 1
    elif sentiment == "negative": score -= 1
    score += s.get("macro_vix_adj", 0) or 0
    return max(0, min(10, score))

def score_mid(s):
    score = 5
    ma20, ma50 = s.get("ma20"), s.get("ma50")
    if ma20 and ma50 and ma50 > 0:
        gap = (ma20 - ma50) / ma50 * 100
        if   gap >  8: score += 2
        elif gap >  5: score += 1
        elif gap > -3: pass
        elif gap > -8: score -= 2
        else:          score -= 3
    elif s.get("ma_trend") == "Uptrend":   score += 1
    elif s.get("ma_trend") == "Downtrend": score -= 2
    e = s.get("earnings_trend", "") or ""
    if   "Beat x4"   in e: score += 2
    elif "Beat x3"   in e: score += 1
    elif "Beat x2"   in e: score += 1
    elif "Missed x4" in e: score -= 3
    elif "Missed x3" in e: score -= 2
    elif "Missed x2" in e: score -= 1
    rev = s.get("revenue_growth")
    if rev is not None:
        if   rev >  0.25: score += 1
        elif rev < -0.10: score -= 1
    return max(0, min(10, score))

def score_long(s):
    if s.get("is_etf"):
        score = 7
        ma20, ma50 = s.get("ma20"), s.get("ma50")
        if ma20 and ma50 and ma50 > 0:
            gap = (ma20 - ma50) / ma50 * 100
            if   gap >  2: score += 1
            elif gap < -5: score -= 2
            elif gap < -2: score -= 1
        beta = s.get("beta")
        if beta:
            if   beta <= 1.2: score += 1
            elif beta >  2.0: score -= 1
        score += s.get("macro_credit_adj", 0) or 0
        return max(0, min(10, score))

    score = 5
    rating_pts = {"Strong Buy": 2, "Buy": 1, "Hold": 0, "Sell": -2, "Strong Sell": -3}
    score += rating_pts.get(s.get("rating", "N/A"), 0)
    fpe = s.get("forward_pe")
    if fpe is not None:
        if   fpe <  0:  score -= 2
        elif fpe < 12:  score += 1
        elif fpe > 50:  score -= 1
        elif fpe > 80:  score -= 2
    inst = s.get("held_pct_institutions")
    if inst is not None:
        if   inst > 0.75: score += 1
        elif inst < 0.20: score -= 1
    insider = s.get("insider_signal")
    if   insider == "buying":  score += 1
    elif insider == "selling": score -= 1
    score += s.get("macro_credit_adj", 0) or 0
    return max(0, min(10, score))

def score_overall(s):
    return round(score_short(s) * 0.25 + score_mid(s) * 0.40 + score_long(s) * 0.35, 1)


# ─────────────────────────────────────────────
# MAIN DATA FETCH
# ─────────────────────────────────────────────

def fetch_all_data(watchlist):
    print("[Data] Fetching macro data...")
    macro = fetch_macro()
    vix_adj, credit_adj = get_macro_adjustments(macro)
    print(f"[Data] Macro: VIX adj={vix_adj}, credit adj={credit_adj}")

    prices, series_map = fetch_prices_and_series(watchlist)

    stocks = {}
    for i, ticker in enumerate(watchlist):
        print(f"[Data] Processing {ticker} ({i+1}/{len(watchlist)})...")
        price_info = prices.get(ticker, {"price": "N/A", "change_1d": 0, "change_5d": 0, "change_1mo": 0})
        series     = series_map.get(ticker)
        tech       = get_technicals(series)
        fund       = fetch_fundamentals(ticker)
        time.sleep(0.3)

        earnings = fetch_earnings(ticker);     time.sleep(0.5)
        rating   = fetch_rating(ticker);       time.sleep(0.5)
        insider  = fetch_insider(ticker);      time.sleep(0.5)
        sentiment= fetch_sentiment(ticker);    time.sleep(0.5)

        entry = {
            "ticker":                ticker,
            "price":                 price_info["price"],
            "change_1d":             price_info["change_1d"],
            "change_5d":             price_info["change_5d"],
            "change_1mo":            price_info["change_1mo"],
            "rsi":                   tech["rsi"],
            "rsi_label":             tech["rsi_label"],
            "ma20":                  tech["ma20"],
            "ma50":                  tech["ma50"],
            "ma_trend":              tech["ma_trend"],
            "earnings_trend":        earnings,
            "rating":                rating["rating"],
            "total_analysts":        rating["total_analysts"],
            "insider_signal":        insider["signal"],
            "sentiment_label":       sentiment["sentiment_label"],
            "sentiment_score":       sentiment["sentiment_score"],
            "short_percent":         fund.get("short_percent"),
            "revenue_growth":        fund.get("revenue_growth"),
            "forward_pe":            fund.get("forward_pe"),
            "held_pct_institutions": fund.get("held_pct_institutions"),
            "beta":                  fund.get("beta"),
            "is_etf":                fund.get("is_etf", False),
            "macro_vix_adj":         vix_adj,
            "macro_credit_adj":      credit_adj,
            "updated":               datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        entry["score_short"]   = score_short(entry)
        entry["score_mid"]     = score_mid(entry)
        entry["score_long"]    = score_long(entry)
        entry["score_overall"] = score_overall(entry)
        stocks[ticker] = entry
        print(f"  → overall={entry['score_overall']} short={entry['score_short']} mid={entry['score_mid']} long={entry['score_long']}")

    return stocks


# ─────────────────────────────────────────────
# SNAPSHOT / CHANGE DETECTION
# ─────────────────────────────────────────────

def load_snapshot():
    if os.path.exists(SNAPSHOT_FILE):
        with open(SNAPSHOT_FILE) as f:
            return json.load(f)
    return {}

def save_snapshot(stocks):
    snap = {t: {"score_overall": d["score_overall"], "score_short": d["score_short"],
                "score_mid": d["score_mid"], "score_long": d["score_long"],
                "price": d["price"]}
            for t, d in stocks.items()}
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(snap, f, indent=2)

def classify(score):
    if score is None:           return "unknown"
    if score >= BUY_THRESHOLD:  return "buy"
    if score <= SELL_THRESHOLD: return "sell"
    return "hold"

def detect_changes(stocks, snapshot):
    new_buys, new_sells, buy_weakened, sell_cleared, notable = [], [], [], [], []
    for ticker, data in stocks.items():
        curr_score = data["score_overall"]
        prev_score = (snapshot.get(ticker) or {}).get("score_overall")
        curr_zone  = classify(curr_score)
        prev_zone  = classify(prev_score) if prev_score is not None else None
        entry      = {**data, "curr_score": curr_score, "prev_score": prev_score,
                      "sentiment": data.get("sentiment_label", "neutral")}
        if prev_zone is None:
            pass
        elif prev_zone != curr_zone:
            if   curr_zone == "buy":                          new_buys.append(entry)
            elif curr_zone == "sell":                         new_sells.append(entry)
            elif curr_zone == "hold" and prev_zone == "buy":  buy_weakened.append(entry)
            elif curr_zone == "hold" and prev_zone == "sell": sell_cleared.append(entry)
        elif curr_score is not None and prev_score is not None and abs(curr_score - prev_score) >= 1.5:
            entry["delta"] = curr_score - prev_score
            notable.append(entry)
    return new_buys, new_sells, buy_weakened, sell_cleared, notable

def get_all_zones(stocks):
    buys  = sorted([d for d in stocks.values() if classify(d["score_overall"]) == "buy"],
                   key=lambda x: x["score_overall"], reverse=True)
    sells = sorted([d for d in stocks.values() if classify(d["score_overall"]) == "sell"],
                   key=lambda x: x["score_overall"])
    holds = sorted([d for d in stocks.values() if classify(d["score_overall"]) == "hold"],
                   key=lambda x: x["score_overall"], reverse=True)
    return buys, sells, holds


# ─────────────────────────────────────────────
# HTML HELPERS (shared email + local report)
# ─────────────────────────────────────────────

def score_pill(score, prev=None, small=False):
    if score is None:
        return '<span style="background:#555;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">N/A</span>'
    bg = "#22c55e" if score >= BUY_THRESHOLD else "#ef4444" if score <= SELL_THRESHOLD else "#f59e0b"
    delta_html = ""
    if prev is not None and prev != score:
        arrow = "▲" if score > prev else "▼"
        delta_html = f' <span style="font-size:10px;opacity:0.85">{arrow}{abs(score-prev):.1f}</span>'
    fs = "12px" if small else "13px"
    return (f'<span style="background:{bg};color:#fff;padding:2px 9px;border-radius:12px;'
            f'font-size:{fs};font-weight:600;">{score:.1f}{delta_html}</span>')

def chg_badge(v):
    if v is None: return ""
    c = "#22c55e" if v >= 0 else "#ef4444"
    return f'<span style="color:{c};font-size:12px;font-weight:600;">{"+" if v>=0 else ""}{v:.2f}%</span>'

TH = "padding:7px 12px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;color:#64748b;white-space:nowrap;"
TABLE_HDR = f"""<tr style="border-bottom:2px solid #334155;">
  <th style="{TH}">Ticker</th><th style="{TH}">Price</th><th style="{TH}">Score</th>
  <th style="{TH}">S/M/L</th><th style="{TH}">MA</th><th style="{TH}">RSI</th>
  <th style="{TH}">Analyst</th><th style="{TH}">Sentiment</th>
</tr>"""

def email_row(d, score_key="score_overall", prev=None, hl=None):
    ticker = d.get("ticker",""); price = d.get("price"); chg = d.get("change_1d",0) or 0
    score  = d.get(score_key); s = d.get("score_short"); m = d.get("score_mid"); l = d.get("score_long")
    rsi    = d.get("rsi"); ma = d.get("ma_trend","N/A") or "N/A"
    rating = d.get("rating","N/A") or "N/A"; sent = d.get("sentiment_label","neutral") or "neutral"
    if d.get("curr_score") is not None and score_key == "score_overall": score = d["curr_score"]
    price_str = f"${price:.2f}" if isinstance(price,(int,float)) else "N/A"
    mac = "#22c55e" if ma=="Uptrend" else "#ef4444" if ma=="Downtrend" else "#888"
    sc  = "#22c55e" if sent=="positive" else "#ef4444" if sent=="negative" else "#888"
    bg  = f"background:{hl};" if hl else ""
    sml = f"{s:.0f}/{m:.0f}/{l:.0f}" if all(x is not None for x in [s,m,l]) else "N/A"
    return f"""<tr style="{bg}border-bottom:1px solid #2a2a2a;">
      <td style="padding:9px 12px;font-weight:700;font-size:14px;color:#e2e8f0;">{ticker}</td>
      <td style="padding:9px 12px;color:#cbd5e1;">{price_str} {chg_badge(chg)}</td>
      <td style="padding:9px 12px;">{score_pill(score, prev)}</td>
      <td style="padding:9px 8px;font-size:12px;color:#94a3b8;">{sml}</td>
      <td style="padding:9px 8px;font-size:12px;color:{mac};">{ma}</td>
      <td style="padding:9px 8px;font-size:12px;color:#94a3b8;">{f'{rsi:.0f}' if rsi else 'N/A'}</td>
      <td style="padding:9px 8px;font-size:12px;color:#94a3b8;">{rating}</td>
      <td style="padding:9px 8px;font-size:12px;color:{sc};">{sent.title()}</td>
    </tr>"""

def email_sec(title, bg):
    return f"""<tr><td colspan="8" style="padding:11px 12px 4px;background:{bg};
      font-size:12px;font-weight:700;letter-spacing:0.06em;color:#f1f5f9;text-transform:uppercase;">
      {title}</td></tr>"""

def divider(label):
    return f"""<tr><td colspan="8" style="padding:16px 12px 4px;background:#0f172a;">
      <div style="border-top:1px solid #334155;padding-top:12px;font-size:11px;font-weight:700;
           letter-spacing:0.1em;text-transform:uppercase;color:#475569;">{label}</div>
    </td></tr>"""

def horizon_rows(stocks, horizon_key, label, emoji):
    all_s  = list(stocks.values())
    buys   = sorted([d for d in all_s if classify(d.get(horizon_key)) == "buy"],  key=lambda x: x.get(horizon_key) or 0, reverse=True)
    sells  = sorted([d for d in all_s if classify(d.get(horizon_key)) == "sell"], key=lambda x: x.get(horizon_key) or 10)
    rows = ""
    if buys:
        rows += email_sec(f"{emoji} {label} Buys ({len(buys)})", "#0f2d1f")
        for d in buys:
            rows += email_row({**d, "curr_score": d.get(horizon_key)})
    if sells:
        rows += email_sec(f"🔴 {label} Sells ({len(sells)})", "#2d0f0f")
        for d in sells:
            rows += email_row({**d, "curr_score": d.get(horizon_key)})
    if not buys and not sells:
        rows += f'<tr><td colspan="8" style="padding:10px 12px;color:#64748b;font-size:12px;">No {label.lower()} signals.</td></tr>'
    return rows


# ─────────────────────────────────────────────
# EMAIL BUILDER
# ─────────────────────────────────────────────

def build_email(stocks, new_buys, new_sells, buy_weakened, sell_cleared, notable,
                all_buys, all_sells, all_holds, is_morning, data_updated):
    period    = "🌅 Morning" if is_morning else "🌙 Evening"
    now_str   = datetime.now().strftime("%B %d, %Y — %I:%M %p UTC")
    total_chg = len(new_buys) + len(new_sells) + len(buy_weakened) + len(sell_cleared)

    summary = "".join(f"""<td style="text-align:center;padding:0 20px;">
      <div style="font-size:26px;font-weight:800;color:#f1f5f9;">{icon} {count}</div>
      <div style="font-size:11px;color:#94a3b8;margin-top:2px;text-transform:uppercase;letter-spacing:0.06em;">{lbl}</div>
    </td>""" for icon, count, lbl in [("🟢",len(all_buys),"Active Buys"),("🔴",len(all_sells),"Active Sells"),("🔔",total_chg,"New Changes")])

    # Changes section
    chg_rows = ""
    if new_buys:
        chg_rows += email_sec("🚨 New Buy Signals — Just crossed ≥7", "#14532d")
        for e in sorted(new_buys, key=lambda x: x.get("curr_score") or 0, reverse=True):
            chg_rows += email_row(e, prev=e.get("prev_score"), hl="rgba(34,197,94,0.07)")
    if new_sells:
        chg_rows += email_sec("🚨 New Sell Signals — Just crossed ≤4", "#7f1d1d")
        for e in sorted(new_sells, key=lambda x: x.get("curr_score") or 10):
            chg_rows += email_row(e, prev=e.get("prev_score"), hl="rgba(239,68,68,0.07)")
    if buy_weakened:
        chg_rows += email_sec("⚠️ Buy Zone Exited", "#422006")
        for e in buy_weakened:
            chg_rows += email_row(e, prev=e.get("prev_score"), hl="rgba(245,158,11,0.06)")
    if sell_cleared:
        chg_rows += email_sec("✅ Sell Zone Cleared", "#1e3a5f")
        for e in sell_cleared:
            chg_rows += email_row(e, prev=e.get("prev_score"), hl="rgba(59,130,246,0.07)")
    if notable:
        chg_rows += email_sec("📈 Notable Score Moves (≥1.5 pts)", "#1e1b4b")
        for e in sorted(notable, key=lambda x: abs(x.get("delta",0)), reverse=True):
            chg_rows += email_row(e, prev=e.get("prev_score"))
    if not chg_rows:
        chg_rows = '<tr><td colspan="8" style="padding:14px 12px;color:#64748b;font-size:13px;text-align:center;">✓ No signal changes since last report.</td></tr>'

    # Overall
    overall_rows = ""
    if all_buys:
        overall_rows += email_sec(f"🟢 Overall Buys ({len(all_buys)})", "#0f2d1f")
        for d in all_buys: overall_rows += email_row(d)
    if all_sells:
        overall_rows += email_sec(f"🔴 Overall Sells ({len(all_sells)})", "#2d0f0f")
        for d in all_sells: overall_rows += email_row(d)
    overall_rows += email_sec(f"📋 Hold ({len(all_holds)})", "#1a1a2e")
    for d in all_holds: overall_rows += email_row(d)

    change_summary = f"{total_chg} signal change{'s' if total_chg!=1 else ''}" if total_chg else "No signal changes since last report"

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#0f172a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#e2e8f0;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:800px;margin:0 auto;">
  <tr><td style="background:linear-gradient(135deg,#1e3a5f,#0f2d1f);padding:26px 24px 18px;">
    <div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:5px;">{now_str}</div>
    <div style="font-size:24px;font-weight:800;color:#f1f5f9;">{period} Stock Report</div>
    <div style="font-size:13px;color:#94a3b8;margin-top:5px;">{change_summary}</div>
  </td></tr>
  <tr><td style="background:#1e293b;padding:14px 0;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>{summary}</tr></table>
  </td></tr>
  <tr><td style="padding:0;">
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:#0f172a;">
      {divider("🔔 Signal Changes")}{TABLE_HDR}{chg_rows}
      {divider("⚡ Short-Term (days–weeks)")}{TABLE_HDR}{horizon_rows(stocks,"score_short","Short-Term","⚡")}
      {divider("📊 Mid-Term (weeks–months)")}{TABLE_HDR}{horizon_rows(stocks,"score_mid","Mid-Term","📊")}
      {divider("🏦 Long-Term (months+)")}{TABLE_HDR}{horizon_rows(stocks,"score_long","Long-Term","🏦")}
      {divider("📋 Full Watchlist — Overall")}{TABLE_HDR}{overall_rows}
    </table>
  </td></tr>
  <tr><td style="padding:14px 24px;background:#0a0f1e;border-top:1px solid #1e293b;">
    <div style="font-size:11px;color:#475569;">
      <span style="background:#22c55e;color:#fff;padding:1px 7px;border-radius:8px;font-size:11px;">≥7 Buy</span>&nbsp;
      <span style="background:#f59e0b;color:#fff;padding:1px 7px;border-radius:8px;font-size:11px;">5–6.9 Hold</span>&nbsp;
      <span style="background:#ef4444;color:#fff;padding:1px 7px;border-radius:8px;font-size:11px;">≤4 Sell</span>
      &nbsp;·&nbsp; S/M/L = Short/Mid/Long &nbsp;·&nbsp; Data: {data_updated or 'unknown'}
    </div>
  </td></tr>
</table></body></html>"""


# ─────────────────────────────────────────────
# LOCAL INTERACTIVE HTML REPORT
# ─────────────────────────────────────────────

def local_row(d, score_key):
    ticker = d.get("ticker",""); price = d.get("price"); chg = d.get("change_1d",0) or 0
    score  = d.get(score_key); s = d.get("score_short"); m = d.get("score_mid"); l = d.get("score_long")
    rsi    = d.get("rsi"); ma = d.get("ma_trend","N/A") or "N/A"
    rating = d.get("rating","N/A") or "N/A"; sent = d.get("sentiment_label","neutral") or "neutral"
    zone   = classify(score)
    price_str = f"${price:.2f}" if isinstance(price,(int,float)) else "N/A"
    chg_cls   = "pos" if chg>=0 else "neg"
    chg_str   = f"{'+'if chg>=0 else ''}{chg:.2f}%"
    ma_cls    = "pos" if ma=="Uptrend" else "neg" if ma=="Downtrend" else "neu"
    sc_cls    = "pos" if sent=="positive" else "neg" if sent=="negative" else "neu"
    score_str = f"{score:.1f}" if score is not None else "N/A"
    sml       = f"{s:.0f}/{m:.0f}/{l:.0f}" if all(x is not None for x in [s,m,l]) else "N/A"
    return f"""<tr class="zone-{zone}">
      <td class="ticker">{ticker}</td>
      <td>{price_str} <span class="{chg_cls}">{chg_str}</span></td>
      <td><span class="badge {zone}">{score_str}</span></td>
      <td class="sml">{sml}</td>
      <td class="{ma_cls}">{ma}</td>
      <td>{f'{rsi:.0f}' if rsi else 'N/A'}</td>
      <td>{rating}</td>
      <td class="{sc_cls}">{sent.title()}</td>
    </tr>"""

def build_local_html(stocks, data_updated, is_morning):
    period  = "🌅 Morning" if is_morning else "🌙 Evening"
    now_str = datetime.now().strftime("%B %d, %Y — %I:%M %p UTC")
    all_s   = list(stocks.values())

    def sorted_rows(key):
        return "".join(local_row(d, key) for d in sorted(all_s, key=lambda x: x.get(key) or 0, reverse=True))

    th = "<tr><th>Ticker</th><th>Price</th><th>Score</th><th>S/M/L</th><th>MA</th><th>RSI</th><th>Analyst</th><th>Sentiment</th></tr>"

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stock Report — {now_str}</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0f172a;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px}}
header{{background:linear-gradient(135deg,#1e3a5f,#0f2d1f);padding:24px 32px 20px}}
header .lbl{{font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px}}
header h1{{font-size:22px;font-weight:800;color:#f1f5f9}}
header .sub{{font-size:13px;color:#94a3b8;margin-top:4px}}
.tabs{{display:flex;gap:4px;padding:16px 32px 0;background:#1e293b;border-bottom:2px solid #334155}}
.tab{{padding:8px 20px;border-radius:6px 6px 0 0;cursor:pointer;font-size:13px;font-weight:600;color:#94a3b8;background:transparent;border:none;transition:all .15s}}
.tab:hover{{color:#e2e8f0;background:rgba(255,255,255,.05)}}
.tab.active{{color:#f1f5f9;background:#0f172a;border-bottom:2px solid #0f172a;margin-bottom:-2px}}
.panel{{display:none;padding:24px 32px}}.panel.active{{display:block}}
.legend{{display:flex;gap:12px;margin-bottom:16px;align-items:center;flex-wrap:wrap;font-size:12px;color:#64748b}}
.badge{{display:inline-block;padding:3px 10px;border-radius:10px;font-size:12px;font-weight:600}}
.badge.buy{{background:#22c55e;color:#fff}}.badge.sell{{background:#ef4444;color:#fff}}
.badge.hold{{background:#f59e0b;color:#fff}}.badge.unknown{{background:#555;color:#fff}}
table{{width:100%;border-collapse:collapse}}
th{{padding:8px 12px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#64748b;text-align:left;border-bottom:2px solid #334155;white-space:nowrap}}
td{{padding:10px 12px;border-bottom:1px solid #1e293b}}
tr.zone-buy{{background:rgba(34,197,94,.05)}}tr.zone-sell{{background:rgba(239,68,68,.05)}}
tr:hover{{background:rgba(255,255,255,.04)!important}}
td.ticker{{font-weight:700;font-size:14px;color:#f1f5f9}}td.sml{{font-size:12px;color:#94a3b8}}
.pos{{color:#22c55e;font-weight:600}}.neg{{color:#ef4444;font-weight:600}}.neu{{color:#94a3b8}}
footer{{padding:14px 32px;background:#0a0f1e;border-top:1px solid #1e293b;font-size:11px;color:#475569;margin-top:32px}}
</style></head><body>
<header><div class="lbl">{now_str}</div><h1>{period} Stock Report</h1>
<div class="sub">Data: {data_updated or 'unknown'}</div></header>
<div class="tabs">
  <button class="tab active" onclick="show('overall',this)">📊 Overall</button>
  <button class="tab" onclick="show('short',this)">⚡ Short-Term</button>
  <button class="tab" onclick="show('mid',this)">📈 Mid-Term</button>
  <button class="tab" onclick="show('long',this)">🏦 Long-Term</button>
</div>
<div id="overall" class="panel active">
  <div class="legend"><span class="badge buy">≥7 Buy</span><span class="badge hold">5–6.9 Hold</span><span class="badge sell">≤4 Sell</span>· Sorted by Overall score</div>
  <table>{th}{sorted_rows('score_overall')}</table></div>
<div id="short" class="panel">
  <div class="legend"><span class="badge buy">≥7 Buy</span><span class="badge sell">≤4 Sell</span>· Short-term (RSI + momentum) · best for trades (days–weeks)</div>
  <table>{th}{sorted_rows('score_short')}</table></div>
<div id="mid" class="panel">
  <div class="legend"><span class="badge buy">≥7 Buy</span><span class="badge sell">≤4 Sell</span>· Mid-term (MA + earnings) · best for swings (weeks–months)</div>
  <table>{th}{sorted_rows('score_mid')}</table></div>
<div id="long" class="panel">
  <div class="legend"><span class="badge buy">≥7 Buy</span><span class="badge sell">≤4 Sell</span>· Long-term (fundamentals + analyst ratings) · best for holds (months+)</div>
  <table>{th}{sorted_rows('score_long')}</table></div>
<footer>S/M/L = Short/Mid/Long &nbsp;·&nbsp; Generated: {now_str} &nbsp;·&nbsp; Data: {data_updated or 'unknown'}</footer>
<script>
function show(id,btn){{
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}}
</script></body></html>"""


# ─────────────────────────────────────────────
# EMAIL SENDER
# ─────────────────────────────────────────────

def send_email(subject, html_body):
    if not GMAIL_APP_PASS:
        print("[Email] ERROR: GMAIL_APP_PASS env var not set.")
        sys.exit(1)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject; msg["From"] = GMAIL_USER; msg["To"] = RECIPIENT
    msg.attach(MIMEText(html_body, "html"))
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASS)
        server.sendmail(GMAIL_USER, RECIPIENT, msg.as_string())
    print(f"[Email] ✅ Sent to {RECIPIENT}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print(f"\n[Cloud Emailer] Starting at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    watchlist = load_watchlist()
    print(f"[Cloud Emailer] Watchlist: {watchlist}")

    snapshot = load_snapshot()
    stocks   = fetch_all_data(watchlist)

    new_buys, new_sells, buy_weakened, sell_cleared, notable = detect_changes(stocks, snapshot)
    all_buys, all_sells, all_holds = get_all_zones(stocks)

    hour       = datetime.utcnow().hour
    is_morning = hour < 18   # UTC: before 6pm UTC = morning PT
    total_chg  = len(new_buys) + len(new_sells) + len(buy_weakened) + len(sell_cleared)
    period     = "🌅 Morning" if is_morning else "🌙 Evening"
    date_str   = datetime.utcnow().strftime("%b %d")
    chg_part   = f" · {total_chg} change{'s' if total_chg!=1 else ''}" if total_chg else " · No changes"
    subject    = f"{period} Report · {date_str}{chg_part} · {len(all_buys)}🟢 {len(all_sells)}🔴"

    data_updated = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    email_html = build_email(stocks, new_buys, new_sells, buy_weakened, sell_cleared,
                              notable, all_buys, all_sells, all_holds, is_morning, data_updated)
    send_email(subject, email_html)

    local_html = build_local_html(stocks, data_updated, is_morning)
    with open(REPORT_FILE, "w") as f:
        f.write(local_html)
    print(f"[Cloud Emailer] 📄 latest_report.html saved")

    save_snapshot(stocks)
    print("[Cloud Emailer] Done.\n")


if __name__ == "__main__":
    main()
