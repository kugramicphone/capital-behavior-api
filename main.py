"""
資金行為學 — FastAPI 後端 v1.1
修正 TWSE API 路徑，使用最新可用端點
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
from datetime import datetime, timedelta
from statistics import mean

app = FastAPI(title="資金行為學 API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

TWSE_STOCK_DAY = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
TWSE_INST      = "https://www.twse.com.tw/rwd/zh/fund/T86"
TWSE_INDEX_DAY = "https://www.twse.com.tw/rwd/zh/indices/MI_5MINS_HIST"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.twse.com.tw/",
    "Accept": "application/json, text/plain, */*",
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
            d = d.replace(year=d.year-1, month=12)
        else:
            d = d.replace(month=d.month-1)
    return result

async def twse_get(client, url: str, params: dict) -> dict:
    for attempt in range(3):
        try:
            await asyncio.sleep(0.5 * attempt)
            r = await client.get(url, params=params, headers=HEADERS, timeout=20)
            r.raise_for_status()
            data = r.json()
            if data.get("stat") in ("OK", "ok") or data.get("data"):
                return data
        except Exception:
            pass
    return {}


@app.get("/stock/{stock_id}/price")
async def get_price(stock_id: str):
    key = cache_key("price", sid=stock_id)
    if key in _cache:
        return _cache[key]

    months = recent_months(3)
    rows = []

    async with httpx.AsyncClient() as client:
        for ym in months:
            data = await twse_get(client, TWSE_STOCK_DAY, {
                "stockNo": stock_id,
                "date": ym + "01",
                "response": "json",
            })
            for row in (data.get("data") or []):
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
    latest  = rows[-1]
    prev    = rows[-2] if len(rows) >= 2 else rows[-1]

    chg   = round((latest["close"] - prev["close"]) / prev["close"] * 100, 2)
    ma20  = round(mean(closes[-20:]),  2) if len(closes)  >= 20 else None
    ma60  = round(mean(closes[-60:]),  2) if len(closes)  >= 60 else None
    vol5  = round(mean(volumes[-5:]),  0) if len(volumes) >= 5  else None
    vol20 = round(mean(volumes[-20:]), 0) if len(volumes) >= 20 else None

    result = {
        "stock_id":      stock_id,
        "price":         latest["close"],
        "change_pct":    chg,
        "above_20ma":    (latest["close"] > ma20) if ma20 else None,
        "ma20":          ma20,
        "ma60":          ma60,
        "vol5avg":       int(vol5)  if vol5  else None,
        "vol20avg":      int(vol20) if vol20 else None,
        "data_date":     latest["date"],
        "close_history": closes[-60:],
    }
    _cache[key] = result
    return result


@app.get("/stock/{stock_id}/institutional")
async def get_institutional(stock_id: str):
    key = cache_key("inst", sid=stock_id)
    if key in _cache:
        return _cache[key]

    months = recent_months(2)
    foreign_nets = []
    sit_nets     = []

    async with httpx.AsyncClient() as client:
        for ym in months:
            data = await twse_get(client, TWSE_INST, {
                "date": ym + "01",
                "selectType": "ALLBUT0999",
                "response": "json",
            })
            for row in (data.get("data") or []):
                if len(row) < 13:
                    continue
                if str(row[0]).strip() != stock_id:
                    continue
                try:
                    fn = int(str(row[6]).replace(",","").replace("−","-").replace("–","-"))
                    sn = int(str(row[12]).replace(",","").replace("−","-").replace("–","-"))
                    foreign_nets.append(fn)
                    sit_nets.append(sn)
                except (ValueError, IndexError):
                    continue

    def consec(lst):
        count = 0
        for v in reversed(lst):
            if v > 0: count += 1
            else: break
        return count

    result = {
        "stock_id":        stock_id,
        "sit_days":        consec(sit_nets),
        "foreign_days":    consec(foreign_nets),
        "sit_buy":         (sit_nets[-1] > 0)     if sit_nets     else False,
        "foreign_buy":     (foreign_nets[-1] > 0) if foreign_nets else False,
        "recent5_sit":     sit_nets[-5:]     if len(sit_nets)     >= 5 else sit_nets,
        "recent5_foreign": foreign_nets[-5:] if len(foreign_nets) >= 5 else foreign_nets,
    }
    _cache[key] = result
    return result


@app.get("/market/regime")
async def get_market_regime():
    key = cache_key("regime")
    if key in _cache:
        return _cache[key]

    months = recent_months(4)
    rows = []

    async with httpx.AsyncClient() as client:
        for ym in months:
            data = await twse_get(client, TWSE_INDEX_DAY, {
                "date": ym + "01",
                "response": "json",
            })
            for row in (data.get("data") or []):
                try:
                    close = float(str(row[4]).replace(",", ""))
                    rows.append({"date": row[0], "close": close})
                except (ValueError, IndexError):
                    continue

    if len(rows) < 5:
        raise HTTPException(status_code=503, detail="無法取得大盤資料")

    rows   = sorted(rows, key=lambda x: x["date"])
    closes = [r["close"] for r in rows]
    latest = rows[-1]
    prev   = rows[-2]

    chg  = round((latest["close"] - prev["close"]) / prev["close"] * 100, 2)
    ma20 = round(mean(closes[-20:]), 2) if len(closes) >= 20 else None
    ma60 = round(mean(closes[-60:]), 2) if len(closes) >= 60 else None

    if ma20 and ma60 and latest["close"] > ma20 > ma60:
        regime, weight = "溫床市場", 1.0
    elif ma20 and latest["close"] < ma20 and (not ma60 or ma20 < ma60):
        regime, weight = "墳場市場", 0.0
    else:
        regime, weight = "混沌輪動盤", 0.5

    result = {
        "index":      latest["close"],
        "change_pct": chg,
        "ma20":       ma20,
        "ma60":       ma60,
        "above_20ma": (latest["close"] > ma20) if ma20 else None,
        "regime":     regime,
        "weight":     weight,
        "data_date":  latest["date"],
    }
    _cache[key] = result
    return result


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
    return {"status": "ok", "version": "1.1.0"}

@app.get("/health")
def health():
    return {"status": "healthy", "time": datetime.now().isoformat()}
