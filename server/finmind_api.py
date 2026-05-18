import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx

BASE = "https://api.finmindtrade.com/api/v4/data"
TOKEN = os.getenv("FINMIND_TOKEN", "")
FUGLE_TOKEN = os.getenv("FUGLE_TOKEN", "")
_info_cache: dict[str, dict] = {}
_all_stocks_cache: list[dict] = []
_t86_cache: dict[str, list] = {}
_margn_cache: dict[str, list] = {}
_t86_locks: dict[str, asyncio.Lock] = {}
_margn_locks: dict[str, asyncio.Lock] = {}


def _start(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _parse_num(s) -> float:
    try:
        return float(str(s).replace(",", "").strip())
    except Exception:
        return 0.0


def _twse_date(s: str) -> str:
    """民國日期 '115/05/01' → ISO '2026-05-01'"""
    try:
        parts = str(s).split("/")
        return f"{int(parts[0]) + 1911}-{parts[1]}-{parts[2]}"
    except Exception:
        return ""


async def _fetch(dataset: str, stock_id: str, days: int) -> list:
    """FinMind API，402/錯誤時回傳空清單"""
    params = {"dataset": dataset, "data_id": stock_id, "start_date": _start(days)}
    if TOKEN:
        params["token"] = TOKEN
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(BASE, params=params)
            if r.status_code in (402, 401, 403):
                return []
            r.raise_for_status()
            body = r.json()
            return body.get("data", []) if body.get("status") == 200 else []
    except Exception:
        return []


# ── Yahoo Finance 日K（主要價格來源）─────────────────────────────────────────

async def get_yahoo_history(stock_id: str, days: int = 120) -> list:
    """Yahoo Finance 日K線，轉換為 FinMind 格式"""
    range_str = "1y" if days > 180 else "6mo"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock_id}.TW"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, params={"interval": "1d", "range": range_str}, headers=headers)
            body = r.json()
            res = body.get("chart", {}).get("result", [{}])[0]
            timestamps = res.get("timestamp", [])
            q = res.get("indicators", {}).get("quote", [{}])[0]
            opens = q.get("open", [])
            highs = q.get("high", [])
            lows = q.get("low", [])
            closes = q.get("close", [])
            volumes = q.get("volume", [])
            rows = []
            for ts, o, h, l, c, v in zip(timestamps, opens, highs, lows, closes, volumes):
                if c is None:
                    continue
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                rows.append({
                    "date": dt.strftime("%Y-%m-%d"),
                    "open": round(float(o), 2) if o else None,
                    "max": round(float(h), 2) if h else None,
                    "min": round(float(l), 2) if l else None,
                    "close": round(float(c), 2),
                    "Trading_Volume": int(v) * 1000 if v else 0,
                })
            return rows[-days:] if len(rows) > days else rows
    except Exception:
        return []


# ── TWSE API 工具函式 ─────────────────────────────────────────────────────────

async def _fetch_twse(url: str, params: dict) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.twse.com.tw/",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, params=params, headers=headers)
            if r.status_code != 200:
                return {}
            body = r.json()
            if body.get("stat") not in ("OK", "ok"):
                return {}
            return body
    except Exception:
        return {}


def _recent_weekdays(n: int) -> list:
    """生成最近 n 個工作日（週一到週五，格式 YYYYMMDD）"""
    dates = []
    d = datetime.now()
    while len(dates) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            dates.append(d.strftime("%Y%m%d"))
    return dates


async def _get_t86(date_str: str) -> list:
    """抓取 T86 當日全市場三大法人，帶快取避免重複請求"""
    if date_str in _t86_cache:
        return _t86_cache[date_str]
    if date_str not in _t86_locks:
        _t86_locks[date_str] = asyncio.Lock()
    async with _t86_locks[date_str]:
        if date_str in _t86_cache:
            return _t86_cache[date_str]
        body = await _fetch_twse(
            "https://www.twse.com.tw/fund/T86",
            {"response": "json", "date": date_str, "selectType": "ALLBUT0999"},
        )
        data = body.get("data", [])
        _t86_cache[date_str] = data
        return data


async def _get_margn(date_str: str) -> list:
    """抓取 MI_MARGN 當日全市場融資融券，帶快取避免重複請求"""
    if date_str in _margn_cache:
        return _margn_cache[date_str]
    if date_str not in _margn_locks:
        _margn_locks[date_str] = asyncio.Lock()
    async with _margn_locks[date_str]:
        if date_str in _margn_cache:
            return _margn_cache[date_str]
        body = await _fetch_twse(
            "https://www.twse.com.tw/exchangeReport/MI_MARGN",
            {"response": "json", "date": date_str, "selectType": "ALL"},
        )
        # 回傳 (data, fields) tuple 存快取，方便欄位解析
        result = (body.get("data", []), body.get("fields", []))
        _margn_cache[date_str] = result
        return result


async def get_twse_institutional(stock_id: str, max_days: int = 20) -> list:
    """TWSE T86 三大法人每日，並行抓取最近 max_days 個工作日"""
    async def _one(date_str: str) -> list:
        data = await _get_t86(date_str)
        for row in data:
            if len(row) > 16 and row[0].strip() == stock_id:
                dt = datetime.strptime(date_str, "%Y%m%d").strftime("%Y-%m-%d")
                fb = _parse_num(row[2]);  fs = _parse_num(row[3])
                tb = _parse_num(row[8]);  ts = _parse_num(row[9])
                db = _parse_num(row[12]); ds = _parse_num(row[13])
                hb = _parse_num(row[15]); hs = _parse_num(row[16])
                return [
                    {"date": dt, "name": "Foreign_Investor", "buy": fb, "sell": fs, "buy_sell": fb - fs},
                    {"date": dt, "name": "Investment_Trust",  "buy": tb, "sell": ts, "buy_sell": tb - ts},
                    {"date": dt, "name": "Dealer_self",       "buy": db, "sell": ds, "buy_sell": db - ds},
                    {"date": dt, "name": "Dealer_Hedging",    "buy": hb, "sell": hs, "buy_sell": hb - hs},
                ]
        return []

    all_results = await asyncio.gather(*[_one(d) for d in _recent_weekdays(max_days)])
    rows = [r for sub in all_results for r in sub]
    return sorted(rows, key=lambda r: r["date"])


async def get_twse_margin(stock_id: str, max_days: int = 20) -> list:
    """TWSE MI_MARGN 融資融券每日，並行抓取最近 max_days 個工作日"""
    async def _one(date_str: str):
        result = await _get_margn(date_str)
        data, fields = result if isinstance(result, tuple) else (result, [])
        if not data or not fields:
            return None
        try:
            fi_mbuy  = next(i for i, f in enumerate(fields) if "融資買進" in f)
            fi_msell = next(i for i, f in enumerate(fields) if "融資賣出" in f)
            fi_mbal  = next(i for i, f in enumerate(fields) if "融資餘額" in f)
            fi_sbuy  = next(i for i, f in enumerate(fields) if "融券買進" in f)
            fi_ssell = next(i for i, f in enumerate(fields) if "融券賣出" in f)
            fi_sbal  = next(i for i, f in enumerate(fields) if "融券餘額" in f)
        except StopIteration:
            return None
        for row in data:
            if not row or row[0].strip() != stock_id:
                continue
            dt = datetime.strptime(date_str, "%Y%m%d").strftime("%Y-%m-%d")
            return {
                "date":                       dt,
                "MarginPurchaseBuy":          int(_parse_num(row[fi_mbuy])),
                "MarginPurchaseSell":         int(_parse_num(row[fi_msell])),
                "MarginPurchaseTodayBalance": int(_parse_num(row[fi_mbal])),
                "ShortSaleBuy":               int(_parse_num(row[fi_sbuy])),
                "ShortSaleSell":              int(_parse_num(row[fi_ssell])),
                "ShortSaleTodayBalance":      int(_parse_num(row[fi_sbal])),
            }
        return None

    all_results = await asyncio.gather(*[_one(d) for d in _recent_weekdays(max_days)])
    rows = [r for r in all_results if r]
    return sorted(rows, key=lambda r: r["date"])


# ── 股票資訊 / 搜尋 ──────────────────────────────────────────────────────────

_TWSE_STOCKS_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
_TPEX_STOCKS_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_R"


async def get_stock_info(stock_id: str) -> dict:
    if stock_id in _info_cache:
        return _info_cache[stock_id]
    # 嘗試從全股票清單中找
    stocks = await get_all_stocks()
    for s in stocks:
        if s["stock_id"] == stock_id:
            info = {"name": s["stock_name"], "type": s["type"]}
            _info_cache[stock_id] = info
            return info
    info = {"name": stock_id, "type": "twse"}
    _info_cache[stock_id] = info
    return info


async def get_stock_name(stock_id: str) -> str:
    info = await get_stock_info(stock_id)
    return info["name"]


async def get_all_stocks() -> list[dict]:
    global _all_stocks_cache
    if _all_stocks_cache:
        return _all_stocks_cache
    results = []
    headers = {"User-Agent": "Mozilla/5.0"}
    for url, stype in [(_TWSE_STOCKS_URL, "twse"), (_TPEX_STOCKS_URL, "otc")]:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(url, headers=headers)
                for row in r.json():
                    sid = (row.get("有價證券代號") or row.get("公司代號") or row.get("股票代號") or "").strip()
                    name = (row.get("有價證券名稱") or row.get("公司名稱") or row.get("公司簡稱") or "").strip()
                    if sid and name:
                        results.append({"stock_id": sid, "stock_name": name, "type": stype})
        except Exception:
            pass
    if results:
        _all_stocks_cache = results
    else:
        # 最後回退：嘗試 FinMind
        rows = await _fetch("TaiwanStockInfo", "", 1)
        _all_stocks_cache = [
            {"stock_id": r.get("stock_id", ""), "stock_name": r.get("stock_name", ""), "type": r.get("type", "twse")}
            for r in rows if r.get("stock_id")
        ]
    return _all_stocks_cache


# ── 主要資料 API（外部呼叫點）────────────────────────────────────────────────

async def get_price(stock_id: str, days: int = 120) -> list:
    rows = await get_yahoo_history(stock_id, days)
    if rows:
        return rows
    return await _fetch("TaiwanStockPrice", stock_id, days)


async def get_institutional(stock_id: str, days: int = 60) -> list:
    # 最多抓 20 個工作日（約 1 個月），避免對 TWSE 發送過多請求
    fetch_days = min(max(days * 5 // 7, 10), 20)
    rows = await get_twse_institutional(stock_id, fetch_days)
    if rows:
        cutoff = _start(days)
        return [r for r in rows if r["date"] >= cutoff]
    return await _fetch("TaiwanStockInstitutionalInvestorsBuySell", stock_id, days)


async def get_margin(stock_id: str, days: int = 60) -> list:
    fetch_days = min(max(days * 5 // 7, 10), 20)
    rows = await get_twse_margin(stock_id, fetch_days)
    if rows:
        cutoff = _start(days)
        return [r for r in rows if r["date"] >= cutoff]
    rows = await _fetch("TaiwanStockMarginPurchaseShortSale", stock_id, days)
    for r in rows:
        r["buy_sell"] = r.get("buy", 0) - r.get("sell", 0)
    return rows


# ── 即時行情（保持不變）──────────────────────────────────────────────────────

async def get_twse_quote(stock_id: str, stock_type: str = "twse") -> dict:
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
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock_id}.TW"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, params={"interval": "1m", "range": "1d"}, headers=headers)
            return r.json()
    except Exception:
        pass
    return {}


async def get_fugle_quote(stock_id: str) -> dict:
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
                "current":    body.get("close"),
                "yesterday":  body.get("previousClose"),
                "open":       body.get("open"),
                "high":       body.get("high"),
                "low":        body.get("low"),
                "volume":     body.get("volume"),
                "bid_prices": [b["price"] for b in bids],
                "bid_vols":   [b.get("size", b.get("unit", 0)) for b in bids],
                "ask_prices": [a["price"] for a in asks],
                "ask_vols":   [a.get("size", a.get("unit", 0)) for a in asks],
            }
    except Exception:
        return {}


async def get_fugle_trades(stock_id: str, limit: int = 100) -> list:
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
