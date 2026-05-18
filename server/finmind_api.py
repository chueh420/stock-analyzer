import os
from datetime import datetime, timedelta, timezone

import httpx

BASE = "https://api.finmindtrade.com/api/v4/data"
TOKEN = os.getenv("FINMIND_TOKEN", "")
FUGLE_TOKEN = os.getenv("FUGLE_TOKEN", "")
_info_cache: dict[str, dict] = {}
_all_stocks_cache: list[dict] = []


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


# ── TWSE 法人/融資（月報 API）────────────────────────────────────────────────

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


async def get_twse_institutional(stock_id: str, months: int = 3) -> list:
    """TWSE 三大法人月報，轉換為 FinMind 格式"""
    rows = []
    now = datetime.now()
    seen_dates: set[str] = set()
    for i in range(months):
        dt = (now.replace(day=1) - timedelta(days=i * 28)).replace(day=1)
        body = await _fetch_twse(
            "https://www.twse.com.tw/fund/TWT38U",
            {"response": "json", "date": dt.strftime("%Y%m01"), "stockNo": stock_id},
        )
        data = body.get("data", [])
        fields = body.get("fields", [])
        if not data or not fields:
            continue
        # 找欄位索引
        try:
            fi_buy   = next(i for i, f in enumerate(fields) if "外陸資買進" in f and "自營" not in f)
            fi_sell  = next(i for i, f in enumerate(fields) if "外陸資賣出" in f and "自營" not in f)
            fi_tbuy  = next(i for i, f in enumerate(fields) if "投信買進" in f)
            fi_tsell = next(i for i, f in enumerate(fields) if "投信賣出" in f)
            fi_dbuy  = next(i for i, f in enumerate(fields) if "自營商買進股數(自行" in f)
            fi_dsell = next(i for i, f in enumerate(fields) if "自營商賣出股數(自行" in f)
            fi_hbuy  = next(i for i, f in enumerate(fields) if "自營商買進股數(避險" in f)
            fi_hsell = next(i for i, f in enumerate(fields) if "自營商賣出股數(避險" in f)
        except StopIteration:
            continue
        for row in data:
            date = _twse_date(row[0])
            if not date or date in seen_dates:
                continue
            seen_dates.add(date)
            fb = _parse_num(row[fi_buy])
            fs = _parse_num(row[fi_sell])
            tb = _parse_num(row[fi_tbuy])
            ts = _parse_num(row[fi_tsell])
            db = _parse_num(row[fi_dbuy])
            ds = _parse_num(row[fi_dsell])
            hb = _parse_num(row[fi_hbuy])
            hs = _parse_num(row[fi_hsell])
            rows += [
                {"date": date, "name": "Foreign_Investor", "buy": fb, "sell": fs, "buy_sell": fb - fs},
                {"date": date, "name": "Investment_Trust",  "buy": tb, "sell": ts, "buy_sell": tb - ts},
                {"date": date, "name": "Dealer_self",       "buy": db, "sell": ds, "buy_sell": db - ds},
                {"date": date, "name": "Dealer_Hedging",    "buy": hb, "sell": hs, "buy_sell": hb - hs},
            ]
    return sorted(rows, key=lambda r: r["date"])


async def get_twse_margin(stock_id: str, months: int = 3) -> list:
    """TWSE 融資融券月報，轉換為 FinMind 格式"""
    rows = []
    now = datetime.now()
    seen_dates: set[str] = set()
    for i in range(months):
        dt = (now.replace(day=1) - timedelta(days=i * 28)).replace(day=1)
        body = await _fetch_twse(
            "https://www.twse.com.tw/exchangeReport/MARGIN_PURCHASE_SHORT_SALE",
            {"response": "json", "date": dt.strftime("%Y%m01"), "stockNo": stock_id},
        )
        data = body.get("data", [])
        fields = body.get("fields", [])
        if not data or not fields:
            continue
        try:
            fi_mbuy  = next(i for i, f in enumerate(fields) if f == "融資買進")
            fi_msell = next(i for i, f in enumerate(fields) if f == "融資賣出")
            fi_mbal  = next(i for i, f in enumerate(fields) if f == "融資餘額")
            fi_sbuy  = next(i for i, f in enumerate(fields) if f == "融券買進")
            fi_ssell = next(i for i, f in enumerate(fields) if f == "融券賣出")
            fi_sbal  = next(i for i, f in enumerate(fields) if f == "融券餘額")
        except StopIteration:
            continue
        for row in data:
            date = _twse_date(row[0])
            if not date or date in seen_dates:
                continue
            seen_dates.add(date)
            rows.append({
                "date": date,
                "MarginPurchaseBuy":          int(_parse_num(row[fi_mbuy])),
                "MarginPurchaseSell":         int(_parse_num(row[fi_msell])),
                "MarginPurchaseTodayBalance": int(_parse_num(row[fi_mbal])),
                "ShortSaleBuy":               int(_parse_num(row[fi_sbuy])),
                "ShortSaleSell":              int(_parse_num(row[fi_ssell])),
                "ShortSaleTodayBalance":      int(_parse_num(row[fi_sbal])),
            })
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
                    sid = row.get("有價證券代號", row.get("股票代號", "")).strip()
                    name = row.get("有價證券名稱", row.get("公司簡稱", "")).strip()
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
    months = max(2, (days // 22) + 1)
    rows = await get_twse_institutional(stock_id, months)
    if rows:
        cutoff = _start(days)
        return [r for r in rows if r["date"] >= cutoff]
    return await _fetch("TaiwanStockInstitutionalInvestorsBuySell", stock_id, days)


async def get_margin(stock_id: str, days: int = 60) -> list:
    months = max(2, (days // 22) + 1)
    rows = await get_twse_margin(stock_id, months)
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
