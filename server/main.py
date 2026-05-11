import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates

from analysis import analyze_live, calc_ma, calc_macd, calc_rsi, chip_score, is_market_open, realtime_signal
from finmind_api import (get_all_stocks, get_fugle_quote, get_institutional,
                         get_margin, get_price, get_stock_info, get_stock_name,
                         get_twse_quote, get_yahoo_intraday, get_yahoo_quote)

app = FastAPI()
templates = Jinja2Templates(directory="templates")
# Railway Volume 掛載在 /data，若不存在則用目前目錄（本機開發）
_data_dir = Path("/data") if Path("/data").exists() else Path(".")
WATCHLIST = _data_dir / "watchlist.json"
DEFAULT_STOCKS = ["2330", "2317", "2454"]


def load_wl() -> list:
    if WATCHLIST.exists():
        return json.loads(WATCHLIST.read_text(encoding="utf-8"))
    return DEFAULT_STOCKS[:]


def save_wl(stocks: list):
    WATCHLIST.write_text(json.dumps(stocks, ensure_ascii=False), encoding="utf-8")


@app.get("/")
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {"stocks": load_wl()})


@app.get("/stock/{stock_id}")
async def stock_page(request: Request, stock_id: str):
    return templates.TemplateResponse(request, "stock.html", {"stock_id": stock_id})


@app.get("/api/search")
async def api_search(q: str = ""):
    """股票代碼/名稱模糊搜尋，最多回傳 15 筆"""
    q = q.strip()
    if not q:
        return []
    stocks = await get_all_stocks()
    ql = q.lower()
    seen: set[str] = set()
    results = []
    for s in stocks:
        if s["stock_id"] in seen:
            continue
        if ql in s["stock_id"].lower() or ql in s["stock_name"].lower():
            seen.add(s["stock_id"])
            results.append(s)
        if len(results) >= 15:
            break
    return results


@app.get("/api/stock/{stock_id}")
async def api_stock(stock_id: str):
    prices, inst, margin, name = await asyncio.gather(
        get_price(stock_id, 120),
        get_institutional(stock_id, 60),
        get_margin(stock_id, 60),
        get_stock_name(stock_id),
    )
    prices = [r for r in prices if r.get("close")]  # 過濾今日尚未收盤的不完整資料
    if not prices:
        raise HTTPException(404, "查無股票資料，請確認代碼是否正確")

    closes = [r["close"] for r in prices]
    ma5 = calc_ma(closes, 5)
    ma10 = calc_ma(closes, 10)
    ma20 = calc_ma(closes, 20)
    ma60 = calc_ma(closes, 60)
    rsi = calc_rsi(closes)

    return {
        "stock_id": stock_id,
        "stock_name": name,
        "price": prices,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ma60": ma60,
        "rsi": rsi,
        "macd": calc_macd(closes),
        "institutional": inst,
        "margin": margin,
        "chip": chip_score(inst, margin, prices),
        "realtime": realtime_signal(prices, inst, margin, ma5, ma20, ma60, rsi),
    }


@app.get("/api/scan")
async def api_scan():
    stocks = load_wl()

    async def scan_one(sid: str) -> dict:
        try:
            prices, inst, margin, name = await asyncio.gather(
                get_price(sid, 30),
                get_institutional(sid, 20),
                get_margin(sid, 20),
                get_stock_name(sid),
            )
            prices = [r for r in prices if r.get("close")]
            if not prices:
                return {"stock_id": sid, "stock_name": sid, "error": "查無資料"}

            latest = prices[-1]
            prev = prices[-2] if len(prices) >= 2 else prices[-1]
            change = latest["close"] - prev["close"]
            change_pct = change / prev["close"] * 100 if prev["close"] else 0

            foreign_bs = next(
                (r["buy_sell"] for r in reversed(inst) if r.get("name") == "Foreign_Investor"), 0
            )
            trust_bs = next(
                (r["buy_sell"] for r in reversed(inst) if r.get("name") == "Investment_Trust"), 0
            )
            margin_chg = 0
            if len(margin) >= 2:
                margin_chg = (
                    margin[-1].get("MarginPurchaseTodayBalance", 0)
                    - margin[-2].get("MarginPurchaseTodayBalance", 0)
                )

            closes = [r["close"] for r in prices]
            ma5 = calc_ma(closes, 5)
            ma20 = calc_ma(closes, 20)
            ma60 = calc_ma(closes, 60)
            rsi = calc_rsi(closes)
            rt = realtime_signal(prices, inst, margin, ma5, ma20, ma60, rsi)
            c = chip_score(inst, margin, prices)

            return {
                "stock_id": sid,
                "stock_name": name,
                "date": latest["date"],
                "close": latest["close"],
                "change": round(change, 2),
                "change_pct": round(change_pct, 2),
                "foreign_buy_sell": int(foreign_bs),
                "trust_buy_sell": int(trust_bs),
                "margin_change": int(margin_chg),
                "chip_score": c["score"],
                "chip_label": c["label"],
                "big_player": rt["big_player"],
                "bp_color": rt["bp_color"],
                "direction": rt["direction"],
                "dir_color": rt["dir_color"],
            }
        except Exception as e:
            return {"stock_id": sid, "stock_name": sid, "error": str(e)}

    results = await asyncio.gather(*[scan_one(s) for s in stocks])
    return list(results)


@app.get("/api/bigorder")
async def api_bigorder():
    """即時大單偵測：掃描自選股，回傳有大單出現的股票"""
    stocks = load_wl()

    async def check_one(sid: str) -> dict | None:
        try:
            info = await get_stock_info(sid)
            yf = await get_yahoo_intraday(sid)
            live = analyze_live({}, yf)

            avg_v = live.get("avg_min_vol") or 0
            latest_v = live.get("latest_min_vol") or 0
            max_v = live.get("max_min_vol") or 0
            if not avg_v:
                return None

            is_recent = latest_v >= avg_v * 3   # 最新這分鐘爆量
            has_spike = max_v >= avg_v * 5       # 今日曾出現大單
            if not (is_recent or has_spike):
                return None

            spike_vol = latest_v if is_recent else max_v
            last_bar_up = live.get("last_bar_up")
            if last_bar_up is True:
                direction, dir_color = "積極買進", "red"
            elif last_bar_up is False:
                direction, dir_color = "出貨賣壓", "green"
            else:
                direction, dir_color = "量能放大", "yellow"

            return {
                "stock_id": sid,
                "stock_name": info.get("name", sid),
                "spike_vol": int(spike_vol),
                "avg_vol": int(avg_v),
                "multiplier": round(spike_vol / avg_v, 1) if avg_v else 0,
                "is_recent": is_recent,
                "direction": direction,
                "dir_color": dir_color,
            }
        except Exception:
            return None

    results = await asyncio.gather(*[check_one(s) for s in stocks])
    return {
        "market_open": is_market_open(),
        "alerts": [r for r in results if r is not None],
    }


@app.get("/api/live/{stock_id}")
async def api_live(stock_id: str):
    """即時行情：TWSE掛單 + Yahoo分鐘K + Fugle委買委賣5檔，每5秒可呼叫"""
    info = await get_stock_info(stock_id)
    quote, yf, fugle = await asyncio.gather(
        get_twse_quote(stock_id, info.get("type", "twse")),
        get_yahoo_intraday(stock_id),
        get_fugle_quote(stock_id),
    )
    return analyze_live(quote, yf, fugle_quote=fugle)


@app.post("/api/watchlist/add")
async def add_stock(data: dict):
    sid = str(data.get("stock_id", "")).strip()
    if not sid:
        raise HTTPException(400, "請輸入股票代碼")
    stocks = load_wl()
    if sid not in stocks:
        stocks.append(sid)
        save_wl(stocks)
    return {"ok": True}


@app.delete("/api/watchlist/{stock_id}")
async def del_stock(stock_id: str):
    stocks = load_wl()
    if stock_id in stocks:
        stocks.remove(stock_id)
        save_wl(stocks)
    return {"ok": True}
