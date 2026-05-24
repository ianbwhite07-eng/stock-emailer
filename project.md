# Stock Dashboard — Project Documentation

## Overview
A Flask-based stock monitoring dashboard that tracks a personal watchlist of small/mid-cap stocks. It fetches price data via yfinance, fundamental signals via yfinance + Finnhub, and scores each stock across three time horizons. Includes a paper trading portfolio, a news/recommendations tab, and (in progress) a prediction accuracy tracker.

---

## Stack
- **Backend:** Python 3.14, Flask, APScheduler, yfinance, pandas, requests, vaderSentiment
- **Frontend:** Single-file HTML/CSS/JS (no framework)
- **APIs:** Finnhub (earnings, analyst ratings, news, insider transactions, price targets), Anthropic Claude (AI overviews for recommendations + accuracy analysis)
- **Storage:** JSON flat files (no database)

---

## File Structure
```
stock-dashboard/
├── app.py                    # Main Flask app
├── config.py                 # API keys and settings (not committed)
├── templates/
│   └── index.html            # Single-page frontend (4-tab redesign)
│   └── index.html.bak        # Backup of original design
├── watchlist.json            # Current watchlist (user-editable)
├── stock_data.json           # Cached stock data (auto-generated)
├── fundamentals_data.json    # Cached fundamentals + sentiment + insider data (auto-generated)
├── portfolio.json            # Paper portfolio state
├── score_history.json        # Historical scores per ticker
├── recs_cache.json           # Cached recommendations
├── accuracy_log.json         # [PENDING] Prediction snapshots for accuracy tracking
└── requirements.txt          # Python dependencies
```

---

## config.py (required, not committed)
```python
API_KEY = ""              # unused / reserved
REFRESH_HOURS = 4         # how often scheduler refreshes
FINNHUB_KEY = "your_key"  # from finnhub.io
ANTHROPIC_KEY = "your_key"
WATCHLIST = ["ALGT", "ECPG", ...]  # default watchlist
```

---

## Scoring System

Each stock gets three scores (0–10) combined into an overall score:

| Dimension | Weight | Signals Used |
|-----------|--------|--------------|
| Short Term | 25% | RSI (14d), 5D price change, short interest, beta, news sentiment |
| Mid Term | 40% | 20MA vs 50MA gap %, earnings beat/miss (4 qtrs), revenue growth |
| Long Term | 35% | Finnhub analyst consensus, forward P/E, institutional ownership, insider transactions |

**Overall = Short×0.25 + Mid×0.40 + Long×0.35**

### Short Term Scoring
```
Base: 5
RSI ≥75: +3 | RSI 65–74: +2 | RSI 55–64: +1
RSI ≤25: -3 | RSI 26–35: -2 | RSI 36–45: -1
5D change >5%: +2 | >2%: +1 | <-5%: -2 | <-2%: -1
Short interest >25% of float: -1
Beta >2.5: -1
News sentiment (14-day VADER): positive +1 | negative -1
```

### Mid Term Scoring
```
Base: 5
20MA/50MA gap >5%: +3 | 2–5%: +2 | 0–2%: +1
20MA/50MA gap -2–0%: -1 | -5–-2%: -2 | <-5%: -3
Beat x4: +3 | Beat x3: +2 | Beat x2: +1
Missed x4: -3 | Missed x3: -2 | Missed x2: -1
Revenue growth >15% YoY: +1 | <-10% YoY: -1
```

### Long Term Scoring
```
Strong Buy: 10 | Buy: 8 | Hold: 5 | Sell: 2 | Strong Sell: 0  (analyst consensus base)
Forward P/E <0: -1 | <8: +1 | >50: -1
Institutional ownership >80%: +1 | <25%: -1
Net insider buying (last 90 days): +1 | Net insider selling: -1
```

---

## Data Sources & Refresh Cadence

| Source | Data | Refresh |
|--------|------|---------|
| yfinance (bulk) | Price, RSI, MA20/50, 1D/5D/1M change | Cron at 8:00, 10:00, 12:00, 13:30 PT |
| Finnhub | Analyst rating, earnings trend, price target | Same as above |
| yfinance `.info` | Short %, beta, revenue growth, fwd P/E, institutions | Every 24 hours |
| Finnhub insider | Net insider buy/sell last 90 days | Every 24 hours |
| Finnhub company news + VADER | 14-day sentiment score | Every 24 hours |
| yfinance `.news` | Recent headlines for News button tooltip | Every 24 hours |

---

## Scheduler (module-level, not inside `__name__` block)

**CRITICAL:** The app is started with `import app as a; a.app.run(...)` — the `if __name__ == "__main__":` block never runs. All scheduler setup must be at module level.

```python
scheduler = BackgroundScheduler(timezone="America/Los_Angeles")
scheduler.add_job(fetch_stock_data, "cron", hour=8,  minute=0)
scheduler.add_job(fetch_stock_data, "cron", hour=10, minute=0)
scheduler.add_job(fetch_stock_data, "cron", hour=12, minute=0)
scheduler.add_job(fetch_stock_data, "cron", hour=13, minute=30)
scheduler.add_job(build_recommendations, "interval", hours=6)
scheduler.add_job(build_recommendations, "cron", hour=8, minute=10)
scheduler.add_job(lambda: fetch_all_fundamentals(load_watchlist()), "interval", hours=24)
scheduler.start()
```

---

## Key Functions (app.py)

### Data Fetching
- `fetch_prices(watchlist)` — bulk yfinance download, returns prices dict and raw series dict. Handles MultiIndex columns from newer yfinance versions.
- `get_short_term(series)` — calculates RSI(14), returns label (Bullish/Bearish/Neutral)
- `get_ma_trend(series)` — calculates 20MA and 50MA, returns trend + raw ma20/ma50 values
- `fetch_earnings_trend(ticker)` — Finnhub API, last 4 quarters beat/miss
- `fetch_analyst_rating(ticker)` — Finnhub API, consensus rating + breakdown
- `fetch_price_target(ticker)` — Finnhub API, mean/high/low analyst price targets
- `fetch_market_news()` — Finnhub general market news
- `fetch_insider_signal(ticker)` — Finnhub insider transactions last 90 days; returns `{"signal": "buying"/"selling"/"neutral", "net": int, "buys": int, "sells": int}`
- `fetch_news_sentiment(ticker)` — Finnhub company news last 14 days + VADER compound score; returns `{"sentiment_score": float, "sentiment_label": str, "headlines": [...]}`

### Fundamentals
- `load_fundamentals()` — loads fundamentals_data.json
- `fetch_all_fundamentals(watchlist)` — daily refresh: yfinance .info fields (short_percent, revenue_growth, earnings_growth, forward_pe, trailing_pe, held_pct_institutions, beta), yfinance .news, Finnhub insider signal, Finnhub news sentiment. Writes to fundamentals_data.json.
- `fundamentals_stale()` — returns True if fundamentals_data.json is >24h old

### Core Logic
- `fetch_stock_data()` — main refresh loop, iterates watchlist, fetches all signals, merges fundamentals, writes stock_data.json. Sleeps 2s between tickers to avoid Finnhub rate limits. Also computes `score_changes` by comparing new vs previous scores. **[TODO: call record_prediction() and evaluate_predictions() here]**
- `record_scores(ticker, ...)` — appends daily score snapshot to score_history.json
- `build_recommendations()` — scans UNIVERSE list (excluding watchlist), scores candidates, fetches top 10 Finnhub data + price targets, calls `generate_stock_overview`, caches to recs_cache.json
- `generate_stock_overview(...)` — calls Claude Haiku for a 3-sentence AI summary (company + investment case, market context, hold horizon), max_tokens=220. Returns `""` on failure.
- `clean_dead_tickers()` — checks watchlist for delisted tickers (disabled)

### Scoring
- `score_short(s)` — see Short Term Scoring above
- `score_mid(s)` — see Mid Term Scoring above
- `score_long(s)` — see Long Term Scoring above
- `score_overall(s)` — weighted average

---

## API Routes

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/` | Serves index.html |
| GET | `/api/stocks` | Returns all watchlist stock data |
| GET | `/api/score-history/<ticker>` | Returns score history array for ticker |
| GET | `/api/news` | Returns 10 market news articles |
| GET | `/api/market-overview` | Returns Claude Haiku 2-3 sentence market summary from headlines |
| GET | `/api/watchlist` | Returns current watchlist array |
| GET | `/api/watchlist/add/<ticker>` | Validates + adds ticker, triggers refresh |
| GET | `/api/watchlist/remove/<ticker>` | Removes ticker from watchlist + data |
| GET | `/api/portfolio` | Returns portfolio with live values |
| GET | `/api/buy/<ticker>?amount=X` | Buys $X of ticker (fractional shares) |
| GET | `/api/sell/<ticker>` | Sells all shares of ticker |
| GET | `/api/refresh` | Triggers background data refresh |
| GET | `/api/recommendations` | Returns cached recommendations |
| GET | `/api/accuracy` | **[PENDING]** Returns per-signal hit rate stats |
| GET | `/api/accuracy/analysis` | **[PENDING]** Claude Haiku narrative on which signals are working |

---

## Frontend (index.html) — Current Design

Single HTML file with inline CSS and JS. **4-tab layout** (redesigned in this session):

**Color scheme:** `#0f0f0f` background, `#141414` cards, `#1e2535` borders (subtle blue tint), `#60a5fa` active tab underline.

### Tab 1: My Profiler
- Add/remove tickers from watchlist
- Sort by: Alphabetical, Short Term, Mid Term, Long Term, Overall Score
- **Card grid layout** — `.cards-grid` CSS grid, 3 columns, `.stock-card` per ticker (not a table)
- Each card: ticker + remove btn + score ring (color-coded glow), price + 1D/1Mo changes, S/M/L sub-scores row, 3-column signal section (Short: RSI+5D, Mid: MA+Earnings, Long: Rating), History/News buttons
- `renderCards()` function — detects `price === "N/A"` and renders `—` for all change/score values (no stale 0%)
- Score ring glow: green `rgba(34,197,94,0.2)`, amber, red based on score
- Collapsible **Metric Reference Guide** with 6 metric blocks in 3-column grid
- Alert banners for analyst rating changes

### Tab 2: Paper Portfolio
- Start with $100 cash
- Buy by dollar amount (fractional shares)
- Sell all shares of a holding
- Shows P&L per holding, lot details with scores at time of purchase
- Trade history

### Tab 3: Recommendations
- Top picks from watchlist by time horizon (Short/Mid/Long)
- AI-powered recommendations from UNIVERSE scan with price target upside
- Synopsis button → modal with Claude Haiku 3-sentence overview
- Add to Watchlist button
- Modal shows helpful message when overview is empty: "ensure your Anthropic API key has credits and wait for the next scan (runs every 6 hours)"

### Tab 4: News & Changes
- **Score Changes Since Last Refresh** — "consider selling" on overall drop ≥2, "watch closely" on drop 1, "looking stronger" on gain
- **AI Market Overview** — 2-3 sentence Claude Haiku summary from `/api/market-overview`
- Market news from Finnhub (10 articles)

### Frontend Scoring
JS mirrors Python scoring exactly — `scoreShort()`, `scoreMid()`, `scoreLong()`, `scoreOverall()` — so scores render instantly from cached data without a server round-trip. **Both must be kept in sync whenever scoring logic changes.**

---

## Prediction Accuracy System — PENDING IMPLEMENTATION

This feature is designed and ready to build. The goal: track whether each scoring signal actually predicted price movement correctly, accumulate hit rates per signal, and use Claude to suggest which signals are well-calibrated vs misleading.

### Data File: `accuracy_log.json`
```json
{
  "TICKER": [
    {
      "date": "2026-04-16",
      "price": 42.50,
      "score_short": 7,
      "score_mid": 6,
      "score_overall": 6.4,
      "signals": {
        "rsi_high": true,        // RSI ≥55
        "price_5d_positive": true,
        "short_interest_high": false,
        "beta_high": false,
        "sentiment_positive": true,
        "ma_bullish": true,
        "earnings_beats": true,
        "revenue_growing": false
      },
      "outcome_price": null,     // filled in 5 trading days later
      "outcome_correct": null    // true if prediction matched direction
    }
  ]
}
```

### Functions to Add (app.py)
- `record_prediction(ticker, stock_data)` — called inside `fetch_stock_data()` on each refresh. Saves current price + active signals to accuracy_log.json. Skip if entry already exists for today.
- `evaluate_predictions()` — called inside `fetch_stock_data()` on each refresh. Finds entries where outcome_price is null and date is ≥5 trading days ago. Fetches current price, computes if prediction was correct (high score + price up OR low score + price down), fills in outcome fields.
- `build_signal_stats()` — reads accuracy_log.json, aggregates per-signal hit rate across all tickers and all time. Returns dict like `{"rsi_high": {"correct": 45, "total": 71, "pct": 63.4}, ...}`. Only include signals with ≥10 observations.

### Routes to Add (app.py)
- `GET /api/accuracy` — calls `build_signal_stats()`, returns JSON
- `GET /api/accuracy/analysis` — calls `build_signal_stats()`, formats as table, sends to Claude Haiku for a 3-4 sentence narrative on which signals are working and which aren't. Returns `{"analysis": "..."}`.

### Frontend (index.html)
- New **Accuracy** section, probably inside Recommendations tab or as a 5th tab
- Shows a table: Signal | Hit Rate | Sample Size | Status (green/amber/red)
- "Get AI Analysis" button → fetches `/api/accuracy/analysis` → shows narrative
- Caveat displayed: "Signals with <30 observations are preliminary"
- No auto-weight adjustment — user reviews and manually changes scoring thresholds if desired

### Evaluation Logic
- "Correct" for short-term: score ≥7 AND price up after 5 days, OR score ≤4 AND price down
- "Incorrect": opposite
- Neutral scores (5–6) are excluded from hit rate calculation (ambiguous signal)
- 5 trading days = roughly 1 calendar week; use `pandas_market_calendars` or simple 7-day offset

---

## Known Issues / Tech Debt
1. **yfinance MultiIndex columns** — newer yfinance returns MultiIndex on bulk downloads. Fixed with `close.columns.get_level_values(-1)` after download.
2. **Delisted tickers** — yfinance silently returns NaN rows. `clean_dead_tickers()` was added to auto-remove but was too aggressive. Currently disabled; delisted tickers must be removed manually.
3. **Finnhub rate limits** — free tier hits 429 on rapid requests. `time.sleep(2)` between tickers mitigates this. Concurrent jobs at startup can still cause N/A ratings — fix by waiting ~1 min after startup then hitting /api/refresh.
4. **Flask template caching** — templates are cached in memory with `debug=False`. Every HTML change requires an app restart to take effect.
5. **score_history.json corruption** — if the app crashes mid-write, the JSON can become malformed. Fix: `echo '{}' > score_history.json`
6. **Port 5000 conflict** — macOS AirPlay Receiver uses port 5000. Always run on port 5001.
7. **Debug mode** — run with `debug=False, use_reloader=False` to avoid double-startup and double refresh threads.
8. **Anthropic API credits** — if AI synopses show "not yet available", check account credits. Errors are silent.
9. **N/A data on startup** — Finnhub rate limits can cause N/A analyst ratings on first refresh. Wait ~1 min then hit /api/refresh.

---

## How to Run
```bash
# Start the app (ALWAYS use this command — NOT python3 app.py)
venv/bin/python3 -c "import app as a; a.app.run(debug=False, port=5001, use_reloader=False)"

# Kill port 5001 if already in use
kill $(lsof -ti:5001)

# Reset corrupted score history
echo '{}' > score_history.json

# Force fresh data fetch
rm stock_data.json && [restart app]

# Force fresh fundamentals fetch
rm fundamentals_data.json && [restart app]

# Restore original UI design
cp templates/index.html.bak templates/index.html && [restart app]
```

---

## Current Watchlist
```
ALGT, ECPG, JILL, ENVA, OMF, FCFS, EZPW, ATLC, LC, AUNA,
BTSG, ASTS, BE, LUMN, NWPX, LCUT, AFYA, ALTO, PRAA, INVA,
SATS, IRDM, PAHC
```

---

## Dependencies
```
flask
apscheduler
requests
yfinance
pandas
vaderSentiment
```
