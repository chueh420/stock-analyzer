import os
from datetime import datetime, timedelta

import httpx

BASE = "https://api.finmindtrade.com/api/v4/data"
TOKEN = os.getenv("FINMIND_TOKEN", "")
FUGLE_TOKEN = os.getenv("FUGLE_TOKEN", "")
_info_cache: dict[str, dict] = {}  # {name, type}
_all_stocks_cache: list[dict] = []  # [{stock_id, stock_name, type}]


def _start(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


async def _fetch(dataset: str, stock_id: str, days: int) -> list:
    params = {"dataset": dataset, "data_id": stock_id, "start_date": _start(days)}
    if TOKEN:
        params["token"] = TOKEN
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(BASE, params=params)
        r.raise_for_status()
        body = r.json()
        return body.get("data", []) if body.get("status") == 200 else []


async def get_stock_info(stock_id: str) -> dict:
    if stock_id in _info_cache:
        return _info_cache[stock_id]
    try:
        params = {"dataset": "TaiwanStockInfo", "data_id": stock_id}
        if TOKEN:
            params["token"] = TOKEN
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(BASE, params=params)
            body = r.json()
            data = body.get("data", [])
            if data:
                info = {
                    "name": data[0].get("stock_name", stock_id),
                    "type": data[0].get("type", "twse"),
                }
                _info_cache[stock_id] = info
                return info
    except Exception:
        pass
    info = {"name": stock_id, "type": "twse"}
    _info_cache[stock_id] = info
    return info


async def get_stock_name(stock_id: str) -> str:
    info = await get_stock_info(stock_id)
    return info["name"]


async def get_all_stocks() -> list[dict]:
    """回傳台股全部股票清單，結果快取於記憶體（啟動後只抓一次）"""
    global _all_stocks_cache
    if _all_stocks_cache:
        return _all_stocks_cache
    try:
        params = {"dataset": "TaiwanStockInfo"}
        if TOKEN:
            params["token"] = TOKEN
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(BASE, params=params)
            body = r.json()
            data = body.get("data", [])
            _all_stocks_cache = [
                {
                    "stock_id": row.get("stock_id", ""),
                    "stock_name": row.get("stock_name", ""),
                    "type": row.get("type", "twse"),
                }
                for row in data
                if row.get("stock_id")
            ]
    except Exception:
        pass
    return _all_stocks_cache


async def get_twse_quote(stock_id: str, stock_type: str = "twse") -> dict:
    """TWSE 即時報價：委買/委賣5檔、現價、量"""
    ex = "tse" if stock_type == "twse" else "otc"
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex}_{stock_id}.tw&json=1&delay=0"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://mis.twse.com.tw/stock/",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=headers)
            body = r.json()
            if body.get("rtcode") == "0000" and body.get("msgArray"):
                return body["msgArray"][0]
    except Exception:
        pass
    return {}


async def get_yahoo_quote(stock_id: str) -> dict:
    """Yahoo Finance v7 即時報價：現價、昨收、開高低、量、1檔委買委賣（可跨國存取）"""
    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, params={"symbols": f"{stock_id}.TW"}, headers=headers)
            body = r.json()
            res = body.get("quoteResponse", {}).get("result", [])
            return res[0] if res else {}
    except Exception:
        pass
    return {}


async def get_yahoo_intraday(stock_id: str) -> dict:
    """Yahoo Finance 1分K（計算VWAP、大單偵測）"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock_id}.TW"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, params={"interval": "1m", "range": "1d"}, headers=headers)
            return r.json()
    except Exception:
        pass
    return {}


async def get_price(stock_id: str, days: int = 120) -> list:
    return await _fetch("TaiwanStockPrice", stock_id, days)


async def get_institutional(stock_id: str, days: int = 60) -> list:
    rows = await _fetch("TaiwanStockInstitutionalInvestorsBuySell", stock_id, days)
    for r in rows:
        r["buy_sell"] = r.get("buy", 0) - r.get("sell", 0)
    return rows


async def get_margin(stock_id: str, days: int = 60) -> list:
    return await _fetch("TaiwanStockMarginPurchaseShortSale", stock_id, days)


async def get_fugle_quote(stock_id: str) -> dict:
    """Fugle MarketData v1.0 即時報價含委買委賣5檔（需設定 FUGLE_TOKEN 環境變數）"""
    if not FUGLE_TOKEN:
        return {}
    url = f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{stock_id}"
    headers = {"Authorization": f"Bearer {FUGLE_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=headers)
            if r.status_code != 200:
                return {}
            body = r.json()
            bids = body.get("bids", body.get("bidOrders", []))
            asks = body.get("asks", body.get("askOrders", []))
            return {
                "current":   body.get("close"),
                "yesterday": body.get("previousClose"),
                "open":      body.get("open"),
                "high":      body.get("high"),
                "low":       body.get("low"),
                "volume":    body.get("volume"),  # Fugle 已是張
                "bid_prices": [b["price"] for b in bids],
                "bid_vols":   [b.get("size", b.get("unit", 0)) for b in bids],
                "ask_prices": [a["price"] for a in asks],
                "ask_vols":   [a.get("size", a.get("unit", 0)) for a in asks],
            }
    except Exception:
        return {}


async def get_fugle_trades(stock_id: str, limit: int = 100) -> list:
    """Fugle 當日即時成交明細（最近 N 筆）"""
    if not FUGLE_TOKEN:
        return []
    url = f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/trades/{stock_id}"
    headers = {"Authorization": f"Bearer {FUGLE_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=headers)
            if r.status_code != 200:
                return []
            body = r.json()
            trades = body if isinstance(body, list) else body.get("data", body.get("trades", []))
            result = []
            prev_price = None
            for t in trades:
                price = t.get("price") or t.get("close")
                vol   = t.get("volume") or t.get("size") or 0
                at    = t.get("at") or t.get("time") or ""
                bid   = t.get("bid")
                ask   = t.get("ask")
                if bid and ask:
                    side = "買" if price >= ask else ("賣" if price <= bid else "中性")
                elif prev_price is not None:
                    side = "買" if price > prev_price else ("賣" if price < prev_price else "中性")
                else:
                    side = "—"
                result.append({"at": at, "price": price, "volume": vol, "side": side})
                prev_price = price
            return result[-limit:]
    except Exception:
        return []
