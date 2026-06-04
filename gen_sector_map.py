#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
產生 / 重建 sector_map.json （股票代號 -> 自訂細分類股）

分三層，越下面越細、越不穩：
  1) 內建 curated SEED        : 主要電子細分 + 各大非電子龍頭，立即可用、最穩
  2) FinMind 官方產業別 (選用) : 補齊 SEED 沒列到的個股，用證交所官方 8 電子子類等
  3) MoneyDJ 同業頁面 (選用)   : 想更細時用；非官方、版型會改，失敗不影響前兩層

用法（都在 GitHub Actions / 有網路環境執行）：
  python gen_sector_map.py                 # 只輸出 SEED（不需網路 / token）
  python gen_sector_map.py --finmind       # SEED + FinMind 官方產業別補齊
  python gen_sector_map.py --finmind --moneydj   # 再疊 MoneyDJ 細分

環境變數：FINMIND_TOKEN（選用，可提高 FinMind 速率上限）
"""
import os, re, sys, json, time, argparse
import requests

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sector_map.json")
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
TOKEN = os.environ.get("FINMIND_TOKEN", "")

# ── 1) curated SEED：手動維護的細分對照（季度更新一次即可）────────────────
# 電子業細分到供應鏈 / 主題層級；非電子用官方大類龍頭代表。
SEED = {
    # ── 半導體 ─────────────────────────────
    "晶圓代工":   ["2330", "2303", "6770", "5347"],
    "IC設計":     ["2454", "3034", "2379", "3443", "3661", "4966", "8016", "3035", "6531"],
    "矽智財IP":   ["3529", "6643", "3293"],
    "封裝測試":   ["3711", "6239", "2449", "8150", "6147"],
    "記憶體":     ["2337", "2344", "2408", "4967"],
    "矽晶圓材料": ["6488", "5483", "8358"],
    "半導體設備": ["3680", "3413", "2360", "6196"],
    "IC通路":     ["3036", "2347", "3025"],
    # ── 電子零組件 / 下游 ───────────────────
    "PCB載板":    ["3037", "8046", "3189", "2368", "6269"],
    "被動元件":   ["2327", "2492", "3026", "2375"],
    "散熱":       ["3017", "3324", "6230", "8996"],
    "連接器線材": ["3023", "2392", "2308"],
    "面板":       ["2409", "3481", "6116"],
    "光學鏡頭":   ["3008", "3406", "2393"],
    # ── 系統 / 組裝 / 網通 ──────────────────
    "AI伺服器":   ["2317", "2382", "3231", "6669", "2376", "4938", "2356"],
    "網通":       ["2345", "6285", "3596", "4906"],
    # ── 非電子：官方大類龍頭代表 ────────────
    "金融保險":   ["2881", "2882", "2891", "2886", "2884", "2892", "5880", "2880"],
    "航運":       ["2603", "2609", "2615", "2606", "2637"],
    "鋼鐵":       ["2002", "2014", "2027", "2023"],
    "塑膠":       ["1301", "1303", "1326", "6505"],
    "水泥":       ["1101", "1102"],
    "食品":       ["1216", "1227", "1210", "1229"],
    "紡織纖維":   ["1402", "1476", "1477", "1437"],
    "汽車":       ["2207", "2201", "2227", "2204"],
    "觀光餐旅":   ["2707", "2731", "5706", "2723"],
    "生技醫療":   ["6446", "1762", "4736", "1789", "6491"],
    "建材營造":   ["2542", "2545", "5522", "2548"],
    "電機機械":   ["1519", "1503", "1513", "1504"],
    "貿易百貨":   ["2912", "5903", "2903", "2915"],
    "油電燃氣":   ["9937", "9908", "9931"],
}

# 證交所官方電子 8 子類（FinMind industry_category 會出現的字串）
OFFICIAL_ELECTRONIC = {
    "半導體業", "電腦及週邊設備業", "光電業", "通信網路業",
    "電子零組件業", "電子通路業", "資訊服務業", "其他電子業",
}
# 補齊基底時要排除的非個股類別
EXCLUDE_CATEGORY = {"ETF", "ETN", "Index", "大盤", "存託憑證", "受益證券", ""}

# 想用 MoneyDJ 細分時填：{自訂類股名稱: MoneyDJ 同業代碼}
# 代碼可在 moneydj 同業頁面網址 ?a=Cxxxxxx 取得（例：IC設計 C023190）
MONEYDJ_CODES = {
    "IC設計":   "C023190",
    "晶圓代工": "C023181",
    "封裝測試": "C023203",
    # 需要更多就自己加，例如 類比IC C023183 …
}


def finmind_get(dataset):
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    r = requests.get(FINMIND_URL, params={"dataset": dataset}, headers=headers, timeout=60)
    r.raise_for_status()
    return r.json().get("data", [])


def base_from_seed():
    """攤平 SEED 成 {stock_id: sector}。後加入者覆蓋先前者（細分優先）。"""
    m = {}
    for sector, ids in SEED.items():
        for sid in ids:
            m[sid] = sector
    return m


def enrich_with_finmind(m):
    """用官方產業別補齊 SEED 沒列到的個股；已在 SEED 的不覆蓋。"""
    try:
        rows = finmind_get("TaiwanStockInfo")
    except Exception as e:
        print(f"[finmind] 取 TaiwanStockInfo 失敗，跳過官方補齊：{e}", file=sys.stderr)
        return m
    added = 0
    for r in rows:
        sid = str(r.get("stock_id", ""))
        cat = (r.get("industry_category") or "").strip()
        if not re.fullmatch(r"\d{4}", sid):       # 只要 4 碼普通股
            continue
        if cat in EXCLUDE_CATEGORY:
            continue
        if sid in m:                              # SEED 細分優先，不覆蓋
            continue
        m[sid] = cat
        added += 1
    print(f"[finmind] 官方產業別補齊 {added} 檔")
    return m


def scrape_moneydj(code):
    """抓 MoneyDJ 同業頁面成員股號。非官方，selector 可能要依實際版型微調。"""
    url = f"https://www.moneydj.com/z/zh/zha/zh00.djhtm?a={code}"
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.encoding = "cp950"                          # MoneyDJ 為 Big5/cp950
    html = r.text
    ids = re.findall(r"[?&]a=(\d{4})\b", html)    # 內頁連結常帶 a=股號
    if not ids:                                   # 備援：表格內裸 4 碼
        ids = re.findall(r">\s*(\d{4})\s*<", html)
    return sorted(set(ids))


def enrich_with_moneydj(m):
    for sector, code in MONEYDJ_CODES.items():
        try:
            ids = scrape_moneydj(code)
            for sid in ids:
                m[sid] = sector                   # MoneyDJ 細分覆蓋官方大類
            print(f"[moneydj] {sector} ({code}) → {len(ids)} 檔")
        except Exception as e:
            print(f"[moneydj] {sector} ({code}) 失敗，跳過：{e}", file=sys.stderr)
        time.sleep(1.0)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--finmind", action="store_true", help="用 FinMind 官方產業別補齊")
    ap.add_argument("--moneydj", action="store_true", help="再疊 MoneyDJ 細分（需 --finmind 或 SEED）")
    args = ap.parse_args()

    m = base_from_seed()
    print(f"[seed] 內建 {len(m)} 檔")
    if args.finmind:
        m = enrich_with_finmind(m)
    if args.moneydj:
        m = enrich_with_moneydj(m)

    m = dict(sorted(m.items()))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    n_sectors = len(set(m.values()))
    print(f"完成：{len(m)} 檔 / {n_sectors} 類 → {OUT}")


if __name__ == "__main__":
    main()
