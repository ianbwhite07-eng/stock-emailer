
from flask import Flask, render_template, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import json
import os
import yfinance as yf
import pandas as pd
import threading
import time
from datetime import datetime, timedelta
from config import API_KEY, REFRESH_HOURS, FINNHUB_KEY, ANTHROPIC_KEY, FRED_KEY

app = Flask(__name__)

DATA_FILE          = "stock_data.json"
PORTFOLIO_FILE     = "portfolio.json"
WATCHLIST_FILE     = "watchlist.json"
HISTORY_FILE       = "score_history.json"
FUNDAMENTALS_FILE  = "fundamentals_data.json"
ACCURACY_LOG_FILE  = "accuracy_log.json"
MACRO_FILE         = "macro_data.json"
DAILY_PICKS_FILE   = "daily_picks.json"

# ─────────────────────────────────────────────
# WATCHLIST
# ─────────────────────────────────────────────

def load_watchlist():
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE, "r") as f:
            return json.load(f)
    from config import WATCHLIST
    save_watchlist(WATCHLIST)
    return WATCHLIST

def save_watchlist(watchlist):
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(watchlist, f)

# ─────────────────────────────────────────────
# SCORE HISTORY
# ─────────────────────────────────────────────

def load_score_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    return {}

def save_score_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f)

def record_scores(ticker, short_score, mid_score, long_score, overall_score, price=None):
    history = load_score_history()
    if ticker not in history:
        history[ticker] = []
    today = datetime.now().strftime("%Y-%m-%d")
    new_entry = {
        "date": today,
        "price": round(price, 2) if isinstance(price, (int, float)) else None,
        "short": short_score,
        "mid": mid_score,
        "long": long_score,
        "overall": overall_score
    }
    entries = history[ticker]
    if not entries:
        entries.append(new_entry)
    else:
        last = entries[-1]
        if last["date"] == today:
            entries[-1] = new_entry  # update today's entry in place
        else:
            entries.append(new_entry)  # always add a new entry for a new day
    history[ticker] = entries
    save_score_history(history)

# ─────────────────────────────────────────────
# PREDICTION ACCURACY
# ─────────────────────────────────────────────

def record_prediction(ticker, stock_data):
    """Save today's price + active signals for later outcome evaluation. Skip if already logged today."""
    log = {}
    if os.path.exists(ACCURACY_LOG_FILE):
        try:
            with open(ACCURACY_LOG_FILE) as f:
                log = json.load(f)
        except Exception:
            log = {}
    if ticker not in log:
        log[ticker] = []
    today = datetime.now().strftime("%Y-%m-%d")
    if any(e["date"] == today for e in log[ticker]):
        return
    price = stock_data.get("price")
    if not isinstance(price, (int, float)):
        return
    rsi = stock_data.get("rsi")
    signals = {
        "rsi_high":            rsi is not None and rsi >= 55,
        "price_5d_positive":   (stock_data.get("change_5d") or 0) > 0,
        "short_interest_high": (stock_data.get("short_percent") or 0) > 0.25,
        "beta_high":           (stock_data.get("beta") or 0) > 2.5,
        "sentiment_positive":  stock_data.get("sentiment_label") == "positive",
        "ma_bullish":          stock_data.get("ma_trend") == "Uptrend",
        "earnings_beats":      "Beat" in (stock_data.get("earnings_trend") or ""),
        "revenue_growing":     (stock_data.get("revenue_growth") or 0) > 0.15,
    }
    entry = {
        "date":           today,
        "price":          round(price, 2),
        "score_short":    stock_data.get("score_short"),
        "score_mid":      stock_data.get("score_mid"),
        "score_overall":  stock_data.get("score_overall"),
        "signals":        signals,
        "outcome_price":  None,
        "outcome_correct": None,
    }
    log[ticker].append(entry)
    with open(ACCURACY_LOG_FILE, "w") as f:
        json.dump(log, f)

def evaluate_predictions():
    """Fill in outcome_price/outcome_correct for entries that are ≥7 calendar days old (≈5 trading days)."""
    if not os.path.exists(ACCURACY_LOG_FILE):
        return
    with open(ACCURACY_LOG_FILE) as f:
        log = json.load(f)
    cutoff = datetime.now() - timedelta(days=7)
    tickers_to_fetch = [t for t, entries in log.items()
                        if any(e.get("outcome_price") is None and
                               _parse_date(e["date"]) is not None and
                               _parse_date(e["date"]) <= cutoff
                               for e in entries)]
    if not tickers_to_fetch:
        return
    print(f"[Accuracy] Evaluating outcomes for: {tickers_to_fetch}")
    try:
        raw   = yf.download(tickers_to_fetch, period="5d", progress=False, auto_adjust=True)
        close = raw["Close"]
        if isinstance(close, pd.Series):
            close = close.to_frame(name=tickers_to_fetch[0])
        if isinstance(close.columns, pd.MultiIndex):
            close.columns = close.columns.get_level_values(-1)
    except Exception as e:
        print(f"[Accuracy] yfinance error: {e}")
        return
    changed = False
    for ticker, entries in log.items():
        if ticker not in close.columns:
            continue
        series = close[ticker].dropna()
        if series.empty:
            continue
        current_price = float(series.iloc[-1])
        for entry in entries:
            if entry.get("outcome_price") is not None:
                continue
            entry_date = _parse_date(entry["date"])
            if entry_date is None or entry_date > cutoff:
                continue
            entry["outcome_price"] = round(current_price, 2)
            score   = entry.get("score_overall")
            price_up = current_price > entry["price"]
            if score is not None and score >= 7:
                entry["outcome_correct"] = price_up
            elif score is not None and score <= 4:
                entry["outcome_correct"] = not price_up
            else:
                entry["outcome_correct"] = None  # neutral score — excluded from stats
            changed = True
    if changed:
        with open(ACCURACY_LOG_FILE, "w") as f:
            json.dump(log, f)
        print("[Accuracy] Outcomes updated.")

def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except Exception:
        return None

def build_signal_stats():
    """Aggregate per-signal hit rates across all tickers. Only returns signals with ≥10 observations."""
    if not os.path.exists(ACCURACY_LOG_FILE):
        return {}
    with open(ACCURACY_LOG_FILE) as f:
        log = json.load(f)
    signal_names = [
        "rsi_high", "price_5d_positive", "short_interest_high", "beta_high",
        "sentiment_positive", "ma_bullish", "earnings_beats", "revenue_growing",
    ]
    stats = {s: {"correct": 0, "total": 0} for s in signal_names}
    for ticker, entries in log.items():
        for entry in entries:
            if entry.get("outcome_correct") is None:
                continue
            correct = entry["outcome_correct"]
            signals = entry.get("signals", {})
            for signal in signal_names:
                if signals.get(signal):
                    stats[signal]["total"] += 1
                    if correct:
                        stats[signal]["correct"] += 1
    return {
        signal: {
            "correct": d["correct"],
            "total":   d["total"],
            "pct":     round(d["correct"] / d["total"] * 100, 1),
        }
        for signal, d in stats.items() if d["total"] >= 10
    }

# ─────────────────────────────────────────────
# MACRO DATA (FRED)
# ─────────────────────────────────────────────

FRED_SERIES = {
    "VIXCLS":       "VIX",
    "BAMLH0A0HYM2": "HY Credit Spread",
    "T10Y2Y":       "10Y-2Y Spread",
    "FEDFUNDS":     "Fed Funds Rate",
}

def fetch_macro_data():
    """Fetch key macro indicators from FRED and cache to macro_data.json."""
    result = {}
    for series_id, label in FRED_SERIES.items():
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
                    current = float(obs[0]["value"])
                    prev    = float(obs[1]["value"]) if len(obs) > 1 else current
                    result[series_id] = {
                        "value": current, "prev": prev,
                        "label": label, "date": obs[0]["date"],
                    }
                    print(f"[FRED] {series_id}: {current} (prev {prev})")
            else:
                print(f"[FRED] {series_id}: HTTP {r.status_code}")
        except Exception as e:
            print(f"[FRED] {series_id}: {e}")
    result["_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(MACRO_FILE, "w") as f:
        json.dump(result, f)
    print("[FRED] Macro data updated.")
    return result

def load_macro():
    if os.path.exists(MACRO_FILE):
        with open(MACRO_FILE) as f:
            return json.load(f)
    return {}

def macro_stale():
    d = load_macro()
    updated = d.get("_updated", "")
    try:
        return datetime.now() - datetime.strptime(updated, "%Y-%m-%d %H:%M") > timedelta(hours=24)
    except Exception:
        return True

def get_macro_adjustments():
    """Return (vix_adj, credit_adj) based on current FRED data."""
    macro   = load_macro()
    vix_val = (macro.get("VIXCLS") or {}).get("value")
    hy_val  = (macro.get("BAMLH0A0HYM2") or {}).get("value")
    t10y2y  = (macro.get("T10Y2Y") or {}).get("value")
    if vix_val is not None:
        if   vix_val > 30: vix_adj = -1
        elif vix_val < 15: vix_adj =  1
        else:              vix_adj =  0
    else:
        vix_adj = 0
    # Only apply whichever credit/curve signal is triggered first
    if hy_val is not None and hy_val > 500:
        credit_adj = -1
    elif t10y2y is not None and t10y2y < -0.5:
        credit_adj = -1
    else:
        credit_adj = 0
    return vix_adj, credit_adj

# ─────────────────────────────────────────────
# FUNDAMENTALS (yfinance)
# ─────────────────────────────────────────────

def load_fundamentals():
    if os.path.exists(FUNDAMENTALS_FILE):
        with open(FUNDAMENTALS_FILE) as f:
            return json.load(f)
    return {}

def fetch_all_fundamentals(watchlist):
    print("[Fundamentals] Fetching yfinance fundamentals...")
    existing = load_fundamentals()
    data = {k: v for k, v in existing.items() if k != "_updated"}
    for ticker in watchlist:
        try:
            t_obj = yf.Ticker(ticker)
            info  = t_obj.info

            # yfinance fundamentals
            entry = {
                "short_percent":         round(info.get("shortPercentOfFloat") or 0, 4),
                "revenue_growth":        info.get("revenueGrowth"),
                "earnings_growth":       info.get("earningsGrowth"),
                "forward_pe":            info.get("forwardPE"),
                "trailing_pe":           info.get("trailingPE"),
                "held_pct_institutions": info.get("heldPercentInstitutions"),
                "beta":                  info.get("beta"),
                "quote_type":            info.get("quoteType", "EQUITY"),
            }

            # yfinance recent news headlines for display
            try:
                raw_news = t_obj.news or []
                yf_news  = []
                for n in raw_news[:5]:
                    content = n.get("content") or n
                    title   = content.get("title") or n.get("title", "")
                    url     = (content.get("canonicalUrl") or {}).get("url") or n.get("link", "")
                    src     = (content.get("provider") or {}).get("displayName") or n.get("publisher", "")
                    if title:
                        yf_news.append({"title": title, "url": url, "source": src})
                entry["yf_news"] = yf_news
            except Exception:
                entry["yf_news"] = existing.get(ticker, {}).get("yf_news", [])

            # Finnhub insider transactions
            time.sleep(0.5)
            insider = fetch_insider_signal(ticker)
            entry["insider_signal"] = insider["signal"]
            entry["insider_net"]    = insider["net"]
            entry["insider_buys"]   = insider["buys"]
            entry["insider_sells"]  = insider["sells"]

            # Finnhub company news + VADER sentiment
            time.sleep(0.5)
            news_data = fetch_news_sentiment(ticker)
            entry["sentiment_score"] = news_data["sentiment_score"]
            entry["sentiment_label"] = news_data["sentiment_label"]
            entry["news_headlines"]  = news_data["headlines"]

            data[ticker] = entry
            print(f"[Fundamentals] {ticker}: short={entry['short_percent']:.1%} "
                  f"revGrowth={entry['revenue_growth']} insider={insider['signal']} "
                  f"sentiment={news_data['sentiment_label']}({news_data['sentiment_score']})")
        except Exception as e:
            print(f"[Fundamentals] {ticker}: {e}")
            data[ticker] = existing.get(ticker, {})
        time.sleep(0.3)
    data["_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(FUNDAMENTALS_FILE, "w") as f:
        json.dump(data, f)
    print(f"[Fundamentals] Done. {len(watchlist)} tickers updated.")
    return data

def fundamentals_stale():
    d = load_fundamentals()
    updated = d.get("_updated", "")
    try:
        return datetime.now() - datetime.strptime(updated, "%Y-%m-%d %H:%M") > timedelta(hours=24)
    except Exception:
        return True

# ─────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────

def score_short(s):
    score = 5
    rsi = s.get("rsi")
    if rsi is not None:
        # Cap RSI upside at +2 so a single overbought reading can't max out the score alone
        if   rsi >  80: score += 2
        elif rsi >  70: score += 1
        elif rsi <= 25: score -= 3
        elif rsi <= 30: score -= 2
        elif rsi <= 40: score -= 1
    # Require stronger price action (>8%) to earn the full +2
    c5 = s.get("change_5d", 0) or 0
    if   c5 >  8: score += 2
    elif c5 >  3: score += 1
    elif c5 < -8: score -= 2
    elif c5 < -3: score -= 1
    # High short interest = elevated downside pressure
    short_pct = s.get("short_percent")
    if short_pct is not None and short_pct > 0.25:
        score -= 1
    # Extreme beta = amplified volatility risk for short-term holds
    beta = s.get("beta")
    if beta is not None and beta > 2.5:
        score -= 1
    # News sentiment over last 14 days (only meaningful when momentum supports it)
    sentiment = s.get("sentiment_label")
    if   sentiment == "positive" and (rsi or 50) > 50: score += 1
    elif sentiment == "negative": score -= 1
    # Macro: VIX environment
    score += s.get("macro_vix_adj", 0) or 0
    return max(0, min(10, score))

def score_mid(s):
    score = 5
    ma20, ma50 = s.get("ma20"), s.get("ma50")
    if ma20 and ma50 and ma50 > 0:
        gap = (ma20 - ma50) / ma50 * 100
        if   gap >  8: score += 2
        elif gap >  5: score += 1   # raised from >3 — mild uptrend no longer earns a point
        elif gap > -3: score -= 0
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
        if   rev >  0.25: score += 1   # raised from >0.20 — 25%+ growth for the bonus
        elif rev < -0.10: score -= 1
    return max(0, min(10, score))

def score_long(s):
    # ETFs don't have analyst ratings, insider activity, or forward P/E —
    # use a separate path so broad market funds aren't unfairly penalized.
    if s.get("is_etf"):
        score = 7  # ETFs are structurally solid long-term instruments
        ma20, ma50 = s.get("ma20"), s.get("ma50")
        if ma20 and ma50 and ma50 > 0:
            gap = (ma20 - ma50) / ma50 * 100
            if   gap >  2: score += 1
            elif gap < -5: score -= 2
            elif gap < -2: score -= 1
        beta = s.get("beta")
        if beta is not None:
            if   beta <= 1.2: score += 1   # broad-market / low-vol
            elif beta >  2.0: score -= 1   # high-vol sector ETF
        score += s.get("macro_credit_adj", 0) or 0
        return max(0, min(10, score))

    score = 5
    # Analyst rating: reduced weight so a single "Strong Buy" can't get near 10 alone
    rating_pts = {"Strong Buy": 2, "Buy": 1, "Hold": 0, "Sell": -2, "Strong Sell": -3}
    score += rating_pts.get(s.get("rating", "N/A"), 0)
    # Forward P/E: negative = cash burn; cheap = value; expensive = risk
    fpe = s.get("forward_pe")
    if fpe is not None:
        if   fpe <  0:  score -= 2
        elif fpe < 12:  score += 1
        elif fpe > 50:  score -= 1
        elif fpe > 80:  score -= 2
    # Institutional ownership: smart money conviction
    inst = s.get("held_pct_institutions")
    if inst is not None:
        if   inst > 0.75: score += 1
        elif inst < 0.20: score -= 1
    # Net insider buying/selling last 90 days
    insider = s.get("insider_signal")
    if   insider == "buying":  score += 1
    elif insider == "selling": score -= 1
    # Macro: credit stress / yield curve
    score += s.get("macro_credit_adj", 0) or 0
    return max(0, min(10, score))

def score_overall(s):
    return round(score_short(s)*0.25 + score_mid(s)*0.40 + score_long(s)*0.35, 1)

# ─────────────────────────────────────────────
# PRICE FETCHING
# ─────────────────────────────────────────────

def _parse_yf_download(raw, watchlist):
    """Extract per-ticker price dicts and series from a yfinance bulk download result."""
    prices     = {}
    raw_series = {}
    empty      = {"price": "N/A", "change_1d": 0, "change_5d": 0, "change_1mo": 0}
    close = raw["Close"]
    if isinstance(close.columns, pd.MultiIndex):
        close.columns = close.columns.get_level_values(-1)
    if isinstance(close, pd.Series):
        close = close.to_frame(name=watchlist[0])
    clean = close.dropna(how="all")
    for ticker in watchlist:
        try:
            series   = clean[ticker].dropna()
            current  = float(series.iloc[-1])
            prev_1d  = float(series.iloc[-2])  if len(series) >= 2  else current
            prev_5d  = float(series.iloc[-6])  if len(series) >= 6  else float(series.iloc[0])
            prev_1mo = float(series.iloc[-22]) if len(series) >= 22 else float(series.iloc[0])
            prices[ticker] = {
                "price":      round(current, 2),
                "change_1d":  round(((current - prev_1d)  / prev_1d)  * 100, 2) if prev_1d  else 0,
                "change_5d":  round(((current - prev_5d)  / prev_5d)  * 100, 2) if prev_5d  else 0,
                "change_1mo": round(((current - prev_1mo) / prev_1mo) * 100, 2) if prev_1mo else 0,
            }
            raw_series[ticker] = series
        except Exception:
            prices[ticker] = empty.copy()
    return prices, raw_series

def fetch_prices(watchlist):
    empty = {"price": "N/A", "change_1d": 0, "change_5d": 0, "change_1mo": 0}
    for attempt in range(2):
        try:
            raw    = yf.download(watchlist, period="3mo", progress=False, auto_adjust=True)
            prices, raw_series = _parse_yf_download(raw, watchlist)
            na_count = sum(1 for p in prices.values() if p["price"] == "N/A")
            if na_count > len(watchlist) * 0.5 and attempt == 0:
                print(f"[yfinance] {na_count}/{len(watchlist)} tickers N/A on attempt 1 — retrying...")
                time.sleep(3)
                continue
            print(f"[yfinance] Got prices for {len(watchlist)-na_count}/{len(watchlist)} tickers")
            return prices, raw_series
        except Exception as e:
            print(f"[yfinance] Error (attempt {attempt+1}): {e}")
            if attempt == 0:
                time.sleep(3)
    return {t: empty.copy() for t in watchlist}, {}

# ─────────────────────────────────────────────
# SHORT TERM
# ─────────────────────────────────────────────

def calculate_rsi(series, period=14):
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs       = avg_gain / avg_loss
    rsi      = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 1)

def get_short_term(series):
    try:
        if len(series) < 16:
            return {"rsi": None, "rsi_label": "N/A"}
        rsi = calculate_rsi(series)
        label = "Bullish" if rsi >= 60 else "Bearish" if rsi <= 40 else "Neutral"
        return {"rsi": rsi, "rsi_label": label}
    except Exception as e:
        print(f"[RSI] Error: {e}")
        return {"rsi": None, "rsi_label": "N/A"}

# ─────────────────────────────────────────────
# MID TERM
# ─────────────────────────────────────────────

def get_ma_trend(series):
    try:
        if len(series) < 50:
            return {"ma20": None, "ma50": None, "ma_trend": "N/A"}
        ma20  = round(float(series.rolling(20).mean().iloc[-1]), 2)
        ma50  = round(float(series.rolling(50).mean().iloc[-1]), 2)
        trend = "Uptrend" if ma20 > ma50 else "Downtrend" if ma20 < ma50 else "Flat"
        return {"ma20": ma20, "ma50": ma50, "ma_trend": trend}
    except Exception as e:
        print(f"[MA Trend] Error: {e}")
        return {"ma20": None, "ma50": None, "ma_trend": "N/A"}

def fetch_earnings_trend(ticker):
    try:
        r = requests.get("https://finnhub.io/api/v1/stock/earnings",
                         params={"symbol": ticker, "token": FINNHUB_KEY}, timeout=10)
        print(f"[Finnhub Earnings] {ticker}: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            if not data: return "N/A"
            beats, misses = 0, 0
            for q in data[:4]:
                actual   = q.get("actual")
                estimate = q.get("estimate")
                if actual is not None and estimate is not None:
                    if actual >= estimate: beats += 1
                    else: misses += 1
            total = beats + misses
            if total == 0: return "N/A"
            if beats  == total: return f"Beat x{beats}"
            if misses == total: return f"Missed x{misses}"
            return f"Mixed ({beats}B/{misses}M)"
    except Exception as e:
        print(f"[Finnhub Earnings] Error for {ticker}: {e}")
    return "N/A"

# ─────────────────────────────────────────────
# LONG TERM
# ─────────────────────────────────────────────

def fetch_analyst_rating(ticker):
    try:
        r = requests.get("https://finnhub.io/api/v1/stock/recommendation",
                         params={"symbol": ticker, "token": FINNHUB_KEY}, timeout=10)
        print(f"[Finnhub Rating] {ticker}: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            if data:
                latest = data[0]
                counts = {
                    "Strong Buy":  latest.get("strongBuy", 0),
                    "Buy":         latest.get("buy", 0),
                    "Hold":        latest.get("hold", 0),
                    "Sell":        latest.get("sell", 0),
                    "Strong Sell": latest.get("strongSell", 0),
                }
                best  = max(counts, key=counts.get)
                total = sum(counts.values())
                return {"rating": best if counts[best] > 0 else "N/A", "breakdown": counts, "total_analysts": total}
    except Exception as e:
        print(f"[Finnhub Rating] Error for {ticker}: {e}")
    return {"rating": "N/A", "breakdown": {}, "total_analysts": 0}

def fetch_price_target(ticker):
    try:
        r = requests.get("https://finnhub.io/api/v1/stock/price-target",
                         params={"symbol": ticker, "token": FINNHUB_KEY}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            mean = data.get("targetMean")
            high = data.get("targetHigh")
            low  = data.get("targetLow")
            if mean and mean > 0:
                return {
                    "target_mean": round(mean, 2),
                    "target_high": round(high, 2) if high else None,
                    "target_low":  round(low,  2) if low  else None,
                }
    except Exception as e:
        print(f"[Finnhub PT] {ticker}: {e}")
    return {"target_mean": None, "target_high": None, "target_low": None}

def fetch_insider_signal(ticker):
    """Net insider buying/selling over the last 90 days via Finnhub."""
    try:
        from_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        to_date   = datetime.now().strftime("%Y-%m-%d")
        r = requests.get("https://finnhub.io/api/v1/stock/insider-transactions",
                         params={"symbol": ticker, "from": from_date, "to": to_date, "token": FINNHUB_KEY},
                         timeout=10)
        if r.status_code == 200:
            txns = r.json().get("data") or []
            net  = sum(t.get("change", 0) for t in txns)
            buys = sum(t.get("change", 0) for t in txns if t.get("change", 0) > 0)
            sells= abs(sum(t.get("change", 0) for t in txns if t.get("change", 0) < 0))
            if   net >  1000: signal = "buying"
            elif net < -1000: signal = "selling"
            else:             signal = "neutral"
            return {"signal": signal, "net": net, "buys": buys, "sells": sells}
    except Exception as e:
        print(f"[Insider] {ticker}: {e}")
    return {"signal": "neutral", "net": 0, "buys": 0, "sells": 0}

def fetch_news_sentiment(ticker):
    """Recent company headlines from Finnhub + VADER compound sentiment score."""
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        from_date = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
        to_date   = datetime.now().strftime("%Y-%m-%d")
        r = requests.get("https://finnhub.io/api/v1/company-news",
                         params={"symbol": ticker, "from": from_date, "to": to_date, "token": FINNHUB_KEY},
                         timeout=10)
        if r.status_code == 200:
            articles = r.json()[:20]
            if not articles:
                return {"sentiment_score": None, "sentiment_label": "neutral", "headlines": []}
            analyzer = SentimentIntensityAnalyzer()
            scores   = [analyzer.polarity_scores(a["headline"])["compound"]
                        for a in articles if a.get("headline")]
            avg = round(sum(scores) / len(scores), 3) if scores else 0
            if   avg >  0.10: label = "positive"
            elif avg < -0.10: label = "negative"
            else:             label = "neutral"
            headlines = [{"title": a["headline"], "url": a.get("url", ""), "source": a.get("source", "")}
                         for a in articles[:5] if a.get("headline")]
            return {"sentiment_score": avg, "sentiment_label": label, "headlines": headlines}
    except Exception as e:
        print(f"[NewsSentiment] {ticker}: {e}")
    return {"sentiment_score": None, "sentiment_label": "neutral", "headlines": []}

# ─────────────────────────────────────────────
# NEWS
# ─────────────────────────────────────────────

_news_cache = {"articles": [], "updated": None}

def fetch_market_news():
    global _news_cache
    # Return cached result if less than 15 minutes old — prevents double-hitting Finnhub
    # when /api/news and /api/market-overview are called simultaneously
    if _news_cache["updated"] and (datetime.now() - _news_cache["updated"]).total_seconds() < 900:
        return _news_cache["articles"]
    try:
        r = requests.get("https://finnhub.io/api/v1/news",
                         params={"category": "general", "token": FINNHUB_KEY}, timeout=10)
        if r.status_code == 200:
            articles = r.json()[:10]
            result = [{
                "headline": a.get("headline", ""),
                "source":   a.get("source", ""),
                "url":      a.get("url", ""),
                "summary":  a.get("summary", "")[:200] + "..." if a.get("summary") else "",
                "datetime": datetime.fromtimestamp(a.get("datetime", 0)).strftime("%Y-%m-%d %H:%M") if a.get("datetime") else ""
            } for a in articles if a.get("headline")]
            _news_cache = {"articles": result, "updated": datetime.now()}
            return result
        print(f"[News] Finnhub returned {r.status_code}")
    except Exception as e:
        print(f"[News] Error: {e}")
    return _news_cache["articles"]  # return stale cache on error rather than empty

# ─────────────────────────────────────────────
# MAIN REFRESH
# ─────────────────────────────────────────────

def fetch_stock_data():
    print(f"\n[Refresh] Starting at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    watchlist = load_watchlist()

    existing_data = {}
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            existing_data = json.load(f)

    # Load score history once — used to compare against yesterday's scores
    score_history = load_score_history()
    today_str = datetime.now().strftime("%Y-%m-%d")

    print("[Refresh] Fetching prices via yfinance...")
    prices, raw_series = fetch_prices(watchlist)

    fundamentals = load_fundamentals()
    vix_adj, credit_adj = get_macro_adjustments()
    print("[Refresh] Fetching Finnhub data...")
    stocks = []
    for ticker in watchlist:
        price_info = prices.get(ticker, {"price": "N/A", "change_1d": 0, "change_5d": 0, "change_1mo": 0})
        series     = raw_series.get(ticker)
        prev       = existing_data.get(ticker, {})

        # If yfinance returned N/A but we have a previous good price, keep it
        if price_info["price"] == "N/A" and isinstance(prev.get("price"), (int, float)):
            price_info = {
                "price":      prev["price"],
                "change_1d":  prev.get("change_1d", 0),
                "change_5d":  prev.get("change_5d", 0),
                "change_1mo": prev.get("change_1mo", 0),
            }

        short_term = get_short_term(series) if series is not None else {"rsi": None, "rsi_label": "N/A"}
        ma_data    = get_ma_trend(series)   if series is not None else {"ma20": None, "ma50": None, "ma_trend": "N/A"}

        fetched_earnings = fetch_earnings_trend(ticker)
        earnings_trend   = fetched_earnings if fetched_earnings != "N/A" else prev.get("earnings_trend", "N/A")

        fetched_rating_data = fetch_analyst_rating(ticker)
        fetched_rating      = fetched_rating_data["rating"]
        previous_rating     = prev.get("rating", "N/A")

        if fetched_rating == "N/A" and previous_rating != "N/A":
            current_rating   = previous_rating
            rating_breakdown = prev.get("rating_breakdown", {})
            total_analysts   = prev.get("total_analysts", 0)
            rating_updated   = prev.get("rating_updated", "unknown")
        else:
            current_rating   = fetched_rating
            rating_breakdown = fetched_rating_data["breakdown"]
            total_analysts   = fetched_rating_data["total_analysts"]
            rating_updated   = datetime.now().strftime("%Y-%m-%d %H:%M") if fetched_rating != "N/A" else prev.get("rating_updated", "unknown")

        alert = (current_rating != previous_rating and previous_rating != "N/A" and current_rating != "N/A")

        time.sleep(2)

        fund = fundamentals.get(ticker, {})
        stock_entry = {
            "ticker":                   ticker,
            "price":                    price_info["price"],
            "change_1d":                price_info["change_1d"],
            "change_5d":                price_info["change_5d"],
            "change_1mo":               price_info["change_1mo"],
            "rsi":                      short_term["rsi"],
            "rsi_label":                short_term["rsi_label"],
            "ma_trend":                 ma_data["ma_trend"],
            "ma20":                     ma_data["ma20"],
            "ma50":                     ma_data["ma50"],
            "earnings_trend":           earnings_trend,
            "rating":                   current_rating,
            "previous_rating":          previous_rating,
            "rating_breakdown":         rating_breakdown,
            "total_analysts":           total_analysts,
            "rating_updated":           rating_updated,
            "alert":                    alert,
            "updated":                  datetime.now().strftime("%Y-%m-%d %H:%M"),
            # Fundamentals (refreshed daily from yfinance)
            "short_percent":            fund.get("short_percent"),
            "revenue_growth":           fund.get("revenue_growth"),
            "earnings_growth":          fund.get("earnings_growth"),
            "forward_pe":               fund.get("forward_pe"),
            "trailing_pe":              fund.get("trailing_pe"),
            "held_pct_institutions":    fund.get("held_pct_institutions"),
            "beta":                     fund.get("beta"),
            # Insider transactions (Finnhub, daily)
            "insider_signal":           fund.get("insider_signal", "neutral"),
            "insider_net":              fund.get("insider_net", 0),
            "insider_buys":             fund.get("insider_buys", 0),
            "insider_sells":            fund.get("insider_sells", 0),
            # News sentiment (Finnhub + VADER, daily)
            "sentiment_score":          fund.get("sentiment_score"),
            "sentiment_label":          fund.get("sentiment_label", "neutral"),
            "news_headlines":           fund.get("news_headlines", []),
            # yfinance news for display
            "yf_news":                  fund.get("yf_news", []),
            # Macro environment (same for all tickers in this refresh)
            "macro_vix_adj":            vix_adj,
            "macro_credit_adj":         credit_adj,
            "is_etf":                   fund.get("quote_type") == "ETF",
        }

        new_short   = score_short(stock_entry)
        new_mid     = score_mid(stock_entry)
        new_long    = score_long(stock_entry)
        new_overall = score_overall(stock_entry)

        # Find the most recent history entry before today to use as the comparison baseline.
        # This means score_changes reflects day-over-day shifts, not refresh-over-refresh.
        ticker_history = score_history.get(ticker, [])
        prev_day_entry = next(
            (e for e in reversed(ticker_history) if e.get("date", "") < today_str),
            None
        )
        prev_short   = prev_day_entry["short"]   if prev_day_entry else None
        prev_mid     = prev_day_entry["mid"]     if prev_day_entry else None
        prev_long    = prev_day_entry["long"]    if prev_day_entry else None
        prev_overall = prev_day_entry["overall"] if prev_day_entry else None

        score_changes = []
        for dim, new_val, old_val in [
            ("short",   new_short,   prev_short),
            ("mid",     new_mid,     prev_mid),
            ("long",    new_long,    prev_long),
            ("overall", new_overall, prev_overall),
        ]:
            if old_val is not None and new_val != old_val:
                score_changes.append({"dim": dim, "from": old_val, "to": new_val})

        stock_entry["score_short"]   = new_short
        stock_entry["score_mid"]     = new_mid
        stock_entry["score_long"]    = new_long
        stock_entry["score_overall"] = new_overall
        stock_entry["score_changes"] = score_changes

        current_price = price_info["price"] if isinstance(price_info["price"], (int, float)) else None
        record_scores(ticker, new_short, new_mid, new_long, new_overall, price=current_price)
        try:
            record_prediction(ticker, stock_entry)
        except Exception as e:
            print(f"[Accuracy] record_prediction error for {ticker}: {e}")
        stocks.append(stock_entry)

    with open(DATA_FILE, "w") as f:
        json.dump({s["ticker"]: s for s in stocks}, f)

    evaluate_predictions()
    print(f"[Refresh] Done. {len(stocks)} stocks updated.\n")

# ─────────────────────────────────────────────
# PORTFOLIO
# ─────────────────────────────────────────────

def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE, "r") as f:
            return json.load(f)
    return {"cash": 100.0, "holdings": {}, "history": []}

def save_portfolio(portfolio):
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(portfolio, f)




RECS_CACHE_FILE = "recs_cache.json"

# Broad small/mid-cap scan universe. Watchlist members are filtered out at scan time.
UNIVERSE = [
    # Current watchlist (filtered out when on watchlist, available as candidates when not)
    'ALGT','ECPG','JILL','ENVA','OMF','FCFS','EZPW','ATLC','LC','AUNA',
    'BTSG','ASTS','BE','LUMN','NWPX','LCUT','AFYA','ALTO',
    'PRAA','INVA','SATS','IRDM','PAHC',
    # Consumer / specialty finance
    'CACC','RM','WRLD','NAVI','SLM','GDOT','OPRT',
    # Healthcare
    'HIMS','ACAD','ADUS','FLGT','HURN','RXRX',
    # Tech / telecom
    'ADTN','NTGR','SPOK','ATNI','TALK',
    # Industrials / other
    'AMRC','LUNR','ESEA','XPOF','PAYO','PRTS',
]


def generate_stock_overview(ticker, short_score, mid_score, long_score, overall_score, rsi_label, ma_trend, earnings_trend, rating):
    try:
        best_term = "short-term" if short_score >= mid_score and short_score >= long_score else                     "mid-term"   if mid_score  >= short_score and mid_score  >= long_score else "long-term"
        prompt = (
            f"Stock: {ticker}. Scores — short: {short_score}/10, mid: {mid_score}/10, long: {long_score}/10, overall: {overall_score}/10. "
            f"Signals: RSI {rsi_label}, MA trend {ma_trend}, earnings {earnings_trend}, analyst rating {rating}. "
            f"Write exactly 3 sentences with no headers or labels: "
            f"(1) What kind of company {ticker} is — industry, what they do, and why it looks like a compelling investment right now based on these signals. "
            f"(2) Brief current market or sector context that makes this stock interesting at this moment. "
            f"(3) Whether this is best as a short-term trade, mid-term hold, or long-term position based on the scores, and why in one sentence. "
            f"Be specific and concise. No fluff."
        )
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 220, "messages": [{"role": "user", "content": prompt}]},
            timeout=15
        )
        if r.status_code == 200:
            return r.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"[AI Overview] {ticker}: {e}")
    return ""

def build_recommendations():
    print("[Recs] Starting background recommendation scan...")
    watchlist = load_watchlist()
    candidates = [t for t in UNIVERSE if t not in watchlist]
    try:
        raw   = yf.download(candidates, period="3mo", progress=False, auto_adjust=True)
        close = raw["Close"]
        if isinstance(close, pd.Series):
            close = close.to_frame(name=candidates[0])
        clean = close.dropna(how="all")
    except Exception as e:
        print(f"[Recs] yfinance error: {e}")
        return
    scored = []
    for ticker in candidates:
        try:
            series = clean[ticker].dropna() if ticker in clean.columns else None
            if series is None or len(series) < 6:
                continue
            st  = get_short_term(series)
            ma  = get_ma_trend(series)
            entry = {
                "ticker":         ticker,
                "price":          round(float(series.iloc[-1]), 2),
                "change_5d":      round(((float(series.iloc[-1]) - float(series.iloc[-6])) / float(series.iloc[-6])) * 100, 2),
                "rsi_label":      st["rsi_label"],
                "rsi":            st["rsi"],
                "ma_trend":       ma["ma_trend"],
                "ma20":           ma["ma20"],
                "ma50":           ma["ma50"],
                "earnings_trend": "N/A",
                "rating":         "N/A",
                "total_analysts": 0,
                "target_mean":    None,
                "target_upside":  None,
            }
            entry["overall"] = score_overall(entry)
            if entry["overall"] >= 6:
                scored.append(entry)
        except Exception:
            continue
    top = sorted(scored, key=lambda x: x["overall"], reverse=True)[:10]
    results = []
    for s in top:
        try:
            s["earnings_trend"] = fetch_earnings_trend(s["ticker"])
            rd = fetch_analyst_rating(s["ticker"])
            s["rating"]         = rd["rating"]
            s["total_analysts"] = rd["total_analysts"]
            pt = fetch_price_target(s["ticker"])
            s["target_mean"]   = pt["target_mean"]
            s["target_high"]   = pt.get("target_high")
            s["target_low"]    = pt.get("target_low")
            if pt["target_mean"] and s["price"] and s["price"] > 0:
                s["target_upside"] = round((pt["target_mean"] - s["price"]) / s["price"] * 100, 1)
            s["overall"] = score_overall(s)
            time.sleep(2)
        except Exception:
            pass
        reasons = []
        if s["rsi_label"] == "Bullish":                      reasons.append("RSI bullish momentum")
        if s["ma_trend"]  == "Uptrend":                      reasons.append("20MA above 50MA")
        if "Beat" in (s["earnings_trend"] or ""):            reasons.append(f"earnings {s['earnings_trend']}")
        if s["rating"] in ["Strong Buy","Buy"]:              reasons.append(f"{s['rating']} from {s['total_analysts']} analysts")
        if s["change_5d"] > 0:                               reasons.append(f"up {s['change_5d']}% this week")
        if s.get("target_upside") and s["target_upside"] > 10: reasons.append(f"+{s['target_upside']}% to analyst target")
        short_s  = score_short(s)
        mid_s    = score_mid(s)
        long_s   = score_long(s)
        overall_s = round(score_overall(s), 1)
        overview = generate_stock_overview(
            s["ticker"], short_s, mid_s, long_s, overall_s,
            s["rsi_label"], s["ma_trend"], s["earnings_trend"], s["rating"]
        )
        results.append({
            "ticker":        s["ticker"],
            "price":         s["price"],
            "overall":       overall_s,
            "short":         short_s,
            "mid":           mid_s,
            "long":          long_s,
            "reasons":       reasons,
            "overview":      overview,
            "target_mean":   s.get("target_mean"),
            "target_upside": s.get("target_upside"),
        })
    results = sorted(results, key=lambda x: x["overall"], reverse=True)
    with open(RECS_CACHE_FILE, "w") as f:
        json.dump({"updated": datetime.now().strftime("%Y-%m-%d %H:%M"), "results": results}, f)
    print(f"[Recs] Done. {len(results)} recommendations cached.")

@app.route("/api/macro")
def get_macro():
    return jsonify(load_macro())

@app.route("/api/accuracy")
def get_accuracy():
    return jsonify(build_signal_stats())

@app.route("/api/accuracy/analysis")
def get_accuracy_analysis():
    stats = build_signal_stats()
    if not stats:
        return jsonify({"analysis": ""})
    table_lines = []
    for signal, data in sorted(stats.items(), key=lambda x: -x[1]["pct"]):
        table_lines.append(f"{signal}: {data['pct']}% hit rate ({data['correct']}/{data['total']} predictions)")
    table_str = "\n".join(table_lines)
    prompt = (
        f"These are signal hit rates from a stock scoring system. Each row shows: "
        f"when a signal was active in a stock with score ≥7 or ≤4, how often did the price "
        f"move in the predicted direction 5 trading days later.\n\n"
        f"{table_str}\n\n"
        f"Write 3-4 sentences analyzing which signals are well-calibrated vs misleading. "
        f"Name specific signals and percentages. Focus on practical advice for adjusting the scoring system."
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=15,
        )
        if r.status_code == 200:
            return jsonify({"analysis": r.json()["content"][0]["text"].strip()})
        print(f"[Accuracy Analysis] API returned {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[Accuracy Analysis] {e}")
    return jsonify({"analysis": ""})

@app.route("/api/recommendations")
def get_recommendations():
    if not os.path.exists(RECS_CACHE_FILE):
        return jsonify({"updated": None, "results": [], "status": "building"})
    with open(RECS_CACHE_FILE, "r") as f:
        return jsonify(json.load(f))

@app.route("/api/market-overview")
def market_overview():
    articles = fetch_market_news()
    if not articles:
        return jsonify({"overview": ""})
    headlines = "\n".join([f"- {a['headline']}" for a in articles[:10]])
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 180,
                "messages": [{"role": "user", "content": (
                    f"Based on these market news headlines, write 2-3 sentences summarizing "
                    f"the current market environment and key themes investors should be aware of today. "
                    f"Be direct and specific. No fluff.\n\n{headlines}"
                )}]
            },
            timeout=15
        )
        if r.status_code == 200:
            return jsonify({"overview": r.json()["content"][0]["text"].strip()})
        print(f"[Market Overview] API returned {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[Market Overview] {e}")
    return jsonify({"overview": ""})

# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/stocks")
def get_stocks():
    if not os.path.exists(DATA_FILE):
        fetch_stock_data()
    with open(DATA_FILE, "r") as f:
        data = json.load(f)
    return jsonify(list(data.values()))

@app.route("/api/score-history/<ticker>")
def get_score_history(ticker):
    history = load_score_history()
    return jsonify(history.get(ticker.upper(), []))

@app.route("/api/news")
def get_news():
    return jsonify(fetch_market_news())

@app.route("/api/watchlist")
def get_watchlist():
    return jsonify(load_watchlist())

@app.route("/api/watchlist/add/<ticker>")
def add_ticker(ticker):
    ticker    = ticker.upper().strip()
    watchlist = load_watchlist()
    if ticker in watchlist:
        return jsonify({"success": False, "error": f"{ticker} is already in your watchlist"})
    try:
        test = yf.download(ticker, period="5d", progress=False, auto_adjust=True)
        if test.empty:
            return jsonify({"success": False, "error": f"{ticker} not found"})
    except Exception:
        return jsonify({"success": False, "error": f"Could not verify {ticker}"})
    watchlist.append(ticker)
    save_watchlist(watchlist)
    thread = threading.Thread(target=fetch_stock_data)
    thread.daemon = True
    thread.start()
    return jsonify({"success": True, "message": f"{ticker} added! Data loads in ~2 min", "watchlist": watchlist})

@app.route("/api/watchlist/remove/<ticker>")
def remove_ticker(ticker):
    ticker    = ticker.upper().strip()
    watchlist = load_watchlist()
    if ticker not in watchlist:
        return jsonify({"success": False, "error": f"{ticker} not in watchlist"})
    watchlist.remove(ticker)
    save_watchlist(watchlist)
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
        data.pop(ticker, None)
        with open(DATA_FILE, "w") as f:
            json.dump(data, f)
    return jsonify({"success": True, "message": f"{ticker} removed"})

@app.route("/api/voo-benchmark")
def voo_benchmark():
    from_date = request.args.get("from", "")
    try:
        datetime.strptime(from_date, "%Y-%m-%d")  # validate format
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400
    try:
        raw   = yf.download("VOO", start=from_date, progress=False, auto_adjust=True)
        close = raw["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close = close.dropna()
        if len(close) < 2:
            return jsonify({"error": f"No trading data found from {from_date}. The market may have been closed on that date — try the nearest trading day."})
        start_price = float(close.iloc[0])
        end_price   = float(close.iloc[-1])
        pct_change  = round((end_price - start_price) / start_price * 100, 2)
        start_date  = close.index[0].strftime("%Y-%m-%d")
        end_date    = close.index[-1].strftime("%Y-%m-%d")
        trading_days = len(close) - 1
        return jsonify({
            "start_date":    start_date,
            "end_date":      end_date,
            "start_price":   round(start_price, 2),
            "end_price":     round(end_price, 2),
            "pct_change":    pct_change,
            "trading_days":  trading_days,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/portfolio")
def get_portfolio():
    portfolio = load_portfolio()
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            stock_data = json.load(f)
        total_holdings_value = sum(
            shares * stock_data.get(t, {}).get("price", 0)
            for t, shares in portfolio["holdings"].items()
            if stock_data.get(t, {}).get("price") and stock_data.get(t, {}).get("price") != "N/A"
        )
        portfolio["holdings_value"] = round(total_holdings_value, 2)
        portfolio["total_value"]    = round(portfolio["cash"] + total_holdings_value, 2)
    return jsonify(portfolio)

@app.route("/api/buy/<ticker>")
def buy_stock(ticker):
    amount = request.args.get("amount", type=float)
    if amount is None:
        return jsonify({"success": False, "error": "Missing amount"})
    portfolio = load_portfolio()
    if amount <= 0:
        return jsonify({"success": False, "error": "Enter a valid amount"})
    if portfolio["cash"] < amount:
        return jsonify({"success": False, "error": f"Not enough cash (have ${portfolio['cash']:.2f})"})
    price = None
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            stock_data = json.load(f)
        p = stock_data.get(ticker, {}).get("price")
        if p and p != "N/A":
            price = float(p)
    shares = round(amount / price, 6) if price else 0
    portfolio["cash"] = round(portfolio["cash"] - amount, 2)
    portfolio["holdings"][ticker] = round(portfolio["holdings"].get(ticker, 0) + shares, 6)
    portfolio["history"].append({
        "action": "BUY", "ticker": ticker,
        "amount": amount, "price": price if price else "pending", "shares": shares,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M")
    })
    save_portfolio(portfolio)
    msg = (f"Bought {shares} shares of {ticker} for ${amount}" if price
           else f"Bought ${amount} of {ticker} — shares pending next price update")
    return jsonify({"success": True, "portfolio": portfolio, "message": msg})

@app.route("/api/sell/<ticker>")
def sell_stock(ticker):
    portfolio = load_portfolio()
    if ticker in portfolio["holdings"] and os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            stock_data = json.load(f)
        price  = stock_data.get(ticker, {}).get("price", 0)
        shares = portfolio["holdings"][ticker]
        value  = round(shares * price, 2)
        portfolio["cash"] = round(portfolio["cash"] + value, 2)
        del portfolio["holdings"][ticker]
        portfolio["history"].append({
            "action": "SELL", "ticker": ticker, "value": value,
            "price": price, "shares": round(shares, 6),
            "date": datetime.now().strftime("%Y-%m-%d %H:%M")
        })
        save_portfolio(portfolio)
        return jsonify({"success": True, "portfolio": portfolio})
    return jsonify({"success": False, "error": "No holdings found"})


@app.route("/api/refresh")
def manual_refresh():
    thread = threading.Thread(target=fetch_stock_data)
    thread.daemon = True
    thread.start()
    return jsonify({"success": True, "message": "Refresh started"})

# ─────────────────────────────────────────────
# ANALYZER
# ─────────────────────────────────────────────

def generate_analysis(data):
    """Call Claude for a detailed investment analysis of a single stock."""
    try:
        t  = data["ticker"]
        s  = data["score_short"]
        m  = data["score_mid"]
        l  = data["score_long"]
        ov = data["score_overall"]

        price      = data.get("price", "N/A")
        change_1d  = data.get("change_1d", 0)
        change_5d  = data.get("change_5d", 0)
        change_1mo = data.get("change_1mo", 0)
        rsi        = data.get("rsi", "N/A")
        ma_trend   = data.get("ma_trend", "N/A")
        earnings   = data.get("earnings_trend", "N/A")
        rating     = data.get("rating", "N/A")
        total_analysts = data.get("total_analysts", 0)
        fwd_pe     = data.get("forward_pe")
        rev_growth = data.get("revenue_growth")
        inst_own   = data.get("held_pct_institutions")
        short_pct  = data.get("short_percent")
        beta       = data.get("beta")
        insider    = data.get("insider_signal", "neutral")
        sentiment  = data.get("sentiment_label", "neutral")
        target_mean= data.get("target_mean")
        target_high= data.get("target_high")
        target_low = data.get("target_low")

        upside = None
        if target_mean and isinstance(price, (int, float)) and price > 0:
            upside = round(((target_mean - price) / price) * 100, 1)

        prompt = f"""You are a sharp, direct investment analyst. Analyze {t} using this data and write a thorough investment brief.

DATA:
- Price: ${price}  |  1D: {change_1d}%  |  5D: {change_5d}%  |  1Mo: {change_1mo}%
- RSI (14): {rsi}  |  MA Trend: {ma_trend}
- Earnings (last 4Q): {earnings}  |  Revenue growth: {f"{rev_growth*100:.1f}%" if rev_growth is not None else "N/A"}
- Analyst consensus: {rating} ({total_analysts} analysts){f"  |  Price target: ${target_mean} (low ${target_low} / high ${target_high})" if target_mean else ""}
{f"  |  Upside to target: {upside}%" if upside is not None else ""}
- Forward P/E: {f"{fwd_pe:.1f}" if fwd_pe else "N/A"}  |  Institutional ownership: {f"{inst_own*100:.0f}%" if inst_own else "N/A"}
- Short interest: {f"{short_pct*100:.1f}%" if short_pct else "N/A"}  |  Beta: {beta if beta else "N/A"}
- Insider activity (90d): {insider}  |  News sentiment (14d): {sentiment}
- Scores — Short: {s}/10  |  Mid: {m}/10  |  Long: {l}/10  |  Overall: {ov}/10

Write a structured analysis with these exact sections (use the headers as shown):

**What they do**
One sentence: what industry, what the company does, and its market position.

**What the data says**
2–3 sentences interpreting the signals above. What's working in their favor? What's a concern? Be specific — cite actual numbers.

**Investment case**
2–3 sentences on why someone would (or wouldn't) buy this now. Anchor it in the score pattern (e.g. strong long but weak short = wait for a dip). Don't be vague.

**How to invest**
2–3 specific, actionable sentences: what time horizon fits (short trade, mid swing, long hold), whether to buy now or wait for a signal, and any key risk to watch. Be concrete.

**Buy or pass?**
Write 2 paragraphs. First paragraph: give a clear verdict — buy, pass, or wait for a better entry — and explain exactly why based on the data. Don't hedge. Second paragraph: who this stock is right for (e.g. risk-tolerant momentum trader, long-term value investor) and under what conditions you'd change your view (e.g. "if earnings disappoint next quarter" or "if price breaks below $X").

Be direct. No filler. No disclaimers."""

        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 750, "messages": [{"role": "user", "content": prompt}]},
            timeout=20
        )
        if r.status_code == 200:
            return r.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"[Analyzer] AI error for {data.get('ticker')}: {e}")
    return ""

@app.route("/api/analyze/<ticker>")
def analyze_ticker(ticker):
    ticker = ticker.upper().strip()
    try:
        # Fetch price history via yfinance
        raw    = yf.download([ticker], period="3mo", progress=False, auto_adjust=True)
        close  = raw["Close"]
        if isinstance(close.columns, pd.MultiIndex):
            close.columns = close.columns.get_level_values(-1)
        if isinstance(close, pd.Series):
            close = close.to_frame(name=ticker)
        series = close[ticker].dropna() if ticker in close.columns else pd.Series(dtype=float)

        if len(series) < 2:
            return jsonify({"error": f"No price data found for {ticker}. It may be delisted or invalid."}), 404

        current  = float(series.iloc[-1])
        prev_1d  = float(series.iloc[-2])  if len(series) >= 2  else current
        prev_5d  = float(series.iloc[-6])  if len(series) >= 6  else float(series.iloc[0])
        prev_1mo = float(series.iloc[-22]) if len(series) >= 22 else float(series.iloc[0])

        price_info = {
            "price":      round(current, 2),
            "change_1d":  round(((current - prev_1d)  / prev_1d)  * 100, 2) if prev_1d  else 0,
            "change_5d":  round(((current - prev_5d)  / prev_5d)  * 100, 2) if prev_5d  else 0,
            "change_1mo": round(((current - prev_1mo) / prev_1mo) * 100, 2) if prev_1mo else 0,
        }

        # Short-term signals
        short_data = get_short_term(series)
        ma_data    = get_ma_trend(series)

        # Finnhub signals
        earnings_trend = fetch_earnings_trend(ticker)
        rating_data    = fetch_analyst_rating(ticker)
        pt_data        = fetch_price_target(ticker)
        insider_data   = fetch_insider_signal(ticker)
        sentiment_data = fetch_news_sentiment(ticker)

        # yfinance fundamentals
        fund = {}
        try:
            info = yf.Ticker(ticker).info
            fund = {
                "short_percent":       info.get("shortPercentOfFloat"),
                "revenue_growth":      info.get("revenueGrowth"),
                "forward_pe":          info.get("forwardPE"),
                "held_pct_institutions": info.get("heldPercentInstitutions"),
                "beta":                info.get("beta"),
                "sector":              info.get("sector"),
                "industry":            info.get("industry"),
                "long_name":           info.get("longName"),
                "market_cap":          info.get("marketCap"),
                "trailing_pe":         info.get("trailingPE"),
                "earnings_growth":     info.get("earningsGrowth"),
            }
        except Exception as e:
            print(f"[Analyzer] yfinance info error for {ticker}: {e}")

        # Macro adjustments
        vix_adj, credit_adj = get_macro_adjustments()

        # Build stock entry for scoring
        entry = {
            "ticker":               ticker,
            **price_info,
            "rsi":                  short_data.get("rsi"),
            "rsi_label":            short_data.get("rsi_label"),
            "ma20":                 ma_data.get("ma20"),
            "ma50":                 ma_data.get("ma50"),
            "ma_trend":             ma_data.get("ma_trend"),
            "earnings_trend":       earnings_trend,
            "rating":               rating_data["rating"],
            "rating_breakdown":     rating_data["breakdown"],
            "total_analysts":       rating_data["total_analysts"],
            "target_mean":          pt_data.get("target_mean"),
            "target_high":          pt_data.get("target_high"),
            "target_low":           pt_data.get("target_low"),
            "insider_signal":       insider_data["signal"],
            "insider_net":          insider_data["net"],
            "insider_buys":         insider_data["buys"],
            "insider_sells":        insider_data["sells"],
            "sentiment_score":      sentiment_data.get("sentiment_score"),
            "sentiment_label":      sentiment_data.get("sentiment_label", "neutral"),
            "news_headlines":       sentiment_data.get("headlines", []),
            "macro_vix_adj":        vix_adj,
            "macro_credit_adj":     credit_adj,
            **fund,
        }

        # Scores
        entry["score_short"]   = score_short(entry)
        entry["score_mid"]     = score_mid(entry)
        entry["score_long"]    = score_long(entry)
        entry["score_overall"] = score_overall(entry)

        # Price target upside
        if entry.get("target_mean") and entry["price"] and isinstance(entry["price"], (int, float)):
            entry["target_upside"] = round(((entry["target_mean"] - entry["price"]) / entry["price"]) * 100, 1)

        # AI analysis
        entry["ai_analysis"] = generate_analysis(entry)

        return jsonify(entry)

    except Exception as e:
        print(f"[Analyzer] Error for {ticker}: {e}")
        return jsonify({"error": str(e)}), 500

# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────



def clean_dead_tickers():
    print("[Cleanup] Checking for delisted tickers...")
    watchlist = load_watchlist()
    if not watchlist:
        return
    try:
        raw = yf.download(watchlist, period="1mo", progress=False, auto_adjust=True)
        close = raw["Close"]
        if isinstance(close.columns, pd.MultiIndex):
            close.columns = close.columns.get_level_values(-1)
        dead = []
        for ticker in watchlist:
            if ticker not in close.columns or close[ticker].dropna().empty:
                dead.append(ticker)
        if not dead:
            print("[Cleanup] All tickers OK.")
            return
        print(f"[Cleanup] Removing dead tickers: {dead}")
        clean_watchlist = [t for t in watchlist if t not in dead]
        save_watchlist(clean_watchlist)
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE) as f:
                data = json.load(f)
            for t in dead:
                data.pop(t, None)
            with open(DATA_FILE, "w") as f:
                json.dump(data, f)
        print(f"[Cleanup] Removed {len(dead)} dead tickers.")
    except Exception as e:
        print(f"[Cleanup] Error: {e}")

# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# AI PAPER PORTFOLIO
# ─────────────────────────────────────────────

AI_PORTFOLIO_FILE    = "ai_portfolio.json"
AI_STARTING_CASH     = 10000.0
AI_MAX_POSITIONS     = 6
AI_POSITION_PCT      = 0.20   # 20% of portfolio per trade
AI_STOP_LOSS         = -0.10  # -10%
AI_TAKE_PROFIT       = 0.20   # +20%

def load_ai_portfolio():
    if os.path.exists(AI_PORTFOLIO_FILE):
        with open(AI_PORTFOLIO_FILE) as f:
            return json.load(f)
    return {"cash": AI_STARTING_CASH, "holdings": {}, "trades": [], "last_scan": None, "scan_count": 0}

def save_ai_portfolio(p):
    with open(AI_PORTFOLIO_FILE, "w") as f:
        json.dump(p, f)

def get_ai_trade_decisions(portfolio, stocks):
    """Ask Claude to decide which stocks to buy/sell given current portfolio state."""
    try:
        cash     = portfolio["cash"]
        holdings = portfolio["holdings"]

        # Compute approximate total portfolio value
        total_value = cash
        for ticker, h in holdings.items():
            s = next((x for x in stocks if x["ticker"] == ticker), None)
            price = s["price"] if s and isinstance(s.get("price"), (int, float)) else h["avg_cost"]
            total_value += h["shares"] * price

        position_target = round(total_value * AI_POSITION_PCT, 2)

        holdings_lines = []
        for ticker, h in holdings.items():
            s = next((x for x in stocks if x["ticker"] == ticker), None)
            price = s["price"] if s and isinstance(s.get("price"), (int, float)) else h["avg_cost"]
            pnl_pct = (price - h["avg_cost"]) / h["avg_cost"] * 100 if h["avg_cost"] else 0
            holdings_lines.append(
                f"  {ticker}: {h['shares']:.3f} shares @ avg ${h['avg_cost']:.2f}, "
                f"now ${price:.2f} ({pnl_pct:+.1f}%), entry overall score {h.get('entry_score','?')}"
            )

        stock_lines = []
        for s in stocks:
            if not isinstance(s.get("price"), (int, float)):
                continue
            tag = " [HELD]" if s["ticker"] in holdings else ""
            stock_lines.append(
                f"  {s['ticker']}: ${s['price']} | "
                f"Overall {s.get('score_overall','?')}/10 "
                f"(S:{s.get('score_short','?')} M:{s.get('score_mid','?')} L:{s.get('score_long','?')}) | "
                f"RSI {s.get('rsi','N/A')} | MA {s.get('ma_trend','N/A')} | "
                f"Earnings {s.get('earnings_trend','N/A')} | Rating {s.get('rating','N/A')} | "
                f"Sentiment {s.get('sentiment_label','N/A')}{tag}"
            )

        prompt = f"""You are an AI day trader managing a paper portfolio. Make buy/sell decisions right now based on the data.

PORTFOLIO:
- Cash: ${cash:.2f} of ~${total_value:.2f} total
- Positions: {len(holdings)}/{AI_MAX_POSITIONS} max
- Current holdings:
{chr(10).join(holdings_lines) if holdings_lines else "  (none)"}

WATCHLIST STOCKS:
{chr(10).join(stock_lines)}

RULES:
- Target position size: ~${position_target:.0f} per trade
- BUY when: overall score ≥ 7.0, strong short-term momentum, not already held, cash available
- SELL when: overall score ≤ 4.0, or signals deteriorating significantly
- Hard rules handled automatically (stop-loss/take-profit) — focus on signal-based decisions
- Be decisive but don't trade just to trade — only act on clear signals

Respond ONLY with valid JSON, no other text:
{{"sells": [{{"ticker": "EXAMPLE", "reason": "one sentence explanation"}}], "buys": [{{"ticker": "EXAMPLE", "amount": {position_target:.0f}, "reason": "one sentence explanation"}}]}}

If no trades make sense right now: {{"sells": [], "buys": []}}"""

        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 500,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=20
        )
        if r.status_code == 200:
            import re
            text = r.json()["content"][0]["text"].strip()
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                return json.loads(m.group())
    except Exception as e:
        print(f"[AI Portfolio] Decision error: {e}")
    return {"sells": [], "buys": []}

def run_ai_portfolio_scan():
    print(f"\n[AI Portfolio] Scan at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if not os.path.exists(DATA_FILE):
        print("[AI Portfolio] No stock data, skipping.")
        return

    with open(DATA_FILE) as f:
        stock_dict = json.load(f)
    stocks = [s for s in stock_dict.values() if isinstance(s.get("price"), (int, float))]
    if not stocks:
        print("[AI Portfolio] No valid prices, skipping.")
        return

    portfolio = load_ai_portfolio()
    now_str   = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── Hard rules: stop-loss / take-profit (no Claude needed) ──
    forced_sells = []
    for ticker, h in list(portfolio["holdings"].items()):
        s = next((x for x in stocks if x["ticker"] == ticker), None)
        if not s:
            continue
        price   = s["price"]
        pnl_pct = (price - h["avg_cost"]) / h["avg_cost"] if h["avg_cost"] else 0
        if pnl_pct <= AI_STOP_LOSS:
            forced_sells.append((ticker, price, h, f"Stop-loss triggered ({pnl_pct*100:+.1f}%)"))
        elif pnl_pct >= AI_TAKE_PROFIT:
            forced_sells.append((ticker, price, h, f"Take-profit triggered ({pnl_pct*100:+.1f}%)"))

    for ticker, price, h, reason in forced_sells:
        shares   = h["shares"]
        proceeds = round(shares * price, 2)
        pnl      = round(proceeds - shares * h["avg_cost"], 2)
        portfolio["cash"] = round(portfolio["cash"] + proceeds, 2)
        del portfolio["holdings"][ticker]
        portfolio["trades"].insert(0, {
            "date": now_str, "action": "SELL", "ticker": ticker,
            "shares": round(shares, 4), "price": price,
            "amount": proceeds, "pnl": pnl, "reason": reason,
            "score_at_trade": next((x.get("score_overall") for x in stocks if x["ticker"] == ticker), None)
        })
        print(f"[AI Portfolio] FORCED SELL {ticker} | {reason} | P&L ${pnl}")

    # ── Claude decides the rest ──
    decisions = get_ai_trade_decisions(portfolio, stocks)

    # Execute sells
    for sell in decisions.get("sells", []):
        ticker = sell.get("ticker", "").upper()
        reason = sell.get("reason", "")
        if ticker not in portfolio["holdings"]:
            continue
        # skip if already force-sold
        h      = portfolio["holdings"][ticker]
        s      = next((x for x in stocks if x["ticker"] == ticker), None)
        price  = s["price"] if s else h["avg_cost"]
        shares = h["shares"]
        proceeds = round(shares * price, 2)
        pnl      = round(proceeds - shares * h["avg_cost"], 2)
        portfolio["cash"] = round(portfolio["cash"] + proceeds, 2)
        del portfolio["holdings"][ticker]
        portfolio["trades"].insert(0, {
            "date": now_str, "action": "SELL", "ticker": ticker,
            "shares": round(shares, 4), "price": price,
            "amount": proceeds, "pnl": pnl, "reason": reason,
            "score_at_trade": s.get("score_overall") if s else None
        })
        print(f"[AI Portfolio] SELL {ticker} @ ${price} | P&L ${pnl} | {reason}")

    # Execute buys
    for buy in decisions.get("buys", []):
        ticker = buy.get("ticker", "").upper()
        reason = buy.get("reason", "")
        amount = float(buy.get("amount", 0))
        if ticker in portfolio["holdings"]:
            continue
        if len(portfolio["holdings"]) >= AI_MAX_POSITIONS:
            break
        s = next((x for x in stocks if x["ticker"] == ticker), None)
        if not s or not isinstance(s.get("price"), (int, float)):
            continue
        price  = s["price"]
        amount = min(amount, portfolio["cash"] * 0.95)
        if amount < 100:
            continue
        shares = round(amount / price, 4)
        cost   = round(shares * price, 2)
        portfolio["holdings"][ticker] = {
            "shares": shares, "avg_cost": price,
            "entry_date": now_str[:10],
            "entry_score": s.get("score_overall")
        }
        portfolio["cash"] = round(portfolio["cash"] - cost, 2)
        portfolio["trades"].insert(0, {
            "date": now_str, "action": "BUY", "ticker": ticker,
            "shares": shares, "price": price,
            "amount": cost, "pnl": None, "reason": reason,
            "score_at_trade": s.get("score_overall")
        })
        print(f"[AI Portfolio] BUY {shares} {ticker} @ ${price} | ${cost} | {reason}")

    portfolio["trades"]    = portfolio["trades"][:50]
    portfolio["last_scan"] = now_str
    portfolio["scan_count"] = portfolio.get("scan_count", 0) + 1
    save_ai_portfolio(portfolio)
    print(f"[AI Portfolio] Scan complete.")

@app.route("/api/ai-portfolio")
def get_ai_portfolio():
    portfolio = load_ai_portfolio()
    stock_dict = {}
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            stock_dict = json.load(f)

    holdings_out   = {}
    holdings_value = 0.0
    for ticker, h in portfolio["holdings"].items():
        s     = stock_dict.get(ticker, {})
        price = s.get("price")
        if not isinstance(price, (int, float)):
            price = h["avg_cost"]
        val      = round(h["shares"] * price, 2)
        cost     = round(h["shares"] * h["avg_cost"], 2)
        pnl      = round(val - cost, 2)
        pnl_pct  = round(pnl / cost * 100, 2) if cost else 0
        holdings_value += val
        holdings_out[ticker] = {
            **h,
            "current_price": price,
            "current_value": val,
            "cost_basis": cost,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "score_overall": s.get("score_overall"),
            "score_short":   s.get("score_short"),
            "score_mid":     s.get("score_mid"),
            "score_long":    s.get("score_long"),
        }

    total_value = round(portfolio["cash"] + holdings_value, 2)
    total_pnl   = round(total_value - AI_STARTING_CASH, 2)
    total_pnl_pct = round(total_pnl / AI_STARTING_CASH * 100, 2)

    return jsonify({
        "cash":           portfolio["cash"],
        "holdings":       holdings_out,
        "holdings_value": round(holdings_value, 2),
        "total_value":    total_value,
        "total_pnl":      total_pnl,
        "total_pnl_pct":  total_pnl_pct,
        "trades":         portfolio["trades"][:30],
        "last_scan":      portfolio.get("last_scan"),
        "scan_count":     portfolio.get("scan_count", 0),
    })

@app.route("/api/ai-portfolio/scan")
def trigger_ai_scan():
    thread = threading.Thread(target=run_ai_portfolio_scan)
    thread.daemon = True
    thread.start()
    return jsonify({"success": True, "message": "AI scan started"})

@app.route("/api/ai-portfolio/reset", methods=["GET"])
def reset_ai_portfolio():
    p = {"cash": AI_STARTING_CASH, "holdings": {}, "trades": [], "last_scan": None, "scan_count": 0}
    save_ai_portfolio(p)
    return jsonify({"success": True})

# ─────────────────────────────────────────────
# DAILY PICKS
# ─────────────────────────────────────────────

def save_daily_picks():
    """Snapshot top 9 short-term picks at 5 AM each morning."""
    try:
        if not os.path.exists(DATA_FILE):
            return
        with open(DATA_FILE) as f:
            data = json.load(f)
        ranked = sorted(
            [(t, d) for t, d in data.items() if isinstance(d.get("score_short"), (int, float))],
            key=lambda x: x[1].get("score_short", 0),
            reverse=True
        )[:9]
        picks = []
        for ticker, d in ranked:
            picks.append({
                "ticker":    ticker,
                "short":     d.get("score_short"),
                "mid":       d.get("score_mid"),
                "long":      d.get("score_long"),
                "overall":   d.get("score_overall"),
                "price":     d.get("price"),
                "change_1d": d.get("change_1d"),
            })
        payload = {
            "date":      datetime.now().strftime("%Y-%m-%d"),
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "picks":     picks,
        }
        with open(DAILY_PICKS_FILE, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"[Daily Picks] Saved {len(picks)} picks at {payload['timestamp']}")
    except Exception as e:
        print(f"[Daily Picks] Error saving: {e}")


@app.route("/api/daily-picks")
def api_daily_picks():
    try:
        with open(DAILY_PICKS_FILE) as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({"picks": [], "date": "", "timestamp": ""})


# ─────────────────────────────────────────────
# SCHEDULER & STARTUP (module-level so it runs under both python app.py and import app)
# ─────────────────────────────────────────────

scheduler = BackgroundScheduler(timezone="America/Los_Angeles")
# 5 AM pre-market snapshot for Daily Picks
scheduler.add_job(fetch_stock_data, "cron", hour=5,  minute=0)   # 5:00 AM PT pre-market
scheduler.add_job(save_daily_picks,  "cron", hour=5,  minute=5)   # 5:05 AM PT snapshot top 9 short
# Fixed daily refreshes — Pacific Time
scheduler.add_job(fetch_stock_data, "cron", hour=8,  minute=0)   # 8:00 AM PT
scheduler.add_job(fetch_stock_data, "cron", hour=10, minute=0)   # 10:00 AM PT
scheduler.add_job(fetch_stock_data, "cron", hour=12, minute=0)   # 12:00 PM PT
scheduler.add_job(fetch_stock_data, "cron", hour=13, minute=30)  # 1:30 PM PT
scheduler.add_job(fetch_stock_data, "cron", hour=16, minute=0)   # 4:00 PM PT (end-of-day for evening report)
# Recommendations and fundamentals
scheduler.add_job(build_recommendations, "interval", hours=6)
scheduler.add_job(build_recommendations, "cron", hour=8, minute=10)  # shortly after 8 AM open refresh
scheduler.add_job(lambda: fetch_all_fundamentals(load_watchlist()), "interval", hours=24)
scheduler.add_job(fetch_macro_data, "interval", hours=24)
# AI portfolio scans — after each main refresh + every 2h during the day
scheduler.add_job(run_ai_portfolio_scan, "cron", hour=8,  minute=15)
scheduler.add_job(run_ai_portfolio_scan, "cron", hour=10, minute=15)
scheduler.add_job(run_ai_portfolio_scan, "cron", hour=12, minute=15)
scheduler.add_job(run_ai_portfolio_scan, "cron", hour=13, minute=45)
scheduler.start()


def startup_refresh():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
        if data:
            first = next(iter(data.values()))
            updated_str = first.get("updated", "")
            try:
                updated_dt = datetime.strptime(updated_str, "%Y-%m-%d %H:%M")
                if datetime.now() - updated_dt > timedelta(hours=1):
                    print("[Startup] Data is stale, refreshing...")
                    fetch_stock_data()
                    return
            except Exception:
                pass
    fetch_stock_data()


if fundamentals_stale():
    fund_thread = threading.Thread(target=lambda: fetch_all_fundamentals(load_watchlist()))
    fund_thread.daemon = True
    fund_thread.start()

if macro_stale():
    macro_thread = threading.Thread(target=fetch_macro_data)
    macro_thread.daemon = True
    macro_thread.start()

rec_thread = threading.Thread(target=build_recommendations)
rec_thread.daemon = True
rec_thread.start()

thread = threading.Thread(target=startup_refresh)
thread.daemon = True
thread.start()
