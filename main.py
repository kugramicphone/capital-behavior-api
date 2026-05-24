"""
資金行為學 — FastAPI 後端 v1.3
加入診斷端點，直接看 TWSE 回傳內容
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
from datetime import datetime
from statistics import mean

app = FastAPI(title="資金行為學 API", version="1.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.twse.com.tw/",
    "Accept": "application/json",
}

_cache: dict = {}

def cache_key(tag: str, **kw) -> str:
    today = datetime.now().strftime("%Y%m%d")
    return f"{today}:{tag}:{kw}"

def recent_months(n: int) -> list:
    result = []
    d = datetime.now()
    for _ in range(n):
        result.append(d.strftime("%Y%m"))
        if d.month == 1:
            d = d.replace(year=d.year - 1, month=12)
        else:
            d = d.replace(month=d.month - 1)
    return result

async def safe_get(client, url: str, params: dict = None):
    for attempt in range(3):
        try:
            await asyncio.sleep(0.5 * attempt)
            r = await client.get(
                url, params=params or {}, headers=HEADERS, timeout=20
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = str(e)
    return {"_error": last_err}


# ════════════════════════════════════════════
# 診斷端點 — 直接看 TWSE 各端點回傳什麼
# ════════════════════════════════════════════
@app.get("/debug/twse")
async def debug_twse():
    """
    測試所有 TWSE 端點，回傳原始結果的前 300 字元
    讓我們知道哪個端點有效、欄位格式是什麼
    """
    now = datetime.now()
    ym  = now.strftime("%Y%m")
    results = {}

    async with httpx.AsyncClient() as client:

        # 測試 1: openapi 今日大盤
        d = await safe_get(client, "https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX")
        results["openapi_MI_INDEX"] = str(d)[:300]

        await asyncio.sleep(1)

        # 測試 2: openapi 每月指數
        d = await safe_get(client, "https://openapi.twse.com.tw/v1/indices/MFI_INDEX")
        results["openapi_MFI_INDEX"] = str(d)[:300]

        await asyncio.sleep(1)

        # 測試 3: rwd 大盤歷史（本月）
        d = await safe_get(client,
            "https://www.twse.com.tw/rwd/zh/indices/MI_5MINS_HIST",
            {"date": ym + "01", "response": "json"}
        )
        results["rwd_MI_5MINS_HIST"] = str(d)[:300]

        await asyncio.sleep(1)

        # 測試 4: 舊版大盤歷史
        d = await safe_get(client,
            "https://www.twse.com.tw/exchangeReport/MI_INDEX",
            {"response": "json", "date": ym + "01", "type": "IND"}
        )
        results["old_MI_INDEX"] = str(d)[:300]

        await asyncio.sleep(1)

        # 測試 5: 個股 0001（加權指數）當月日K
        d = await safe_get(client,
            "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY",
            {"stockNo": "0001", "date": ym + "01", "response": "json"}
        )
        results["stock_0001"] = str(d)[:300]

        await asyncio.sleep(1)

        # 測試 6: 個股 2330 當月日K（驗證個股端點是否正常）
        d = await safe_get(client,
            "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY",
            {"stockNo": "2330", "date": ym + "01", "response": "json"}
        )
        results["stock_2330"] = str(d)[:300]

    return {"time": now.isoformat(), "ym": ym, "results": results}


# ════════════════════════════════════════════
# 個股日K + 均線
# ════════════════════════════════════════════
@app.get("/stock/{stock_id}/price")
async def get_price(stock_id: str):
    key = cache_key("price", sid=stock_id)
    if key in _cache:
        return _cache[key]

    months = recent_months(3)
    rows = []

    async with httpx.AsyncClient() as client:
        for ym in months:
            data = await safe_get(client,
                "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY",
                {"stockNo": stock_id, "date": ym + "01", "response": "json"},
            )
            for row in (data.get("data") if isinstance(data, dict) else []) or []:
                try:
                    close  = float(row[6].replace(",", ""))
                    volume = int(row[1].replace(",", "")) // 1000
                    rows.append({"close": close, "volume": volume, "date": row[0]})
                except (ValueError, IndexError):
                    continue

    if len(rows) < 5:
        raise HTTPException(status_code=404, detail=f"找不到 {stock_id} 的價格資料")

    rows    = sorted(rows, key=lambda x: x["date"])
    closes  = [r["close"]  for r in rows]
    volumes = [r["volume"] for r in rows]
    latest, prev = rows[-1], rows[-2] if len(rows) >= 2 else rows[-1]

    chg   = round((latest["close"] - prev["close"]) / prev["close"] * 100, 2)
    ma20  = round(mean(closes[-20:]),  2) if len(closes)  >= 20 else None
    ma60  = round(mean(closes[-60:]),  2) if len(closes)  >= 60 else None
    vol5  = round(mean(volumes[-5:]),  0) if len(volumes) >= 5  else None
    vol20 = round(mean(volumes[-20:]), 0) if len(volumes) >= 20 else None

    result = {
        "stock_id": stock_id, "price": latest["close"],
        "change_pct": chg,
        "above_20ma": (latest["close"] > ma20) if ma20 else None,
        "ma20": ma20, "ma60": ma60,
        "vol5avg":  int(vol5)  if vol5  else None,
        "vol20avg": int(vol20) if vol20 else None,
        "data_date": latest["date"],
        "close_history": closes[-60:],
    }
    _cache[key] = result
    return result


# ════════════════════════════════════════════
# 三大法人
# ════════════════════════════════════════════
@app.get("/stock/{stock_id}/institutional")
async def get_institutional(stock_id: str):
    key = cache_key("inst", sid=stock_id)
    if key in _cache:
        return _cache[key]

    months = recent_months(2)
    foreign_nets, sit_nets = [], []

    async with httpx.AsyncClient() as client:
        for ym in months:
            data = await safe_get(client,
                "https://www.twse.com.tw/rwd/zh/fund/T86",
                {"date": ym + "01", "selectType": "ALLBUT0999", "response": "json"},
            )
            for row in (data.get("data") if isinstance(data, dict) else []) or []:
                if len(row) < 13 or str(row[0]).strip() != stock_id:
                    continue
                try:
                    fn = int(str(row[6]).replace(",","").replace("−","-").replace("–","-"))
                    sn = int(str(row[12]).replace(",","").replace("−","-").replace("–","-"))
                    foreign_nets.append(fn)
                    sit_nets.append(sn)
                except (ValueError, IndexError):
                    continue

    def consec(lst):
        c = 0
        for v in reversed(lst):
            if v > 0: c += 1
            else: break
        return c

    result = {
        "stock_id": stock_id,
        "sit_days": consec(sit_nets), "foreign_days": consec(foreign_nets),
        "sit_buy":     (sit_nets[-1] > 0)     if sit_nets     else False,
        "foreign_buy": (foreign_nets[-1] > 0) if foreign_nets else False,
        "recent5_sit":     sit_nets[-5:]     if len(sit_nets)     >= 5 else sit_nets,
        "recent5_foreign": foreign_nets[-5:] if len(foreign_nets) >= 5 else foreign_nets,
    }
    _cache[key] = result
    return result


# ════════════════════════════════════════════
# 大盤環境（暫時用備援邏輯，等診斷後修正）
# ════════════════════════════════════════════
@app.get("/market/regime")
async def get_market_regime():
    key = cache_key("regime")
    if key in _cache:
        return _cache[key]

    months = recent_months(4)
    closes = []

    async with httpx.AsyncClient() as client:
        # 嘗試所有已知端點
        for ym in months:
            for url, params in [
                (
                    "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY",
                    {"stockNo": "0001", "date": ym + "01", "response": "json"},
                ),
                (
                    "https://www.twse.com.tw/exchangeReport/MI_INDEX",
                    {"response": "json", "date": ym + "01", "type": "IND"},
                ),
            ]:
                data = await safe_get(client, url, params)
                raw  = None
                if isinstance(data, dict):
                    raw = data.get("data") or data.get("data1") or data.get("data2")
                if not raw:
                    continue
                for row in raw:
                    try:
                        # 嘗試第 6 欄（STOCK_DAY格式）
                        val = str(row[6] if len(row) > 6 else row[1]).replace(",","")
                        c   = float(val)
                        if 1000 < c < 100000:   # 合理的大盤點位範圍
                            closes.append({"date": str(row[0]), "close": c})
                    except (ValueError, IndexError):
                        continue
                if closes:
                    break
            await asyncio.sleep(0.5)
            if len(closes) >= 20:
                break

        # 備援：openapi 今日
        if len(closes) < 5:
            d = await safe_get(client, "https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX")
            if isinstance(d, list):
                for item in d:
                    if "發行量加權" in str(item.get("指數","")):
                        try:
                            c = float(str(item["收盤指數"]).replace(",",""))
                            closes.append({"date": datetime.now().strftime("%Y/%m/%d"), "close": c})
                        except Exception:
                            pass

    if not closes:
        raise HTTPException(status_code=503, detail="無法取得大盤資料")

    closes  = sorted(closes, key=lambda x: x["date"])
    vals    = [r["close"] for r in closes]
    latest  = vals[-1]
    prev    = vals[-2] if len(vals) >= 2 else vals[-1]
    chg     = round((latest - prev) / prev * 100, 2)
    ma20    = round(mean(vals[-20:]), 2) if len(vals) >= 20 else None
    ma60    = round(mean(vals[-60:]), 2) if len(vals) >= 60 else None

    if ma20 and ma60 and latest > ma20 > ma60:
        regime, weight = "溫床市場", 1.0
    elif ma20 and latest < ma20 and (not ma60 or ma20 < ma60):
        regime, weight = "墳場市場", 0.0
    else:
        regime, weight = "混沌輪動盤", 0.5

    result = {
        "index": latest, "change_pct": chg,
        "ma20": ma20, "ma60": ma60,
        "above_20ma": (latest > ma20) if ma20 else None,
        "regime": regime, "weight": weight,
        "data_date": closes[-1]["date"],
    }
    _cache[key] = result
    return result


# ════════════════════════════════════════════
# 批次掃描
# ════════════════════════════════════════════
@app.post("/scan")
async def scan_stocks(body: dict):
    stock_ids = body.get("stocks", [])
    if not stock_ids:
        raise HTTPException(status_code=400, detail="請提供股票代號清單")
    if len(stock_ids) > 20:
        raise HTTPException(status_code=400, detail="單次最多 20 支")

    results = []
    for sid in stock_ids:
        try:
            price = await get_price(sid)
            inst  = await get_institutional(sid)
            results.append({**price, **inst, "stock_id": sid})
            await asyncio.sleep(1.0)
        except HTTPException as e:
            results.append({"stock_id": sid, "error": e.detail})
        except Exception as e:
            results.append({"stock_id": sid, "error": str(e)})

    return {"results": results}


@app.get("/")
def root():
    return {"status": "ok", "version": "1.3.0"}

@app.get("/health")
def health():
    return {"status": "healthy", "time": datetime.now().isoformat()}
