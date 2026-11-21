import json
import os
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
import requests

TZ_TH = ZoneInfo("Asia/Bangkok")
TZ_CME = ZoneInfo("America/Chicago")

TV_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
    "Content-Type": "application/json",
}


def get_realtime_utc() -> datetime:
    """เช็กเวลาเรียลไทม์จาก HTTP Server Header เพื่อป้องกันเวลาเครื่องเพี้ยน"""
    try:
        resp = requests.head("https://www.google.com", timeout=5)
        if "Date" in resp.headers:
            return parsedate_to_datetime(resp.headers["Date"]).astimezone(timezone.utc)
    except Exception:
        pass
    return datetime.now(timezone.utc)


def evaluate_cme_session(now_utc: datetime) -> dict:
    """
    ตรวจสอบสถานะตลาด CME Globex (Gold GC/OG) และคำนวณ 'วันเทรดจริงของ CME'
    - ตลาดเปิด: อาทิตย์ 17:00 CT ถึง ศุกร์ 16:00 CT
    - พักตลาดรายวัน (Daily Break): 16:00 - 17:00 CT
    - เปลี่ยนวันเทรด (Rollover): ตั้งแต่ 17:00 CT ถือเป็นวันเทรดของวันถัดไป
    """
    now_th = now_utc.astimezone(TZ_TH)
    now_cme = now_utc.astimezone(TZ_CME)
    wd = now_cme.weekday()  # 0=Mon ... 4=Fri, 5=Sat, 6=Sun
    hr = now_cme.hour

    # 1. เช็กตลาดปิดเสาร์-อาทิตย์
    if (wd == 4 and hr >= 16) or wd == 5 or (wd == 6 and hr < 17):
        status = "WEEKEND_CLOSED"
        is_open = False
    # 2. เช็กช่วงพักตลาดรายวัน 16:00 - 17:00 CT
    elif hr == 16:
        status = "DAILY_BREAK"
        is_open = False
    else:
        status = "OPEN"
        is_open = True

    # 3. คำนวณวันเทรดจริงของ CME (Trade Date)
    trade_date = now_cme.date()
    if hr >= 17:
        trade_date = trade_date + timedelta(days=1)
    # ถ้าตกเสาร์-อาทิตย์ ให้เลื่อนไปวันจันทร์ถัดไป
    while trade_date.weekday() >= 5:
        trade_date = trade_date + timedelta(days=1)

    return {
        "is_open": is_open,
        "market_status": status,
        "realtime_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "realtime_th": now_th.strftime("%Y-%m-%d %H:%M:%S ICT"),
        "realtime_cme": now_cme.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "cme_trade_date": trade_date.strftime("%Y-%m-%d"),
        "cme_trade_date_int": int(trade_date.strftime("%Y%m%d")),
    }


def fetch_underlying_gc() -> dict:
    """ดึงราคา Gold Futures ล่าสุด (COMEX:GC1!)"""
    url = "https://scanner.tradingview.com/futures/scan"
    payload = {
        "symbols": {"tickers": ["COMEX:GC1!"], "query": {"types": []}},
        "columns": ["close", "change", "volume", "open", "high", "low"],
    }
    resp = requests.post(url, headers=TV_HEADERS, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    if not data:
        raise RuntimeError("ไม่พบข้อมูลราคา COMEX:GC1!")
    d = data[0]["d"]
    return {
        "futures_price": d[0],
        "future_chg": d[1],
        "futures_volume": d[2],
        "open": d[3],
        "high": d[4],
        "low": d[5],
    }


def fetch_gc_options_chain(cme_trade_date_int: int) -> list:
    """
    ดึงข้อมูล Options Volume & OI ของ COMEX:GC1! จาก TradingView Scanner
    โดยกรองเฉพาะสัญญาที่วันหมดอายุ >= วันเทรด CME ปัจจุบันเท่านั้น
    """
    url = "https://scanner.tradingview.com/options/scan2?label-product=options-builder"
    payload = {
        "columns": [
            "strike",
            "option-type",
            "expiration",
            "volume",
            "open_interest",
            "iv",
            "delta",
            "gamma",
        ],
        "filter": [
            {"left": "expiration", "operation": "egreater", "right": cme_trade_date_int}
        ],
        "index_filters": [{"name": "underlying_symbol", "values": ["COMEX:GC1!"]}],
        "range": [0, 1000],
        "sort": {"sortBy": "expiration", "sortOrder": "asc"},
    }
    resp = requests.post(url, headers=TV_HEADERS, json=payload, timeout=20)
    resp.raise_for_status()
    raw_items = resp.json().get("data", [])

    parsed = []
    for item in raw_items:
        f = item.get("f", [])
        if len(f) < 6 or f[0] is None or f[2] is None:
            continue
        parsed.append({
            "symbol": item.get("s", ""),
            "strike": float(f[0]),
            "type": str(f[1]).lower(),  # 'call' หรือ 'put'
            "expiration": int(f[2]),    # YYYYMMDD
            "volume": int(f[3] or 0),
            "oi": int(f[4] or 0),
            "iv": float(f[5]) if f[5] is not None else None,
            "delta": float(f[6]) if len(f) > 6 and f[6] is not None else None,
            "gamma": float(f[7]) if len(f) > 7 and f[7] is not None else None,
        })
    return parsed


def build_intraday_snapshot(underlying: dict, options: list, session: dict) -> dict:
    """คัดเฉพาะซีรีส์ที่ใกล้หมดอายุที่สุด (>= วันเทรดปัจจุบัน) และเลือก 30 Strikes รอบราคาปัจจุบัน"""
    if not options:
        raise RuntimeError("ไม่พบสัญญา Options ที่ยังไม่หมดอายุในรอบนี้")

    # หาวันหมดอายุที่ใกล้ที่สุดที่ >= cme_trade_date_int
    expirations = sorted({o["expiration"] for o in options if o["expiration"] >= session["cme_trade_date_int"]})
    nearest_exp = expirations[0]
    exp_opts = [o for o in options if o["expiration"] == nearest_exp]

    # รวมข้อมูลตาม Strike
    by_strike = {}
    for o in exp_opts:
        st = o["strike"]
        if st not in by_strike:
            by_strike[st] = {
                "strike": st,
                "call_vol": 0,
                "put_vol": 0,
                "call_oi": 0,
                "put_oi": 0,
                "call_iv": None,
                "put_iv": None,
            }
        if o["type"] == "call":
            by_strike[st]["call_vol"] += o["volume"]
            by_strike[st]["call_oi"] += o["oi"]
            by_strike[st]["call_iv"] = o["iv"]
        elif o["type"] == "put":
            by_strike[st]["put_vol"] += o["volume"]
            by_strike[st]["put_oi"] += o["oi"]
            by_strike[st]["put_iv"] = o["iv"]

    # คัด 30 Strikes ที่ใกล้ราคา Futures ปัจจุบันที่สุด
    fut_price = underlying["futures_price"]
    sorted_strikes = sorted(by_strike.values(), key=lambda x: abs(x["strike"] - fut_price))[:30]
    sorted_strikes.sort(key=lambda x: x["strike"])

    # หา Call Wall, Put Wall (อิง OI สูงสุด หาก OI เป็น 0 ให้ใช้ Volume)
    call_wall = max(sorted_strikes, key=lambda x: (x["call_oi"], x["call_vol"]))["strike"]
    put_wall = max(sorted_strikes, key=lambda x: (x["put_oi"], x["put_vol"]))["strike"]

    # หา ATM IV จาก Strike ที่ใกล้ราคา Futures ที่สุด
    atm_row = min(sorted_strikes, key=lambda x: abs(x["strike"] - fut_price))
    ivs = [v for v in (atm_row["call_iv"], atm_row["put_iv"]) if v is not None]
    atm_iv = round(sum(ivs) / len(ivs), 2) if ivs else None

    total_call_vol = sum(x["call_vol"] for x in sorted_strikes)
    total_put_vol = sum(x["put_vol"] for x in sorted_strikes)

    exp_str = f"{str(nearest_exp)[:4]}-{str(nearest_exp)[4:6]}-{str(nearest_exp)[6:]}"

    return {
        "verified_realtime": True,
        "session": session,
        "expiration_used": exp_str,
        "underlying": underlying,
        "summary": {
            "atm_iv": atm_iv,
            "call_wall": call_wall,
            "put_wall": put_wall,
            "total_call_vol": total_call_vol,
            "total_put_vol": total_put_vol,
            "pc_vol_ratio": round(total_put_vol / total_call_vol, 2) if total_call_vol > 0 else None,
        },
        "strikes_30": sorted_strikes,
    }


def main():
    now_utc = get_realtime_utc()
    session = evaluate_cme_session(now_utc)
    output_path = "data.json"

    print(f"[TIME CHECK] TH: {session['realtime_th']} | CME: {session['realtime_cme']}")
    print(f"[SESSION] Status: {session['market_status']} | CME Trade Date: {session['cme_trade_date']}")

    # ด่านป้องกัน: ถ้าตลาดปิด (เสาร์-อาทิตย์ หรือพักตลาด 16:00-17:00 CT) ไม่ต้องเขียนทับเสา Volume เดิม
    if not session["is_open"]:
        print(f"[SKIP] ตลาดปิด ({session['market_status']}) — รักษาข้อมูลล่าสุดใน {output_path} ไว้เหมือนเดิม")
        if os.path.exists(output_path):
            with open(output_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            existing["last_checked_th"] = session["realtime_th"]
            existing["session"] = session
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
        return

    try:
        underlying = fetch_underlying_gc()
        options = fetch_gc_options_chain(session["cme_trade_date_int"])
        snapshot = build_intraday_snapshot(underlying, options, session)

        # ด่านป้องกันข้อมูลว่าง: ถ้า Volume รวมเป็น 0 ทั้งหมด ให้แจ้งเตือนและไม่เขียนทับข้อมูลเดิม
        if snapshot["summary"]["total_call_vol"] == 0 and snapshot["summary"]["total_put_vol"] == 0:
            print("[WARN] ปริมาณเสา Intraday Volume = 0 (อาจเพิ่งเปิดรอบใหม่หรือข้อมูลยังไม่เข้า) — ข้ามการเขียนทับ")
            return

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        print(f"[SUCCESS] อัปเดต {output_path} เรียบร้อย | Expiration: {snapshot['expiration_used']} | Futures: {underlying['futures_price']}")

    except Exception as e:
        print(f"[ERROR] ดึงข้อมูลไม่สำเร็จ: {e}")
        raise


if __name__ == "__main__":
    main()
