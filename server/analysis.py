from datetime import datetime, timezone, timedelta, time as time_type
from typing import Optional


def calc_ma(closes: list, period: int) -> list:
    result: list[Optional[float]] = []
    for i in range(len(closes)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(round(sum(closes[i - period + 1:i + 1]) / period, 2))
    return result


def calc_rsi(closes: list, period: int = 14) -> list:
    if len(closes) <= period:
        return [None] * len(closes)

    result: list[Optional[float]] = [None] * period
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, period + 1)]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, period + 1)]
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period

    def rsi(ag, al):
        return 100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 2)

    result.append(rsi(avg_g, avg_l))
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(change, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-change, 0)) / period
        result.append(rsi(avg_g, avg_l))
    return result


def calc_ema(closes: list, period: int) -> list:
    if len(closes) < period:
        return [None] * len(closes)
    k = 2 / (period + 1)
    result: list[Optional[float]] = [None] * (period - 1)
    result.append(round(sum(closes[:period]) / period, 4))
    for c in closes[period:]:
        result.append(round(c * k + result[-1] * (1 - k), 4))
    return result


def calc_macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    ema_f = calc_ema(closes, fast)
    ema_s = calc_ema(closes, slow)
    macd_line = [
        round(f - s, 4) if f is not None and s is not None else None
        for f, s in zip(ema_f, ema_s)
    ]
    valid = [(i, v) for i, v in enumerate(macd_line) if v is not None]
    sig_line: list[Optional[float]] = [None] * len(macd_line)
    if len(valid) >= signal:
        k = 2 / (signal + 1)
        seed_idx = valid[signal - 1][0]
        sig_line[seed_idx] = round(sum(v for _, v in valid[:signal]) / signal, 4)
        prev = seed_idx
        for i in range(signal, len(valid)):
            idx, val = valid[i]
            sig_line[idx] = round(val * k + sig_line[prev] * (1 - k), 4)
            prev = idx
    histogram = [
        round(m - s, 4) if m is not None and s is not None else None
        for m, s in zip(macd_line, sig_line)
    ]
    return {"macd": macd_line, "signal": sig_line, "histogram": histogram}


def chip_score(institutional: list, margin: list, prices: list) -> dict:
    score = 50
    signals = []

    foreign = [r for r in institutional if r.get("name") == "Foreign_Investor"]
    trust = [r for r in institutional if r.get("name") == "Investment_Trust"]

    if foreign:
        recent = foreign[-10:]
        buy_days = sum(1 for r in recent if r.get("buy_sell", 0) > 0)
        if buy_days >= 7:
            score += 15
            signals.append(f"外資近10日連續買超 {buy_days} 天")
        elif buy_days >= 5:
            score += 8
            signals.append(f"外資近10日買超 {buy_days} 天")
        elif buy_days <= 3:
            score -= 10
            signals.append(f"外資持續賣出（近10日買超僅 {buy_days} 天）")

    if trust:
        recent = trust[-10:]
        buy_days = sum(1 for r in recent if r.get("buy_sell", 0) > 0)
        if buy_days >= 7:
            score += 10
            signals.append(f"投信近10日持續買超 {buy_days} 天")
        elif buy_days <= 2:
            score -= 5
            signals.append("投信持續出清")

    if margin and len(margin) >= 5 and prices and len(prices) >= 5:
        m_change = (
            margin[-1].get("MarginPurchaseTodayBalance", 0)
            - margin[-5].get("MarginPurchaseTodayBalance", 0)
        )
        p_change = prices[-1].get("close", 0) - prices[-5].get("close", 0)

        if m_change < 0 and p_change > 0:
            score += 12
            signals.append("融資減少但股價上漲（法人主導、健康上漲）")
        elif m_change > 0 and p_change > 0:
            score -= 5
            signals.append("融資增加且股價漲（散戶追高，注意追高風險）")
        elif m_change > 0 and p_change < 0:
            score -= 18
            signals.append("融資增加但股價跌（散戶套牢、籌碼凌亂）")
        elif m_change < 0 and p_change < 0:
            score -= 5
            signals.append("融資減少且股價跌（止損賣壓）")

    score = max(0, min(100, score))
    label = (
        "強勢" if score >= 70 else
        "偏多" if score >= 55 else
        "中性" if score >= 45 else
        "偏空" if score >= 30 else
        "弱勢"
    )
    return {"score": score, "label": label, "signals": signals}


def realtime_signal(
    prices: list,
    institutional: list,
    margin: list,
    ma5: list,
    ma20: list,
    ma60: list,
    rsi_vals: list,
) -> dict:
    if not prices:
        return {"big_player": "無資料", "direction": "無資料",
                "player_signals": [], "direction_signals": []}

    latest = prices[-1]
    prev = prices[-2] if len(prices) >= 2 else latest
    latest_price = latest["close"]
    price_change = latest_price - prev["close"]

    # --- 量能分析 ---
    vols = [r.get("Trading_Volume", 0) for r in prices]
    avg_vol = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else (sum(vols[:-1]) / max(len(vols) - 1, 1))
    vol_ratio = vols[-1] / avg_vol if avg_vol > 0 else 1.0

    # --- 最新均線 / RSI ---
    def last_val(lst):
        return next((v for v in reversed(lst) if v is not None), None)

    lma5 = last_val(ma5)
    lma20 = last_val(ma20)
    lma60 = last_val(ma60)
    lrsi = last_val(rsi_vals)

    # --- 法人最新一日 ---
    by_date: dict[str, dict] = {}
    for r in institutional:
        d = r["date"]
        if d not in by_date:
            by_date[d] = {}
        by_date[d][r["name"]] = r.get("buy_sell", 0)
    last_inst_date = max(by_date.keys()) if by_date else None
    inst_today = by_date.get(last_inst_date, {}) if last_inst_date else {}
    foreign_net_sh = inst_today.get("Foreign_Investor", 0)   # 股（shares）
    trust_net_sh = inst_today.get("Investment_Trust", 0)     # 股（shares）
    dealer_net = inst_today.get("Dealer_self", 0) + inst_today.get("Dealer_Hedging", 0)
    # 股 ÷ 1000 = 張（FinMind buy/sell 欄位單位為股）
    foreign_net = int(foreign_net_sh / 1000)
    trust_net   = int(trust_net_sh   / 1000)

    # --- 融資變化 ---
    margin_chg = 0
    if len(margin) >= 2:
        margin_chg = (
            margin[-1].get("MarginPurchaseTodayBalance", 0)
            - margin[-2].get("MarginPurchaseTodayBalance", 0)
        )

    # ==================== 大戶 / 散戶 判斷 ====================
    big = 0
    retail = 0
    p_sigs = []

    if foreign_net > 0:
        big += 3
        p_sigs.append(f"外資買超 {foreign_net:,} 張")
    elif foreign_net < 0:
        retail += 1
        p_sigs.append(f"外資賣超 {abs(foreign_net):,} 張")

    if trust_net > 0:
        big += 2
        p_sigs.append(f"投信買超 {trust_net:,} 張")
    elif trust_net < 0:
        retail += 1
        p_sigs.append(f"投信賣超 {abs(trust_net):,} 張")

    if margin_chg < 0:
        big += 1
        p_sigs.append(f"融資減少 {abs(margin_chg):,} 張（散戶退場）")
    elif margin_chg > 0:
        retail += 2
        p_sigs.append(f"融資增加 {margin_chg:,} 張（散戶追高）")

    if vol_ratio >= 1.5:
        if price_change > 0:
            big += 2
            p_sigs.append(f"放量上漲（量比 {vol_ratio:.1f}x）主力推動")
        else:
            p_sigs.append(f"放量下跌（量比 {vol_ratio:.1f}x）注意出貨")
    elif vol_ratio < 0.7:
        p_sigs.append(f"縮量（量比 {vol_ratio:.1f}x）市場觀望")

    if big >= 5:
        big_player = "大戶主導買進"
        bp_color = "red"
    elif big >= 3:
        big_player = "偏大戶"
        bp_color = "orange"
    elif retail >= 3:
        big_player = "散戶主導追高"
        bp_color = "yellow"
    elif retail >= 2:
        big_player = "偏散戶"
        bp_color = "yellow"
    else:
        big_player = "法人觀望"
        bp_color = "gray"

    # ==================== 偏多 / 偏空 ====================
    bull = 0
    d_sigs = []

    if lma5 and latest_price > lma5:
        bull += 1
        d_sigs.append("站上 MA5")
    elif lma5:
        bull -= 1
        d_sigs.append("跌破 MA5")

    if lma20:
        if latest_price > lma20:
            bull += 2
            d_sigs.append("站上 MA20")
        else:
            bull -= 2
            d_sigs.append("跌破 MA20 ⚠")

    if lma60:
        if latest_price > lma60:
            bull += 2
            d_sigs.append("站上 MA60（長多格局）")
        else:
            bull -= 2
            d_sigs.append("跌破 MA60（長空格局）")

    if lma5 and lma20:
        if lma5 > lma20:
            bull += 1
            d_sigs.append("均線多頭排列")
        else:
            bull -= 1
            d_sigs.append("均線空頭排列")

    if lrsi:
        if lrsi >= 70:
            bull += 1
            d_sigs.append(f"RSI {lrsi:.0f}（超買區，注意回調）")
        elif lrsi >= 50:
            bull += 2
            d_sigs.append(f"RSI {lrsi:.0f}（強勢）")
        elif lrsi < 30:
            bull -= 2
            d_sigs.append(f"RSI {lrsi:.0f}（超賣，可能反彈）")
        else:
            bull -= 1
            d_sigs.append(f"RSI {lrsi:.0f}（弱勢）")

    if foreign_net > 0 and trust_net > 0:
        bull += 3
        d_sigs.append("外資 + 投信同步買超")
    elif foreign_net > 0:
        bull += 2
    elif foreign_net < 0 and trust_net < 0:
        bull -= 3
        d_sigs.append("外資 + 投信同步賣超 ⚠")

    if vol_ratio >= 1.5 and price_change > 0:
        bull += 2
        d_sigs.append(f"放量上漲（健康走勢）")
    elif vol_ratio >= 1.5 and price_change < 0:
        bull -= 1

    if bull >= 9:
        direction, dir_color = "強烈偏多", "red"
    elif bull >= 6:
        direction, dir_color = "偏多", "orange"
    elif bull >= 2:
        direction, dir_color = "中性偏多", "yellow"
    elif bull >= -1:
        direction, dir_color = "中性", "gray"
    elif bull >= -5:
        direction, dir_color = "偏空", "cyan"
    else:
        direction, dir_color = "強烈偏空", "blue"

    return {
        "big_player": big_player,
        "bp_color": bp_color,
        "big_score": big,
        "retail_score": retail,
        "direction": direction,
        "dir_color": dir_color,
        "bull_score": bull,
        "vol_ratio": round(vol_ratio, 2),
        "foreign_net": int(foreign_net),
        "trust_net": int(trust_net),
        "margin_change": int(margin_chg),
        "player_signals": p_sigs,
        "direction_signals": d_sigs,
    }


def is_market_open() -> bool:
    TST = timezone(timedelta(hours=8))
    now = datetime.now(TST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return time_type(9, 0) <= t <= time_type(13, 30)


def _parse_twse_levels(raw: str) -> list[float]:
    return [float(x) for x in raw.split("_") if x.strip() and x.strip() != "-"]


def analyze_live(quote: dict, yf_data: dict, yf_quote: dict = {}, fugle_quote: dict = {}) -> dict:
    """即時大戶/散戶 + 偏多/偏空，結合 TWSE 掛單與 Yahoo 分鐘K"""
    result: dict = {
        "market_open": is_market_open(),
        "time": "",
        "current": None,
        "yesterday": None,
        "open": None,
        "high": None,
        "low": None,
        "volume": None,
        "bid_prices": [],
        "bid_vols": [],
        "ask_prices": [],
        "ask_vols": [],
        "bid_ask_ratio": None,
        "vwap": None,
        "above_vwap": None,
        "avg_min_vol": None,
        "max_min_vol": None,
        "recent_vol_trend": None,
        "big_player": "—",
        "bp_color": "gray",
        "direction": "—",
        "dir_color": "gray",
        "signals": [],
    }

    # ── TWSE 掛單解析 ──
    if quote:
        def safe_float(v):
            try: return float(v) if v and v != "-" else None
            except: return None

        result["time"] = quote.get("t", "")
        result["current"] = safe_float(quote.get("z"))
        result["yesterday"] = safe_float(quote.get("y"))
        result["open"] = safe_float(quote.get("o"))
        result["high"] = safe_float(quote.get("h"))
        result["low"] = safe_float(quote.get("l"))
        vol = safe_float(quote.get("v"))
        result["volume"] = int(vol) if vol else None

        bp = _parse_twse_levels(quote.get("b", ""))
        bv = _parse_twse_levels(quote.get("g", ""))
        ap = _parse_twse_levels(quote.get("a", ""))
        av = _parse_twse_levels(quote.get("f", ""))
        result["bid_prices"] = bp
        result["bid_vols"] = bv
        result["ask_prices"] = ap
        result["ask_vols"] = av

        total_bid = sum(bv)
        total_ask = sum(av)
        ratio = round(total_bid / total_ask, 2) if total_ask > 0 else 1.0
        result["bid_ask_ratio"] = ratio

    # ── Yahoo Finance 分鐘K 解析 ──
    bars = []
    try:
        chart = yf_data.get("chart", {}).get("result", [{}])[0]
        q = chart.get("indicators", {}).get("quote", [{}])[0]
        opens = q.get("open", [])
        closes = q.get("close", [])
        volumes = q.get("volume", [])
        highs = q.get("high", [])
        lows = q.get("low", [])
        bars = [
            {"o": o, "c": c, "v": v // 1000, "h": h, "l": l}
            for o, c, v, h, l in zip(opens, closes, volumes, highs, lows)
            if c is not None and v is not None
        ]
    except Exception:
        pass

    if bars:
        # VWAP
        tp_sum = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in bars)
        vol_sum = sum(b["v"] for b in bars)
        vwap = round(tp_sum / vol_sum, 2) if vol_sum > 0 else None
        result["vwap"] = vwap

        # TWSE 被擋 → 用 Yahoo v8 chart meta 補全（v7 quote API 已被封鎖）
        if result["current"] is None:
            try:
                meta = yf_data.get("chart", {}).get("result", [{}])[0].get("meta", {})
                result["current"]   = meta.get("regularMarketPrice")
                result["yesterday"] = meta.get("previousClose") or meta.get("chartPreviousClose")
                result["open"]      = meta.get("regularMarketOpen") or (bars[0]["o"] if bars else None)
                result["high"]      = meta.get("regularMarketDayHigh") or (
                    round(max(b["h"] for b in bars if b.get("h")), 2) if bars else None)
                result["low"]       = meta.get("regularMarketDayLow") or (
                    round(min(b["l"] for b in bars if b.get("l")), 2) if bars else None)
                raw_vol = meta.get("regularMarketVolume", 0)
                result["volume"]    = int(raw_vol // 1000) if raw_vol else (sum(b["v"] for b in bars) if bars else None)
            except Exception:
                pass

        # Fugle 行情補全（Yahoo v8 也無資料時的最後保障）
        if result["current"] is None and fugle_quote:
            try:
                result["current"]   = fugle_quote.get("current")
                result["yesterday"] = fugle_quote.get("yesterday")
                result["open"]      = fugle_quote.get("open")
                result["high"]      = fugle_quote.get("high")
                result["low"]       = fugle_quote.get("low")
                result["volume"]    = fugle_quote.get("volume")
            except Exception:
                pass

        # Fugle 委買委賣5檔（TWSE 被擋時的替代，5檔完整資料）
        if not result["bid_prices"] and fugle_quote:
            try:
                bp = fugle_quote.get("bid_prices", [])
                bv = fugle_quote.get("bid_vols", [])
                ap = fugle_quote.get("ask_prices", [])
                av = fugle_quote.get("ask_vols", [])
                if bp and ap:
                    result["bid_prices"] = bp
                    result["bid_vols"]   = bv
                    result["ask_prices"] = ap
                    result["ask_vols"]   = av
                    total_bid = sum(bv)
                    total_ask = sum(av)
                    result["bid_ask_ratio"] = round(total_bid / total_ask, 2) if total_ask > 0 else 1.0
            except Exception:
                pass

        cur = result["current"]
        if vwap and cur:
            if cur > vwap:
                result["above_vwap"] = True
            elif cur < vwap:
                result["above_vwap"] = False
            # cur == vwap（如漲停開盤鎖板）→ 不判斷，避免誤觸弱勢信號

        # 每分鐘均量（排除最後一筆，因為可能尚未結束）
        vols = [b["v"] for b in bars]
        avg = sum(vols[:-1]) / max(len(vols) - 1, 1)
        result["avg_min_vol"] = round(avg)
        result["max_min_vol"] = max(vols)
        result["latest_min_vol"] = vols[-1]

        # 最後一根K棒方向（收盤 > 開盤 = 紅K = 買方主導）
        last = bars[-1]
        if last.get("o") is not None:
            result["last_bar_up"] = last["c"] > last["o"]

        # 近5分鐘 vs 前5分鐘 量能趨勢
        if len(vols) >= 10:
            r5 = sum(vols[-5:]) / 5
            p5 = sum(vols[-10:-5]) / 5
            result["recent_vol_trend"] = round(r5 / p5, 2) if p5 > 0 else 1.0
        else:
            result["recent_vol_trend"] = 1.0

    # ── 大戶/散戶 + 偏多/偏空 綜合判斷 ──
    big = 0
    retail = 0
    sigs = []

    # 漲停/跌停 優先偵測（台股一般 ±10%，先判斷以免被其他弱信號蓋掉）
    _cur_pre = result.get("current")
    _yest_pre = result.get("yesterday")
    _limit_flag = None
    if _cur_pre and _yest_pre and _yest_pre > 0:
        _chg_pct = (_cur_pre - _yest_pre) / _yest_pre * 100
        if _chg_pct >= 9.5:
            _limit_flag = "up"
            big += 6
            sigs.append(f"漲停板（+{_chg_pct:.1f}%），強力鎖板買盤")
        elif _chg_pct <= -9.5:
            _limit_flag = "down"
            retail += 6
            sigs.append(f"跌停板（{_chg_pct:.1f}%），強力鎖板賣壓")

    ratio = result.get("bid_ask_ratio")
    if ratio:
        if ratio >= 2.0:
            big += 4
            sigs.append(f"委買/委賣 = {ratio:.2f}（大量買盤掛單）")
        elif ratio >= 1.4:
            big += 2
            sigs.append(f"委買/委賣 = {ratio:.2f}（買盤積極）")
        elif ratio <= 0.5:
            retail += 3
            sigs.append(f"委買/委賣 = {ratio:.2f}（大量賣盤掛單）")
        elif ratio <= 0.7:
            retail += 1
            sigs.append(f"委買/委賣 = {ratio:.2f}（賣盤積極）")
        else:
            sigs.append(f"委買/委賣 = {ratio:.2f}（買賣平衡）")

    if result.get("above_vwap") is True:
        big += 2
        sigs.append(f"現價 {result['current']} > VWAP {result['vwap']}（強勢站上均價）")
    elif result.get("above_vwap") is False:
        retail += 1
        sigs.append(f"現價 {result['current']} < VWAP {result['vwap']}（低於均價弱勢）")

    avg_v = result.get("avg_min_vol", 0)
    max_v = result.get("max_min_vol", 0)
    trend = result.get("recent_vol_trend", 1.0)
    cur = result.get("current")
    yest = result.get("yesterday")
    price_up = cur and yest and cur > yest

    if max_v and avg_v and max_v >= avg_v * 5:
        sigs.append(f"出現大單（最大量 {max_v} 張/分，均量 {avg_v} 張/分）")
        if price_up:
            big += 2
        else:
            sigs[-1] += "，注意出貨"

    if trend and trend >= 1.5:
        big += 1
        sigs.append(f"近5分鐘量能加速（{trend:.1f}x）")
    elif trend and trend <= 0.6:
        sigs.append(f"近5分鐘量能萎縮（{trend:.1f}x）")

    # 綜合判斷
    if big >= 6:
        bp, bp_c = "大戶積極買進", "red"
    elif big >= 4:
        bp, bp_c = "偏大戶買進", "orange"
    elif big >= 2:
        bp, bp_c = "小幅偏大戶", "yellow"
    elif retail >= 4:
        bp, bp_c = "散戶主導追高", "cyan"
    elif retail >= 2:
        bp, bp_c = "偏散戶", "cyan"
    else:
        bp, bp_c = "觀望盤整", "gray"

    if big >= 4 and price_up:
        dr, dr_c = "偏多做多", "red"
    elif big >= 2 and price_up:
        dr, dr_c = "偏多", "orange"
    elif retail >= 3 and not price_up:
        dr, dr_c = "偏空", "cyan"
    elif price_up:
        dr, dr_c = "中性偏多", "yellow"
    elif price_up is False:
        dr, dr_c = "中性偏空", "cyan"
    else:
        dr, dr_c = "中性", "gray"

    result["big_player"] = bp
    result["bp_color"] = bp_c
    result["direction"] = dr
    result["dir_color"] = dr_c
    result["signals"] = sigs
    return result
