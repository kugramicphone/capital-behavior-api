"""
資金行為學 — FastAPI 後端 v1.0
串接 TWSE 官方免費 API，提供：
  - 個股日K + 均線（20MA / 60MA）
  - 三大法人買賣超（投信、外資連買天數）
  - 大盤指數狀態
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
from datetime import datetime, timedelta
from statistics import mean
from typing import Optional
import re

app = FastAPI(title="資金行為學 API", version="1.0.0")

# ── CORS：允許所有來源（前端從 Claude.ai / 任意網域呼叫）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── TWSE Base URL
TWSE = "https://www.twse.com.tw/rwd/zh"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.twse.com.tw/",
}

# ── 簡單的記憶體快取（避免同一天重複打 TWSE）
_cache: dict = {}

def _cache_key(endpoint: str, **params) -> str:
    today = datetime.now().strftime("%Y%m%d")
    return f"{today}:{endpoint}:{params}"


async def twse_get(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    """呼叫 TWSE API，加上重試一次的容錯"""
    for attempt in range(2):
        try:
            r = await client.get(url, params=params, headers=HEADERS, timeout=15)
            r.raise_for_status()
            data = r.json()
            if data.get("stat") == "OK":
                return data
        except Exception:
            if attempt == 0:
                await asyncio.sleep(1)
    return {}


def _last_n_trading_dates(n: int = 2) -> list[str]:
    """往回找最近 n 個交易日的 YYYYMM 月份清單（去重）"""
    months = set()
    d = datetime.now()
    count = 0
    while count < n * 25:          # 最多回溯 25 * n 天
        if d.weekday() < 5:        # 排除週六日（粗估）
            months.add(d.strftime("%Y%m"))
            count += 1
            if len(months) >= n:
                break
        d -= timedelta(days=1)
    return sorted(months, reverse=True)[:n]


# ════════════════════════════════════════════════
#  端點 1：個股日K + 均線
# ════════════════════════════════════════════════
@app.get("/stock/{stock_id}/price")
async def get_price(stock_id: str):
    """
    回傳：最新收盤價、漲跌幅、20MA、60MA、近5日均量、近20日均量
    資料來源：TWSE /exchangeReport/STOCK_DAY
    """
    key = _cache_key("price", stock_id=stock_id)
    if key in _cache:
        return _cache[key]

    # 抓最近 3 個月資料以計算 60MA
    months = _last_n_trading_dates(3)
    rows: list[dict] = []

    async with httpx.AsyncClient() as client:
        for ym in months:
            data = await twse_get(
                client,
                f"{TWSE}/exchangeReport/STOCK_DAY",
                {"stockNo": stock_id, "date": ym + "01", "response": "json"},
            )
            if data.get("data"):
                for row in data["data"]:
                    # row: [日期, 成交股數, 成交金額, 開盤, 最高, 最低, 收盤, 漲跌, 成交筆數]
                    try:
                        close = float(row[6].replace(",", ""))
                        volume = int(row[1].replace(",", "")) // 1000  # 換算成張
                        rows.append({"close": close, "volume": volume, "date": row[0]})
                    except (ValueError, IndexError):
                        continue

    if len(rows) < 5:
        raise HTTPException(status_code=404, detail=f"找不到 {stock_id} 的價格資料")

    # 依日期排序（由舊到新）
    rows = sorted(rows, key=lambda x: x["date"])
    closes = [r["close"] for r in rows]
    volumes = [r["volume"] for r in rows]

    latest = rows[-1]
    prev   = rows[-2] if len(rows) >= 2 else rows[-1]
    change_pct = round((latest["close"] - prev["close"]) / prev["close"] * 100, 2)

    ma20 = round(mean(closes[-20:]), 2) if len(closes) >= 20 else None
    ma60 = round(mean(closes[-60:]), 2) if len(closes) >= 60 else None
    vol5  = round(mean(volumes[-5:]),  0) if len(volumes) >= 5  else None
    vol20 = round(mean(volumes[-20:]), 0) if len(volumes) >= 20 else None

    result = {
        "stock_id":    stock_id,
        "price":       latest["close"],
        "change_pct":  change_pct,
        "above_20ma":  (latest["close"] > ma20) if ma20 else None,
        "ma20":        ma20,
        "ma60":        ma60,
        "vol5avg":     int(vol5)  if vol5  else None,
        "vol20avg":    int(vol20) if vol20 else None,
        "data_date":   latest["date"],
        "close_history": closes[-60:],   # 前端可自行繪圖
    }
    _cache[key] = result
    return result


# ════════════════════════════════════════════════
#  端點 2：三大法人買賣超
# ════════════════════════════════════════════════
@app.get("/stock/{stock_id}/institutional")
async def get_institutional(stock_id: str):
    """
    回傳：投信連買天數、外資連買天數、近5日法人買賣超明細
    資料來源：TWSE /fund/T86（三大法人每日買賣超）
    """
    key = _cache_key("inst", stock_id=stock_id)
    if key in _cache:
        return _cache[key]

    months = _last_n_trading_dates(2)
    daily: list[dict] = []

    async with httpx.AsyncClient() as client:
        for ym in months:
            # T86：全市場三大法人，需自行過濾股票代號
            data = await twse_get(
                client,
                f"{TWSE}/fund/T86",
                {"date": ym + "01", "selectType": "ALLBUT0999", "response": "json"},
            )
            if not data.get("data"):
                continue
            for row in data["data"]:
                # row[0]=股票代號, row[1]=名稱
                # row[4]=外資買, row[5]=外資賣, row[6]=外資淨
                # row[10]=投信買, row[11]=投信賣, row[12]=投信淨
                if len(row) < 13:
                    continue
                if row[0].strip() != stock_id:
                    continue
                try:
                    foreign_net = int(row[6].replace(",", "").replace("−", "-"))
                    sit_net     = int(row[12].replace(",", "").replace("−", "-"))
                    daily.append({
                        "date":        row[0],   # 實際上 T86 是單日全市場，date 從外層取
                        "foreign_net": foreign_net,
                        "sit_net":     sit_net,
                    })
                except (ValueError, IndexError):
                    continue

    # 連買天數計算（從最新往回數連續 > 0 的天數）
    def consec_buy(records: list[int]) -> int:
        count = 0
        for v in reversed(records):
            if v > 0:
                count += 1
            else:
                break
        return count

    foreign_nets = [d["foreign_net"] for d in daily]
    sit_nets     = [d["sit_net"]     for d in daily]

    result = {
        "stock_id":          stock_id,
        "sit_days":          consec_buy(sit_nets),
        "foreign_days":      consec_buy(foreign_nets),
        "sit_buy":           (sit_nets[-1] > 0)     if sit_nets     else False,
        "foreign_buy":       (foreign_nets[-1] > 0) if foreign_nets else False,
        "recent5_sit":       sit_nets[-5:]     if len(sit_nets)     >= 5 else sit_nets,
        "recent5_foreign":   foreign_nets[-5:] if len(foreign_nets) >= 5 else foreign_nets,
    }
    _cache[key] = result
    return result


# ════════════════════════════════════════════════
#  端點 3：大盤狀態（市場環境引擎輸入）
# ════════════════════════════════════════════════
@app.get("/market/regime")
async def get_market_regime():
    """
    回傳：加權指數、20MA、60MA、漲跌幅 → 前端判斷市場環境
    資料來源：TWSE /indices/MI_5MINS_HIST（大盤歷史）
    """
    key = _cache_key("regime")
    if key in _cache:
        return _cache[key]

    months = _last_n_trading_dates(3)
    rows: list[dict] = []

    async with httpx.AsyncClient() as client:
        for ym in months:
            data = await twse_get(
                client,
                f"{TWSE}/indices/MI_5MINS_HIST",
                {"date": ym + "01", "response": "json"},
            )
            if data.get("data"):
                for row in data["data"]:
                    try:
                        rows.append({
                            "date":  row[0],
                            "close": float(row[4].replace(",", "")),
                        })
                    except (ValueError, IndexError):
                        continue

    if len(rows) < 5:
        raise HTTPException(status_code=503, detail="無法取得大盤資料")

    rows   = sorted(rows, key=lambda x: x["date"])
    closes = [r["close"] for r in rows]
    latest = rows[-1]
    prev   = rows[-2]
    change = round((latest["close"] - prev["close"]) / prev["close"] * 100, 2)
    ma20   = round(mean(closes[-20:]), 2) if len(closes) >= 20 else None
    ma60   = round(mean(closes[-60:]), 2) if len(closes) >= 60 else None

    # 市場環境判斷
    if ma20 and ma60 and latest["close"] > ma20 > ma60:
        regime, weight = "溫床市場", 1.0
    elif ma20 and latest["close"] < ma20 and (not ma60 or ma20 < ma60):
        regime, weight = "墳場市場", 0.0
    else:
        regime, weight = "混沌輪動盤", 0.5

    result = {
        "index":       latest["close"],
        "change_pct":  change,
        "ma20":        ma20,
        "ma60":        ma60,
        "above_20ma":  (latest["close"] > ma20) if ma20 else None,
        "above_60ma":  (latest["close"] > ma60) if ma60 else None,
        "regime":      regime,
        "weight":      weight,
        "data_date":   latest["date"],
    }
    _cache[key] = result
    return result


# ════════════════════════════════════════════════
#  端點 4：批次掃描（一次查多支股票）
# ════════════════════════════════════════════════
@app.post("/scan")
async def scan_stocks(body: dict):
    """
    輸入：{ "stocks": ["2330", "2454", ...] }
    回傳：每支股票的 price + institutional 合併結果
    最多一次 20 支（避免打爆 TWSE）
    """
    stock_ids: list[str] = body.get("stocks", [])
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
            await asyncio.sleep(0.8)   # 每支間隔 0.8 秒，避免 TWSE 擋
        except HTTPException as e:
            results.append({"stock_id": sid, "error": e.detail})
        except Exception as e:
            results.append({"stock_id": sid, "error": str(e)})

    return {"results": results}


# ════════════════════════════════════════════════
#  健康檢查
# ════════════════════════════════════════════════
@app.get("/")
def root():
    return {"status": "ok", "service": "資金行為學 API v1.0"}

@app.get("/health")
def health():
    return {"status": "healthy", "time": datetime.now().isoformat()}
