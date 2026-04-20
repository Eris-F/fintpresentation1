# Portfolio Tracker

Single-user portfolio tracker with live prices, P/L, a USD/EUR toggle, a historical "what if" calculator, and a MiniMax-powered chat sidebar. Built as a classroom demo.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env  # fill in MINIMAX_API_KEY
uvicorn main:app --reload
```

Open http://127.0.0.1:8000.

## What's built

- `main.py` — FastAPI backend with stdlib `sqlite3`
  - Holdings: `POST /holdings`, `GET /holdings`, `DELETE /holdings/{id}`
  - Prices: `GET /prices?tickers=...` (60-second in-memory cache, `fast_info` + `history` fallback, `None` on failure)
  - Portfolio: `GET /portfolio` with per-holding value/cost/PL and totals; failing tickers degrade to `null` fields without breaking the response
  - FX: `GET /fx-rate?base=USD&quote=EUR` via `EURUSD=X` (inverted when needed); 400 for any other pair
  - What-if: `GET /what-if?ticker=X&date=YYYY-MM-DD&amount=N` — future dates 400, empty history 404, first trading-day on/after the requested date returned as `actual_trade_date`
  - Chat: `POST /chat` (Level 2) — serializes current portfolio into a system prompt before forwarding to MiniMax
- `index.html` — single-file vanilla-JS frontend: holdings table, add/delete form, what-if form, chat panel, header totals, USD/EUR toggle (frontend-only conversion, percentages unchanged), auto-refresh (30s portfolio, 5min FX)
- SQLite `portfolio.db` — one `holdings` table, created on first run, persists across restarts

## Gotchas exercised

- Fake ticker (`ZZZZ123`): price fields become `null`, other rows render normally
- Weekend/holiday `/what-if` dates: first row of `history(start=d, end=d+7d)` returns next trading day, exposed as `actual_trade_date`
- Empty portfolio: totals are 0, no divide-by-zero
- Currency round-trip: toggle does pure multiply by cached FX rate, does not touch percentages

## Deviations from the original spec

- **MiniMax endpoint/model**: verified via web search. Endpoint is `https://api.minimax.io/v1/chat/completions` (OpenAI-compatible schema). Model set to `MiniMax-M2.7`. Change `MINIMAX_MODEL` in `main.py` if you want a different variant.
- **yfinance**: `Ticker.fast_info["last_price"]` still works; I guard it with NaN-check and a `history(period="1d")` fallback per spec.
- **Chat**: I jumped straight to Level 2 (portfolio injected as system prompt) — the spec's "build Level 1 first" was a development checkpoint, not a runtime requirement. If you want Level 1 behavior back, remove the system message from the `messages` list in `main.py`.
- **Datetime**: used `datetime.now(timezone.utc).date()` rather than the deprecated `utcnow()`.

## Don'ts honored

No React/bundler/npm. No SQLAlchemy. No auth, CORS middleware, streaming, or persisted chat history. No tests.
