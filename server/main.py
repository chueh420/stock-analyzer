import asyncio
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates

from analysis import analyze_live, calc_ma, calc_macd, calc_rsi, chip_score, is_market_open, realtime_signal
from finmind_api import (get_all_stocks, get_fugle_quote, get_fugle_trades,
                         get_institutional, get_margin, get_price,
                         get_stock_info, get_stock_name,
                         get_twse_quote, get_yahoo_intraday, get_yahoo_quote)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

app = FastAPI()
templates = Jinja2Templates(directory="templates")

_WATCHLIST_PATHS = [Path("/data/watchlist.json"), Path("./watchlist.json")]


def _wl_path() -> Path:
    for p in _WATCHLIST_PATHS:
        if p.exists():
            return p
    # 優先寫 /data，若不存在則用當前目錄
    return _WATCHLIST_PATHS[0] if Path("/data").exists() else _WATCHLIST_PATHS[1]


def load_wl() -> list:
    for p in _WATCHLIST_PATHS:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return []   # 找不到檔案時回傳空清單，不套用預設股票


def save_wl(stocks: list):
    p = _wl_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(stocks, ensure_ascii=False), encoding="utf-8")


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
    """即時監控：回傳全部自選股狀態"""
    stocks = load_wl()

    async def check_one(sid: str) -> dict:
        try:
            info = await get_stock_info(sid)
            yf = await get_yahoo_intraday(sid)
            live = analyze_live({}, yf)

            avg_v = live.get("avg_min_vol") or 0
            latest_v = live.get("latest_min_vol") or 0
            max_v = live.get("max_min_vol") or 0

            is_recent = bool(avg_v and latest_v >= avg_v * 3)
            has_spike = bool(avg_v and max_v >= avg_v * 5)
            spike_vol = int(latest_v if is_recent else max_v) if (is_recent or has_spike) else 0

            cur = live.get("current")
            yest = live.get("yesterday")
            chg_pct = round((cur - yest) / yest * 100, 2) if cur and yest else None
            if cur and yest and cur > yest:
                direction, dir_color = "積極買進", "red"
            elif cur and yest and cur < yest:
                direction, dir_color = "出貨賣壓", "green"
            else:
                direction, dir_color = "觀望", "yellow"

            return {
                "stock_id": sid,
                "stock_name": info.get("name", sid),
                "current": cur,
                "chg_pct": chg_pct,
                "spike_vol": spike_vol,
                "avg_vol": int(avg_v) if avg_v else 0,
                "multiplier": round(spike_vol / avg_v, 1) if avg_v and spike_vol else 0,
                "is_recent": is_recent,
                "has_spike": has_spike,
                "direction": direction,
                "dir_color": dir_color,
            }
        except Exception:
            return {"stock_id": sid, "stock_name": sid, "error": True,
                    "current": None, "chg_pct": None, "spike_vol": 0, "avg_vol": 0,
                    "multiplier": 0, "is_recent": False, "has_spike": False,
                    "direction": "—", "dir_color": "gray"}

    results = await asyncio.gather(*[check_one(s) for s in stocks])
    return {
        "market_open": is_market_open(),
        "alerts": list(results),
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


@app.get("/api/trades/{stock_id}")
async def api_trades(stock_id: str):
    """Fugle 即時成交明細，最近 100 筆"""
    return await get_fugle_trades(stock_id, 100)


@app.get("/api/ai-judge/{stock_id}")
async def api_ai_judge(stock_id: str):
    """Claude AI 當沖方向判斷：做多 / 做空 / 觀望"""
    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY 未設定", "direction": "—", "dir_color": "gray",
                "confidence": "—", "summary": "請在 Railway 設定 ANTHROPIC_API_KEY", "reasons": [], "risk": "—"}

    info = await get_stock_info(stock_id)
    quote, yf, fugle, trades = await asyncio.gather(
        get_twse_quote(stock_id, info.get("type", "twse")),
        get_yahoo_intraday(stock_id),
        get_fugle_quote(stock_id),
        get_fugle_trades(stock_id, 100),
    )
    live = analyze_live(quote, yf, fugle_quote=fugle)

    buy_count = sum(1 for t in trades if t.get("side") == "買")
    sell_count = sum(1 for t in trades if t.get("side") == "賣")
    buy_vol = sum(t.get("volume", 0) for t in trades if t.get("side") == "買")
    sell_vol = sum(t.get("volume", 0) for t in trades if t.get("side") == "賣")

    cur = live.get("current")
    yest = live.get("yesterday")
    chg_pct = round((cur - yest) / yest * 100, 2) if cur and yest else None
    chg_str = f"{'+' if chg_pct and chg_pct > 0 else ''}{chg_pct}%" if chg_pct is not None else "—"
    above_vwap = live.get("above_vwap")
    vwap_str = "高於VWAP（強勢）" if above_vwap is True else "低於VWAP（弱勢）" if above_vwap is False else "等於VWAP"
    vol_trend = live.get("recent_vol_trend") or 1.0

    prompt = f"""你是台股當沖交易分析助手。根據以下即時數據判斷短線方向（分鐘到小時的當沖框架）。

【即時行情】
股票：{info.get('name', stock_id)}（{stock_id}）
現價：{cur}，昨收：{yest}，漲跌：{chg_str}
VWAP：{live.get('vwap')}，現價{vwap_str}
委買/委賣比：{live.get('bid_ask_ratio')}（>2.0=大量買盤，<0.5=大量賣盤）
近5分鐘量能趨勢：{vol_trend:.2f}x（>1.5=加速，<0.6=萎縮）

【近{len(trades)}筆成交明細】
主動買進：{buy_count} 筆，合計 {buy_vol} 張
主動賣出：{sell_count} 筆，合計 {sell_vol} 張
買賣量比：{round(buy_vol/sell_vol, 2) if sell_vol > 0 else 'N/A'}

【系統訊號】
{'; '.join(live.get('signals', [])) or '無'}
系統初判：{live.get('direction')}，{live.get('big_player')}

請用繁體中文回答，只回傳 JSON，格式如下：
{{"direction":"做多","confidence":"高","summary":"買盤積極量能加速做多","reasons":["委買委賣比2.3買盤積極","主動買進量佔65%","現價站上VWAP"],"risk":"委賣反壓留意"}}

direction 只能是「做多」「做空」「觀望」其中一個，confidence 只能是「高」「中」「低」。"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text)
        dir_map = {"做多": "red", "做空": "cyan", "觀望": "gray"}
        result["dir_color"] = dir_map.get(result.get("direction", "觀望"), "gray")
        tst = timezone(timedelta(hours=8))
        result["timestamp"] = datetime.now(tst).strftime("%H:%M:%S")
        return result
    except Exception as e:
        return {"direction": "分析失敗", "dir_color": "gray", "confidence": "—",
                "summary": f"錯誤：{str(e)[:60]}", "reasons": [], "risk": "—",
                "timestamp": datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")}


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
