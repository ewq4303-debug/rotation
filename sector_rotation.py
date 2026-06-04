#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台股類股輪動 (RRG) 計算

流程：
  讀 sector_map.json → FinMind 抓還原股價 → 各類股等權合成指數
  → 計算 RRG (RS-Ratio / RS-Momentum) → 輸出 rrg_data.json 給前端

基準 (benchmark)：預設用「所抓universe的等權平均」當大盤 proxy，整套自洽、
保證可跑。若想改用 TAIEX，請確認 FinMind 對應 dataset 後在 build_benchmark 換掉。

環境變數：FINMIND_TOKEN（選用，提高速率上限到 600/hr）
"""
import os, sys, json, time
from collections import defaultdict
from datetime import datetime, timedelta

import requests
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
TOKEN = os.environ.get("FINMIND_TOKEN", "")

# ── 參數（可調）──────────────────────────────────────────────
RRG_WIN       = 63    # 標準化滾動視窗（約一季交易日）
RRG_MOM       = 21    # 動量回看（約一個月）
TAIL_DAYS     = 8     # 輸出的輪動軌跡尾巴長度
LOOKBACK_DAYS = 420   # 抓多久歷史（需 > WIN+MOM+TAIL+緩衝）
MIN_MEMBERS   = 3     # 一類股至少幾檔成員才納入（越大越穩、建議 5~8）
REQUEST_SLEEP = 0.4   # 每次 FinMind 請求間隔，避開速率上限
PRICE_DATASET = "TaiwanStockPrice"   # 還原股價可改 "TaiwanStockPriceAdj"


# ── FinMind 抓取 ─────────────────────────────────────────────
def finmind_get(dataset, data_id=None, start_date=None, end_date=None):
    params = {"dataset": dataset}
    if data_id:    params["data_id"] = data_id
    if start_date: params["start_date"] = start_date
    if end_date:   params["end_date"] = end_date
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    r = requests.get(FINMIND_URL, params=params, headers=headers, timeout=60)
    r.raise_for_status()
    return pd.DataFrame(r.json().get("data", []))


def fetch_prices(stock_ids, start_date, end_date):
    """逐檔抓收盤，回傳長表 [date, stock_id, close]。"""
    frames = []
    empty_ct, no_close_ct = 0, 0
    for i, sid in enumerate(stock_ids, 1):
        try:
            df = finmind_get(PRICE_DATASET, data_id=sid,
                             start_date=start_date, end_date=end_date)
            # ── 診斷：第一檔印出欄位名 ──
            if i == 1:
                print(f"  [debug] 第一檔 {sid} 欄位: {list(df.columns)}")
                if not df.empty:
                    print(f"  [debug] 第一行: {df.iloc[0].to_dict()}")
            if df.empty:
                empty_ct += 1; continue
            # 自動偵測 close 欄位（不分大小寫）
            col_map = {c.lower(): c for c in df.columns}
            close_col = col_map.get("close")
            if not close_col:
                no_close_ct += 1
                if no_close_ct == 1:
                    print(f"  [warn] {sid} 找不到 close 欄位，有: {list(df.columns)}")
                continue
            sub = df[["date", close_col]].copy()
            sub.columns = ["date", "close"]
            sub["stock_id"] = sid
            frames.append(sub)
        except Exception as e:
            print(f"  ! {sid} 抓取失敗：{e}", file=sys.stderr)
        time.sleep(REQUEST_SLEEP)
        if i % 25 == 0:
            print(f"  ...{i}/{len(stock_ids)}")
    print(f"  [summary] 空回傳={empty_ct}  無close={no_close_ct}  有效={len(frames)}")
    if not frames:
        raise RuntimeError("FinMind 沒抓到任何資料，請檢查 token / 網路")
    return pd.concat(frames, ignore_index=True)


# ── 合成類股指數 ─────────────────────────────────────────────
def build_price_matrix(long_df):
    print(f"  [debug] long_df: {len(long_df)} rows, {long_df['stock_id'].nunique()} stocks")
    m = long_df.pivot_table(index="date", columns="stock_id",
                            values="close", aggfunc="last")
    m.index = pd.to_datetime(m.index)
    m = m.sort_index().astype(float)
    print(f"  [debug] price_matrix: {m.shape[0]} days × {m.shape[1]} stocks")
    return m


def synthesize(price_matrix, sector_map, min_members):
    """等權合成：各類股 = 成員日報酬平均的累積指數；基準 = 全universe等權。"""
    rets = price_matrix.pct_change()
    bench = (1 + rets.mean(axis=1).fillna(0)).cumprod() * 100

    groups = defaultdict(list)
    for sid, sec in sector_map.items():
        if sid in rets.columns:
            groups[sec].append(sid)
    matched = sum(len(v) for v in groups.values())
    print(f"  [debug] sector_map 比對成功: {matched}/{len(sector_map)} 檔")

    levels, used = {}, {}
    skipped = []
    for sec, sids in groups.items():
        if len(sids) < min_members:
            skipped.append(f"{sec}({len(sids)})")
            continue
        sec_ret = rets[sids].mean(axis=1)
        levels[sec] = (1 + sec_ret.fillna(0)).cumprod() * 100
        used[sec] = len(sids)
    print(f"  [debug] 通過門檻: {len(levels)} 類, 被濾掉: {len(skipped)} 類")
    if skipped:
        print(f"  [debug] 濾掉的: {', '.join(skipped[:10])}")
    return pd.DataFrame(levels), bench, used


# ── RRG ──────────────────────────────────────────────────────
def compute_rrg(sector, bench, win=RRG_WIN, mom=RRG_MOM):
    rs = 100 * sector / bench                                  # 相對強弱比值
    rs_ratio = 100 + (rs - rs.rolling(win).mean()) / rs.rolling(win).std()
    roc = rs_ratio - rs_ratio.shift(mom)                       # RS-Ratio 的動量
    rs_mom = 100 + (roc - roc.rolling(win).mean()) / roc.rolling(win).std()
    return rs_ratio, rs_mom


def quadrant(x, y):
    if x >= 100 and y >= 100: return "領先"
    if x >= 100 and y <  100: return "轉弱"
    if x <  100 and y <  100: return "落後"
    return "改善"


def build_output(sectors, bench, used):
    out = []
    for sec in sectors.columns:
        x, y = compute_rrg(sectors[sec], bench)
        d = pd.DataFrame({"x": x, "y": y}).dropna()
        if d.empty:
            continue
        tail = d.tail(TAIL_DAYS)
        pts = [{"date": idx.strftime("%Y-%m-%d"),
                "x": round(float(rx), 2), "y": round(float(ry), 2)}
               for idx, rx, ry in zip(tail.index, tail["x"], tail["y"])]
        out.append({
            "name": sec,
            "members": used[sec],
            "quadrant": quadrant(pts[-1]["x"], pts[-1]["y"]),
            "tail": pts,                       # 由舊到新，最後一點為現況
        })
    out.sort(key=lambda s: (s["quadrant"], s["name"]))
    return {
        "as_of": datetime.today().strftime("%Y-%m-%d"),
        "benchmark": "追蹤universe等權平均",
        "params": {"win": RRG_WIN, "mom": RRG_MOM, "tail": TAIL_DAYS,
                   "min_members": MIN_MEMBERS},
        "sectors": out,
    }


def main():
    with open(os.path.join(HERE, "sector_map.json"), encoding="utf-8") as f:
        sector_map = json.load(f)
    stock_ids = sorted(set(sector_map.keys()))
    end = datetime.today()
    start = end - timedelta(days=LOOKBACK_DAYS)
    print(f"映射 {len(stock_ids)} 檔，抓取 {start.date()} ~ {end.date()}")

    long_df = fetch_prices(stock_ids, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    price = build_price_matrix(long_df)
    sectors, bench, used = synthesize(price, sector_map, MIN_MEMBERS)
    result = build_output(sectors, bench, used)

    with open(os.path.join(HERE, "rrg_data.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"完成：{len(result['sectors'])} 類 → rrg_data.json")


if __name__ == "__main__":
    main()
