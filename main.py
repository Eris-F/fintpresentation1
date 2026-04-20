import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yfinance as yf
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

load_dotenv()

log = logging.getLogger("uvicorn.error")

DB_PATH = Path(__file__).parent / "portfolio.db"
INDEX_PATH = Path(__file__).parent / "index.html"

MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")
MINIMAX_URL = "https://api.minimax.io/v1/chat/completions"
MINIMAX_MODEL = "MiniMax-M2.7"

app = FastAPI()


# ---------- DB ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS holdings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                shares REAL NOT NULL,
                avg_cost REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


init_db()


# ---------- Price cache ----------

_price_cache: dict[str, tuple[float, float | None]] = {}
PRICE_TTL = 60.0

_history_cache: dict[tuple[str, int], tuple[float, list[float]]] = {}
HISTORY_TTL = 300.0


def get_price(ticker: str) -> float | None:
    now = time.time()
    cached = _price_cache.get(ticker)
    if cached and now - cached[0] < PRICE_TTL:
        return cached[1]

    price: float | None = None
    try:
        t = yf.Ticker(ticker)
        try:
            p = t.fast_info["last_price"]
            if p is not None and p == p:  # not NaN
                price = float(p)
        except Exception:
            price = None
        if price is None:
            hist = t.history(period="1d")
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
    except Exception:
        price = None

    _price_cache[ticker] = (now, price)
    return price


# ---------- Models ----------

class HoldingIn(BaseModel):
    ticker: str = Field(min_length=1, max_length=16)
    shares: float = Field(gt=0)
    avg_cost: float = Field(gt=0)


class ChatIn(BaseModel):
    message: str = Field(min_length=1)


# ---------- Routes: static + holdings ----------

@app.get("/")
def root():
    if INDEX_PATH.exists():
        return FileResponse(INDEX_PATH)
    return {"status": "ok"}


@app.post("/holdings", status_code=201)
def create_holding(h: HoldingIn):
    ticker = h.ticker.strip().upper()
    if not ticker:
        raise HTTPException(400, "ticker required")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO holdings (ticker, shares, avg_cost) VALUES (?, ?, ?)",
            (ticker, h.shares, h.avg_cost),
        )
        row = conn.execute(
            "SELECT * FROM holdings WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)


@app.get("/holdings")
def list_holdings():
    with db() as conn:
        rows = conn.execute("SELECT * FROM holdings ORDER BY id").fetchall()
    return [dict(r) for r in rows]


@app.delete("/holdings/{hid}", status_code=204)
def delete_holding(hid: int):
    with db() as conn:
        cur = conn.execute("DELETE FROM holdings WHERE id = ?", (hid,))
        if cur.rowcount == 0:
            raise HTTPException(404, "not found")


# ---------- Prices ----------

@app.get("/history")
def history(ticker: str, days: int = 30):
    ticker = ticker.strip().upper()
    days = max(5, min(days, 365))
    now = time.time()
    key = (ticker, days)
    cached = _history_cache.get(key)
    if cached and now - cached[0] < HISTORY_TTL:
        return {"ticker": ticker, "closes": cached[1]}

    closes: list[float] = []
    try:
        # pull a bit extra for weekends/holidays, then keep last `days` closes
        hist = yf.Ticker(ticker).history(period=f"{days + 15}d")
        if not hist.empty:
            closes = [float(x) for x in hist["Close"].dropna().tolist()[-days:]]
    except Exception:
        closes = []

    _history_cache[key] = (now, closes)
    return {"ticker": ticker, "closes": closes}


@app.get("/prices")
def prices(tickers: str):
    out: dict[str, float | None] = {}
    for t in [x.strip().upper() for x in tickers.split(",") if x.strip()]:
        out[t] = get_price(t)
    return out


# ---------- Portfolio ----------

def _round(x: float | None) -> float | None:
    return None if x is None else round(x, 2)


@app.get("/portfolio")
def portfolio():
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM holdings ORDER BY id").fetchall()]

    total_value = 0.0
    total_cost = 0.0
    out_rows = []

    for r in rows:
        price = get_price(r["ticker"])
        cost_basis = r["shares"] * r["avg_cost"]
        if price is None:
            out_rows.append({
                "id": r["id"],
                "ticker": r["ticker"],
                "shares": r["shares"],
                "avg_cost": _round(r["avg_cost"]),
                "current_price": None,
                "value": None,
                "cost_basis": _round(cost_basis),
                "pl_absolute": None,
                "pl_percent": None,
            })
            total_cost += cost_basis
            continue

        value = r["shares"] * price
        pl_abs = value - cost_basis
        pl_pct = (pl_abs / cost_basis * 100) if cost_basis > 0 else 0.0
        total_value += value
        total_cost += cost_basis
        out_rows.append({
            "id": r["id"],
            "ticker": r["ticker"],
            "shares": r["shares"],
            "avg_cost": _round(r["avg_cost"]),
            "current_price": _round(price),
            "value": _round(value),
            "cost_basis": _round(cost_basis),
            "pl_absolute": _round(pl_abs),
            "pl_percent": _round(pl_pct),
        })

    total_pl = total_value - total_cost
    total_pl_pct = (total_pl / total_cost * 100) if total_cost > 0 else 0.0

    return {
        "holdings": out_rows,
        "total_value": _round(total_value),
        "total_cost": _round(total_cost),
        "total_pl": _round(total_pl),
        "total_pl_percent": _round(total_pl_pct),
        "currency": "USD",
    }


# ---------- FX ----------

@app.get("/fx-rate")
def fx_rate(base: str = "USD", quote: str = "EUR"):
    base = base.upper()
    quote = quote.upper()
    if {base, quote} != {"USD", "EUR"}:
        raise HTTPException(400, "only USD/EUR supported")

    eur_usd = get_price("EURUSD=X")
    if eur_usd is None or eur_usd == 0:
        raise HTTPException(502, "fx fetch failed")

    # EURUSD=X means price of 1 EUR in USD.
    if base == "USD" and quote == "EUR":
        rate = 1.0 / eur_usd
    elif base == "EUR" and quote == "USD":
        rate = eur_usd
    else:
        rate = 1.0
    return {"base": base, "quote": quote, "rate": round(rate, 6)}


# ---------- What-if ----------

@app.get("/what-if")
def what_if(ticker: str, date: str, amount: float):
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    if d >= datetime.now(timezone.utc).date():
        raise HTTPException(400, "date must be in the past")
    if amount <= 0:
        raise HTTPException(400, "amount must be positive")

    ticker = ticker.strip().upper()
    end = d + timedelta(days=7)
    try:
        hist = yf.Ticker(ticker).history(start=d.isoformat(), end=end.isoformat())
    except Exception:
        raise HTTPException(404, "ticker not found")
    if hist.empty:
        raise HTTPException(404, "no historical data for ticker/date")

    first_row = hist.iloc[0]
    hist_price = float(first_row["Close"])
    actual_date = hist.index[0].date().isoformat()

    current = get_price(ticker)
    if current is None:
        raise HTTPException(502, "current price fetch failed")

    shares = amount / hist_price
    current_value = shares * current
    pl_abs = current_value - amount
    pl_pct = pl_abs / amount * 100

    return {
        "ticker": ticker,
        "date": date,
        "actual_trade_date": actual_date,
        "amount_invested": round(amount, 2),
        "historical_price": round(hist_price, 2),
        "shares": round(shares, 6),
        "current_price": round(current, 2),
        "current_value": round(current_value, 2),
        "pl_absolute": round(pl_abs, 2),
        "pl_percent": round(pl_pct, 2),
    }


# ---------- Chat ----------

def _portfolio_summary_text() -> str:
    data = portfolio()
    lines = []
    for r in data["holdings"]:
        if r["current_price"] is None:
            lines.append(f"- {r['ticker']}: {r['shares']} shares, bought at ${r['avg_cost']:.2f}, current price unavailable")
        else:
            sign = "+" if (r["pl_percent"] or 0) >= 0 else ""
            lines.append(
                f"- {r['ticker']}: {r['shares']} shares, bought at ${r['avg_cost']:.2f}, "
                f"now ${r['current_price']:.2f} ({sign}{r['pl_percent']:.2f}%)"
            )
    if not lines:
        return "The portfolio is empty."
    total = data["total_value"] or 0
    pl = data["total_pl"] or 0
    pl_pct = data["total_pl_percent"] or 0
    sign = "+" if pl >= 0 else ""
    lines.append(f"Total value: ${total:.2f}. Total P/L: {sign}${pl:.2f} ({sign}{pl_pct:.2f}%).")
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "You are a concise assistant helping a user understand their stock portfolio. "
    "Answer questions about the held positions in 2-3 sentences. "
    "Use only the data provided below. Decline to give personalized investment advice.\n\n"
    "Current portfolio:\n{summary}"
)


@app.post("/chat")
async def chat(body: ChatIn):
    if not MINIMAX_API_KEY:
        raise HTTPException(502, "MINIMAX_API_KEY not configured")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(summary=_portfolio_summary_text())},
        {"role": "user", "content": body.message},
    ]

    payload = {
        "model": MINIMAX_MODEL,
        "messages": messages,
    }
    headers = {
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(MINIMAX_URL, json=payload, headers=headers)
    except Exception as e:
        log.error("MiniMax network error: %r", e)
        raise HTTPException(502, "chat upstream error")

    if resp.status_code >= 400:
        log.error("MiniMax HTTP %s: %s", resp.status_code, resp.text[:500])
        raise HTTPException(502, f"chat upstream {resp.status_code}")

    try:
        data = resp.json()
    except Exception as e:
        log.error("MiniMax non-JSON response: %r / body=%s", e, resp.text[:500])
        raise HTTPException(502, "chat upstream non-JSON")

    try:
        reply = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        log.error("MiniMax unexpected shape: %s", str(data)[:500])
        raise HTTPException(502, "unexpected chat response")

    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL)
    reply = re.sub(r"<thinking>.*?</thinking>", "", reply, flags=re.DOTALL)
    reply = reply.strip()

    return {"reply": reply}
