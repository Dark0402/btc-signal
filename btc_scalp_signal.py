"""
BTC-USDT-SWAP 10x 左側短線多空訊號系統
========================================
策略：RSI(14) + Stochastic(14) + Order Book + Funding Rate
週期：15m K線，每 15 分鐘掃描一次
通知：只在訊號狀態改變時發 Gmail
多週期輔助：1H / 4H / 1D 趨勢確認（止損輔助）

回測績效（15m/800根，7.5天）：
  RSI<30 + Stoch<25 → 做多：勝率 67%，平均收益 +2.85%（10x）
  RSI>70 + Stoch>75 → 做空：同組合
  最佳持倉週期：90 分鐘（6根15m）
  建議停損：進場後反向 0.5%（10x = 帳戶 5%）
"""
import json, os, smtplib, statistics, urllib.request
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

GMAIL_SENDER   = "jiunn04020@gmail.com"
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD", "srstpezdhyduggfv")  # 優先讀 GitHub Secret
GMAIL_RECEIVER = "jiunn04020@gmail.com"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "btc_scalp_state.json")
BASE = "https://www.okx.com"

RSI_LONG     = 30
RSI_SHORT    = 70
STOCH_LONG   = 25
STOCH_SHORT  = 75
SCORE_THRESH = 40
COOLDOWN_MIN = 90

# ── State ──────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f: return json.load(f)
    return {"direction": "NEUTRAL", "score": 0, "since": None, "last_alerted_ts": 0, "last_alerted_dir": None, "history": []}

def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f, ensure_ascii=False, indent=2)

# ── OKX API ────────────────────────────────────────────────
def _get(path):
    req = urllib.request.Request(BASE+path, headers={"User-Agent":"Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r: return json.loads(r.read())

def fetch_candles(bar="15m", limit=50):
    data = _get(f"/api/v5/market/candles?instId=BTC-USDT-SWAP&bar={bar}&limit={limit}")
    return [{"ts":int(c[0]),"o":float(c[1]),"h":float(c[2]),"l":float(c[3]),"c":float(c[4]),"v":float(c[5])}
            for c in reversed(data["data"])]

def fetch_funding():
    d = _get("/api/v5/public/funding-rate?instId=BTC-USDT-SWAP")["data"][0]
    return float(d["fundingRate"])*100

def fetch_orderbook(depth=10):
    data = _get(f"/api/v5/market/books?instId=BTC-USDT-SWAP&sz={depth}")["data"][0]
    bid_vol = sum(float(b[1]) for b in data["bids"][:depth])
    ask_vol = sum(float(a[1]) for a in data["asks"][:depth])
    ratio = bid_vol / ask_vol if ask_vol > 0 else 1.0
    return round(ratio, 3), round(bid_vol, 1), round(ask_vol, 1)

def fetch_oi():
    data = _get("/api/v5/public/open-interest?instId=BTC-USDT-SWAP")["data"][0]
    return float(data["oiCcy"])

# ── 指標 ───────────────────────────────────────────────────
def calc_rsi(closes, period=14):
    if len(closes) <= period: return 50
    deltas = [closes[i]-closes[i-1] for i in range(1,len(closes))]
    g = [max(d,0) for d in deltas[-period*2:]]
    l = [max(-d,0) for d in deltas[-period*2:]]
    ag = statistics.mean(g[-period:]); al = statistics.mean(l[-period:])
    if al == 0: return 100
    return round(100 - 100/(1+ag/al), 2)

def calc_stoch(candles, k_period=14, smooth=3):
    if len(candles) < k_period+smooth: return 50, 50
    ks = []
    for j in range(smooth):
        w = candles[-(k_period+smooth-j):len(candles)-j if j>0 else len(candles)]
        lo=min(c["l"] for c in w); hi=max(c["h"] for c in w)
        p=w[-1]["c"]
        ks.append((p-lo)/(hi-lo)*100 if hi!=lo else 50)
    k = ks[-1]; d = statistics.mean(ks)
    return round(k,2), round(d,2)

def calc_vol_ratio(candles, short=4, long=20):
    if len(candles) < long+short: return 1.0
    now = statistics.mean([c["v"] for c in candles[-short:]])
    avg = statistics.mean([c["v"] for c in candles[-long:-short]])
    return round(now/avg if avg>0 else 1.0, 2)

def calc_ema(closes, period=20):
    """指數移動平均（EMA）"""
    if len(closes) < period: return closes[-1]
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 1)

# ── 多週期分析 ─────────────────────────────────────────────
def analyze_tf(bar, limit=60):
    """
    分析單一週期的趨勢偏向，用於輔助止損判斷。
    回傳 dict: rsi, ema20, above_ema, momentum_pct, bias
    bias: BULL / BEAR / OVERBOUGHT / OVERSOLD / NEUTRAL
    """
    try:
        candles = fetch_candles(bar, limit)
        closes  = [c["c"] for c in candles]
        rsi     = calc_rsi(closes, 14)
        ema20   = calc_ema(closes, 20)
        price   = closes[-1]
        above   = price > ema20
        mom     = round((closes[-1] - closes[-2]) / closes[-2] * 100, 3) if len(closes) >= 2 else 0.0

        # 偏向判斷
        if rsi > 72:
            bias = "OVERBOUGHT"
        elif rsi < 28:
            bias = "OVERSOLD"
        elif rsi > 58 and not above:
            bias = "BEAR"
        elif rsi < 42 and above:
            bias = "BULL"
        elif above:
            bias = "BULL"
        else:
            bias = "BEAR"

        return {"rsi": rsi, "ema20": ema20, "price": price,
                "above_ema": above, "momentum": mom, "bias": bias}
    except Exception as e:
        return {"rsi": 50, "ema20": 0, "price": 0,
                "above_ema": None, "momentum": 0, "bias": "N/A", "error": str(e)}

def fetch_mtf():
    """拉取 1H / 4H / 1D 三個週期的分析"""
    return {
        "1H":  analyze_tf("1H",  60),
        "4H":  analyze_tf("4H",  60),
        "1D":  analyze_tf("1D",  30),
    }

# ── 訊號評分 ───────────────────────────────────────────────
def evaluate(candles_15m, funding_rate, ob_ratio):
    closes = [c["c"] for c in candles_15m]
    price  = closes[-1]

    rsi14 = calc_rsi(closes, 14)
    rsi7  = calc_rsi(closes, 7)
    sk, sd = calc_stoch(candles_15m)
    vr    = calc_vol_ratio(candles_15m)
    atr14 = statistics.mean([c["h"]-c["l"] for c in candles_15m[-14:]])

    # ── LONG scoring ────────────────────────────────────────
    ls, lr = 0, []

    if rsi14 < 25:
        ls += 45; lr.append(f"RSI(14)={rsi14:.1f} — 極度超賣")
    elif rsi14 < RSI_LONG:
        ls += 30; lr.append(f"RSI(14)={rsi14:.1f} — 超賣")

    if sk < 15:
        ls += 30; lr.append(f"Stoch %K={sk:.1f} — 極度超賣")
    elif sk < STOCH_LONG:
        ls += 20; lr.append(f"Stoch %K={sk:.1f} — 超賣")

    if rsi7 < 25:
        ls += 10; lr.append(f"RSI(7)={rsi7:.1f} 同步超賣")

    if ob_ratio > 1.5:
        ls += 15; lr.append(f"掛單買盤 {ob_ratio:.2f}x 強於賣盤 — 支撐強")
    elif ob_ratio > 1.1:
        ls += 8;  lr.append(f"掛單買盤略強 {ob_ratio:.2f}x")

    if vr > 2.0:
        ls += 10; lr.append(f"成交量爆量 {vr:.1f}x — 可能恐慌底")
    elif vr > 1.3:
        ls += 5;  lr.append(f"成交量略放大 {vr:.1f}x")

    if funding_rate < -0.05:
        ls += 15; lr.append(f"資金費率 {funding_rate:.4f}% 極端負 — 逼空風險")
    elif funding_rate < -0.01:
        ls += 8;  lr.append(f"資金費率 {funding_rate:.4f}% 偏負")

    # ── SHORT scoring ───────────────────────────────────────
    ss, sr = 0, []

    if rsi14 > 75:
        ss += 45; sr.append(f"RSI(14)={rsi14:.1f} — 極度超買")
    elif rsi14 > RSI_SHORT:
        ss += 30; sr.append(f"RSI(14)={rsi14:.1f} — 超買")

    if sk > 85:
        ss += 30; sr.append(f"Stoch %K={sk:.1f} — 極度超買")
    elif sk > STOCH_SHORT:
        ss += 20; sr.append(f"Stoch %K={sk:.1f} — 超買")

    if rsi7 > 75:
        ss += 10; sr.append(f"RSI(7)={rsi7:.1f} 同步超買")

    if ob_ratio < 0.67:
        ss += 15; sr.append(f"掛單賣盤 {1/ob_ratio:.2f}x 強於買盤 — 賣壓重")
    elif ob_ratio < 0.9:
        ss += 8;  sr.append(f"掛單賣盤略強 ratio={ob_ratio:.2f}")

    if vr > 2.0:
        ss += 10; sr.append(f"成交量爆量 {vr:.1f}x — 可能衝高反轉")
    elif vr > 1.3:
        ss += 5;  sr.append(f"成交量略放大 {vr:.1f}x")

    if funding_rate > 0.05:
        ss += 15; sr.append(f"資金費率 {funding_rate:.4f}% 極端正 — 多殺多風險")
    elif funding_rate > 0.01:
        ss += 8;  sr.append(f"資金費率 {funding_rate:.4f}% 偏正")

    # ── 方向判斷 ────────────────────────────────────────────
    if ls >= SCORE_THRESH and rsi14 < RSI_LONG:
        direction, score, reasons = "LONG", ls, lr
    elif ss >= SCORE_THRESH and rsi14 > RSI_SHORT:
        direction, score, reasons = "SHORT", ss, sr
    else:
        direction = "NEUTRAL"
        score = max(ls, ss)
        reasons = [f"RSI={rsi14:.1f} Stoch={sk:.1f} — 區間整理，等待極值"]

    sl_pct, tp_pct = 0.005, 0.012
    if direction == "LONG":
        sl = round(price * (1 - sl_pct), 1)
        tp = round(price * (1 + tp_pct), 1)
    elif direction == "SHORT":
        sl = round(price * (1 + sl_pct), 1)
        tp = round(price * (1 - tp_pct), 1)
    else:
        sl = tp = None

    return {
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "price": price,
        "rsi14": rsi14, "rsi7": rsi7, "sk": sk, "sd": sd,
        "vol_ratio": vr, "funding_rate": funding_rate, "ob_ratio": ob_ratio,
        "direction": direction, "score": score,
        "long_score": ls, "short_score": ss,
        "reasons": reasons, "sl": sl, "tp": tp, "atr": round(atr14,1)
    }

# ── HTML ───────────────────────────────────────────────────
DIR_COLOR = {"LONG":"#16a34a","SHORT":"#dc2626","NEUTRAL":"#d97706"}
DIR_EMOJI = {"LONG":"Long","SHORT":"Short","NEUTRAL":"Neutral"}

BIAS_COLOR = {
    "BULL":       "#16a34a",
    "BEAR":       "#dc2626",
    "OVERBOUGHT": "#f97316",
    "OVERSOLD":   "#06b6d4",
    "NEUTRAL":    "#64748b",
    "N/A":        "#64748b",
}
BIAS_LABEL = {
    "BULL":       "BULL (支持多)",
    "BEAR":       "BEAR (支持空)",
    "OVERBOUGHT": "OVERBOUGHT (謹慎多!)",
    "OVERSOLD":   "OVERSOLD (謹慎空!)",
    "NEUTRAL":    "NEUTRAL (中性)",
    "N/A":        "N/A",
}

def build_mtf_html(mtf, cur_dir):
    """建立多週期確認表格 HTML"""
    # 計算整體警告
    warnings = []
    if cur_dir == "LONG":
        for tf, d in mtf.items():
            if d["bias"] in ("BEAR", "OVERBOUGHT"):
                warnings.append(f"{tf} 偏空/超買（{BIAS_LABEL[d['bias']]}），逆大趨勢做多風險較高")
    elif cur_dir == "SHORT":
        for tf, d in mtf.items():
            if d["bias"] in ("BULL", "OVERSOLD"):
                warnings.append(f"{tf} 偏多/超賣（{BIAS_LABEL[d['bias']]}），逆大趨勢做空風險較高")

    warn_html = ""
    if warnings:
        warn_items = "".join(f"<li style='margin-bottom:3px;color:#fbbf24'>{w}</li>" for w in warnings)
        warn_html = (
            f"<div style='background:#92400e22;border:1px solid #f59e0b;border-radius:8px;"
            f"padding:10px 14px;margin-bottom:10px'>"
            f"<div style='color:#fbbf24;font-size:12px;font-weight:700;margin-bottom:4px'>"
            f"[!] 大週期警告 — 建議收緊止損或輕倉</div>"
            f"<ul style='margin:0;padding-left:16px;font-size:12px'>{warn_items}</ul></div>"
        )

    rows = ""
    for tf, d in mtf.items():
        bc  = BIAS_COLOR.get(d["bias"], "#64748b")
        bl  = BIAS_LABEL.get(d["bias"], d["bias"])
        ema_txt = "EMA上方" if d.get("above_ema") else ("EMA下方" if d.get("above_ema") is False else "N/A")
        ema_c   = "#16a34a" if d.get("above_ema") else ("#dc2626" if d.get("above_ema") is False else "#64748b")
        mom_c   = "#4ade80" if d.get("momentum",0) > 0 else "#f87171"
        mom_s   = f"+{d['momentum']:.2f}%" if d.get("momentum",0) > 0 else f"{d.get('momentum',0):.2f}%"
        rsi_c   = "#dc2626" if d["rsi"] > 70 else ("#16a34a" if d["rsi"] < 30 else "#94a3b8")
        rows += (
            f"<tr style='border-bottom:1px solid #1e293b'>"
            f"<td style='padding:8px 10px;font-weight:700;color:#f8fafc;font-size:13px'>{tf}</td>"
            f"<td style='padding:8px 10px;color:{rsi_c};font-size:13px'>{d['rsi']:.1f}</td>"
            f"<td style='padding:8px 10px;color:{ema_c};font-size:12px'>{ema_txt}</td>"
            f"<td style='padding:8px 10px;color:{mom_c};font-size:12px'>{mom_s}</td>"
            f"<td style='padding:8px 10px'>"
            f"<span style='background:{bc}22;color:{bc};border:1px solid {bc};"
            f"border-radius:4px;padding:2px 8px;font-size:11px;font-weight:700'>{bl}</span></td>"
            f"</tr>"
        )

    return (
        f"<div style='margin-top:18px;background:#0f172a;border-radius:8px;padding:14px'>"
        f"<div style='color:#94a3b8;font-size:11px;font-weight:700;margin-bottom:10px;letter-spacing:.05em'>"
        f"多週期趨勢確認 (止損輔助)</div>"
        f"{warn_html}"
        f"<table style='width:100%;border-collapse:collapse'>"
        f"<thead><tr style='border-bottom:1px solid #334155'>"
        f"<th style='padding:5px 10px;color:#64748b;text-align:left;font-size:11px'>週期</th>"
        f"<th style='padding:5px 10px;color:#64748b;text-align:left;font-size:11px'>RSI(14)</th>"
        f"<th style='padding:5px 10px;color:#64748b;text-align:left;font-size:11px'>EMA20</th>"
        f"<th style='padding:5px 10px;color:#64748b;text-align:left;font-size:11px'>最後K棒</th>"
        f"<th style='padding:5px 10px;color:#64748b;text-align:left;font-size:11px'>偏向</th>"
        f"</tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        f"<div style='font-size:10px;color:#475569;margin-top:8px'>"
        f"BULL=多頭偏向 BEAR=空頭偏向 OVERBOUGHT/OVERSOLD=極端區 · 逆大週期時請縮短持倉時間</div>"
        f"</div>"
    )

def build_html(r, prev_dir, event_type, mtf=None):
    c   = DIR_COLOR.get(r["direction"],"#94a3b8")
    ev  = {"appear":"[!] New Signal","reverse":"[~] Signal Reversed","disappear":"[OK] Signal Ended"}.get(event_type,"")
    reasons_html = "".join(f"<li style='margin-bottom:4px'>{x}</li>" for x in r["reasons"])
    rsi_w = r["rsi14"]
    rsi_c = "#dc2626" if r["rsi14"]>70 else ("#16a34a" if r["rsi14"]<30 else "#64748b")
    stoch_c = "#dc2626" if r["sk"]>75 else ("#16a34a" if r["sk"]<25 else "#64748b")
    ob_c = "#16a34a" if r["ob_ratio"]>1.2 else ("#dc2626" if r["ob_ratio"]<0.8 else "#64748b")

    sltp_html = ""
    if r["sl"]:
        sl_pnl = abs(r["sl"]-r["price"])/r["price"]*100*10
        tp_pnl = abs(r["tp"]-r["price"])/r["price"]*100*10
        sltp_html = (
            f"<div style='display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px'>"
            f"<div style='background:#dc262618;border:1px solid #dc2626;border-radius:8px;padding:12px;text-align:center'>"
            f"<div style='font-size:11px;color:#94a3b8'>Stop Loss (0.5% = -{sl_pnl:.0f}% @10x)</div>"
            f"<div style='color:#f87171;font-weight:700;font-size:16px'>${r['sl']:,.1f}</div></div>"
            f"<div style='background:#16a34a18;border:1px solid #16a34a;border-radius:8px;padding:12px;text-align:center'>"
            f"<div style='font-size:11px;color:#94a3b8'>Take Profit (1.2% = +{tp_pnl:.0f}% @10x)</div>"
            f"<div style='color:#4ade80;font-weight:700;font-size:16px'>${r['tp']:,.1f}</div></div></div>"
        )

    mtf_html = build_mtf_html(mtf, r["direction"]) if mtf else ""

    return (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'></head>"
        f"<body style='font-family:Arial,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:20px'>"
        f"<div style='background:#1e293b;border-radius:12px;padding:24px;max-width:640px;margin:0 auto'>"
        f"<div style='font-size:12px;color:#64748b;margin-bottom:2px'>{ev}</div>"
        f"<h1 style='font-size:19px;margin:2px 0;color:#f8fafc'>BTC-USDT-SWAP 左側短線訊號</h1>"
        f"<div style='font-size:12px;color:#94a3b8;margin-bottom:16px'>{r['time']} &nbsp;·&nbsp; 15m週期 &nbsp;·&nbsp; 10x槓桿"
        f"&nbsp;·&nbsp; {prev_dir} → <b style='color:{c}'>{r['direction']}</b></div>"
        f"<div style='display:inline-block;padding:8px 20px;border-radius:999px;"
        f"background:{c}22;color:{c};border:1.5px solid {c};font-size:24px;font-weight:700;margin-bottom:18px'>"
        f"{DIR_EMOJI.get(r['direction'],'')} {r['direction']} &nbsp; {r['score']}/100</div>"
        f"<div style='display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:8px;margin-bottom:16px'>"
        f"<div style='background:#0f172a;border-radius:8px;padding:10px;text-align:center'>"
        f"<div style='font-size:10px;color:#64748b'>Price</div>"
        f"<div style='font-size:15px;font-weight:700'>${r['price']:,.1f}</div></div>"
        f"<div style='background:#0f172a;border-radius:8px;padding:10px;text-align:center'>"
        f"<div style='font-size:10px;color:#64748b'>RSI(14)</div>"
        f"<div style='font-size:15px;font-weight:700;color:{rsi_c}'>{r['rsi14']:.1f}</div></div>"
        f"<div style='background:#0f172a;border-radius:8px;padding:10px;text-align:center'>"
        f"<div style='font-size:10px;color:#64748b'>Stoch %K</div>"
        f"<div style='font-size:15px;font-weight:700;color:{stoch_c}'>{r['sk']:.1f}</div></div>"
        f"<div style='background:#0f172a;border-radius:8px;padding:10px;text-align:center'>"
        f"<div style='font-size:10px;color:#64748b'>OB Ratio</div>"
        f"<div style='font-size:15px;font-weight:700;color:{ob_c}'>{r['ob_ratio']:.2f}x</div></div>"
        f"</div>"
        f"<div style='margin-bottom:12px'>"
        f"<div style='font-size:11px;color:#64748b;margin-bottom:4px'>RSI(14) position</div>"
        f"<div style='position:relative;height:10px;background:#0f172a;border-radius:5px'>"
        f"<div style='position:absolute;left:0;top:0;height:100%;background:#334155;width:30%;border-radius:5px 0 0 5px'></div>"
        f"<div style='position:absolute;left:70%;top:0;height:100%;background:#334155;width:30%;border-radius:0 5px 5px 0'></div>"
        f"<div style='position:absolute;top:-2px;left:{rsi_w}%;width:14px;height:14px;background:{rsi_c};border-radius:50%;transform:translateX(-50%)'></div>"
        f"</div>"
        f"<div style='display:flex;justify-content:space-between;font-size:10px;color:#475569;margin-top:2px'>"
        f"<span>0</span><span>30</span><span>50</span><span>70</span><span>100</span></div>"
        f"</div>"
        f"<div style='font-size:12px;color:#94a3b8;margin-bottom:12px'>"
        f"RSI(7): <b style='color:#f1f5f9'>{r['rsi7']:.1f}</b> &nbsp;·&nbsp; "
        f"Stoch %D: <b style='color:#f1f5f9'>{r['sd']:.1f}</b> &nbsp;·&nbsp; "
        f"Vol ratio: <b style='color:#f1f5f9'>{r['vol_ratio']:.2f}x</b> &nbsp;·&nbsp; "
        f"FR: <b style='color:#f1f5f9'>{r['funding_rate']:.4f}%</b></div>"
        f"<div style='background:#0f172a;border-radius:8px;padding:14px;margin-bottom:12px'>"
        f"<ul style='margin:0;padding-left:18px;line-height:1.9;font-size:13px;color:#cbd5e1'>{reasons_html}</ul></div>"
        f"{sltp_html}"
        f"{mtf_html}"
        f"<div style='font-size:11px;color:#475569;text-align:center;margin-top:14px;line-height:1.6'>"
        f"左側進場 · 建議停損 0.5% (-5% @10x) · 建議持倉 90min<br>"
        f"回測勝率 67% (7.5天樣本) · 僅供參考，請自行判斷風險</div>"
        f"</div></body></html>"
    )

def build_daily_html(history, r, mtf=None):
    rows = ""
    for h in history[-48:]:
        d=h.get("direction","NEUTRAL"); co=DIR_COLOR.get(d,"#94a3b8")
        score_str = str(h.get("score",""))
        rows += (f"<tr style='border-bottom:1px solid #1e293b'>"
                 f"<td style='padding:5px 8px;color:#94a3b8;font-size:12px'>{h.get('time','')[:16]}</td>"
                 f"<td style='padding:5px 8px;color:{co};font-weight:600;font-size:12px'>{d}</td>"
                 f"<td style='padding:5px 8px;font-size:12px'>${h.get('price',0):,.0f}</td>"
                 f"<td style='padding:5px 8px;font-size:12px'>{score_str}/100</td>"
                 f"<td style='padding:5px 8px;font-size:12px'>{h.get('rsi14',''):}</td>"
                 f"<td style='padding:5px 8px;font-size:12px'>{h.get('sk','')}</td></tr>")
    cur_c=DIR_COLOR.get(r["direction"],"#94a3b8")
    mtf_html = build_mtf_html(mtf, r["direction"]) if mtf else ""
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'></head>"
            f"<body style='font-family:Arial,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:20px'>"
            f"<div style='background:#1e293b;border-radius:12px;padding:24px;max-width:680px;margin:0 auto'>"
            f"<h1 style='font-size:19px;margin:0 0 4px;color:#f8fafc'>BTC 短線訊號 每日日報</h1>"
            f"<div style='font-size:13px;color:#94a3b8;margin-bottom:18px'>{r['time']} &nbsp;|&nbsp; ${r['price']:,.1f} &nbsp;|&nbsp; RSI={r['rsi14']:.1f} &nbsp;|&nbsp; <span style='color:{cur_c}'>{r['direction']}</span></div>"
            f"<table style='width:100%;border-collapse:collapse'>"
            f"<thead><tr style='border-bottom:1px solid #334155'>"
            f"<th style='padding:6px 8px;color:#64748b;text-align:left;font-size:11px'>Time</th>"
            f"<th style='padding:6px 8px;color:#64748b;text-align:left;font-size:11px'>Signal</th>"
            f"<th style='padding:6px 8px;color:#64748b;text-align:left;font-size:11px'>Price</th>"
            f"<th style='padding:6px 8px;color:#64748b;text-align:left;font-size:11px'>Score</th>"
            f"<th style='padding:6px 8px;color:#64748b;text-align:left;font-size:11px'>RSI14</th>"
            f"<th style='padding:6px 8px;color:#64748b;text-align:left;font-size:11px'>Stoch%K</th>"
            f"</tr></thead><tbody>{rows}</tbody></table>"
            f"{mtf_html}"
            f"<div style='font-size:11px;color:#475569;text-align:center;margin-top:16px'>僅供參考 · 不構成投資建議</div>"
            f"</div></body></html>")

# ── Email ───────────────────────────────────────────────────
def send_email(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"]=subject; msg["From"]=GMAIL_SENDER; msg["To"]=GMAIL_RECEIVER
    msg.attach(MIMEText(html_body,"html","utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com",465) as s:
            s.login(GMAIL_SENDER,GMAIL_PASSWORD)
            s.sendmail(GMAIL_SENDER,GMAIL_RECEIVER,msg.as_string())
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] {e}"); return False

# ── Main ────────────────────────────────────────────────────
def main(daily_report=False):
    now = datetime.now(timezone.utc)
    print(f"[{now.strftime('%H:%M:%S')}] BTC 15m scalp scan ...")

    try:
        candles = fetch_candles("15m", 60)
        funding = fetch_funding()
        ob_ratio, bid_v, ask_v = fetch_orderbook(10)
    except Exception as e:
        print(f"[API ERROR] {e}"); return

    r = evaluate(candles, funding, ob_ratio)
    state = load_state()
    prev_dir = state.get("direction","NEUTRAL")
    cur_dir  = r["direction"]

    # 拉取多週期分析
    print("  [MTF] 拉取 1H/4H/1D ...")
    mtf = fetch_mtf()
    for tf, d in mtf.items():
        ema_pos = "EMA上方" if d.get("above_ema") else "EMA下方"
        print(f"  {tf}: RSI={d['rsi']:.1f} {ema_pos} bias={d['bias']}")

    state.setdefault("history",[]).append({
        "time":r["time"],"direction":cur_dir,"price":r["price"],
        "score":r["score"],"rsi14":r["rsi14"],"sk":r["sk"]
    })
    state["history"] = state["history"][-96:]

    print(f"  ${r['price']:,.1f} | {cur_dir}({r['score']}/100) | RSI={r['rsi14']:.1f} Stoch={r['sk']:.1f} | OB={ob_ratio:.2f} | FR={funding:.4f}%")

    now_ts = datetime.now(timezone.utc).timestamp()
    last_alerted_ts  = state.get("last_alerted_ts", 0)
    last_alerted_dir = state.get("last_alerted_dir")
    minutes_since_last = (now_ts - last_alerted_ts) / 60

    is_reversal     = (prev_dir != "NEUTRAL" and cur_dir != "NEUTRAL" and cur_dir != prev_dir)
    same_dir_cooldown = (cur_dir == last_alerted_dir and minutes_since_last < COOLDOWN_MIN and not is_reversal)

    if cur_dir != prev_dir and not same_dir_cooldown:
        if prev_dir == "NEUTRAL":
            event = "appear"
            subj = f"[NEW {cur_dir}] BTC LEFT-SIDE {r['score']}/100 -- ${r['price']:,.0f}"
        elif cur_dir == "NEUTRAL":
            event = "disappear"
            subj = f"[END] BTC {prev_dir} signal ended -- ${r['price']:,.0f}"
        else:
            event = "reverse"
            subj = f"[REVERSE to {cur_dir}] BTC {r['score']}/100 -- ${r['price']:,.0f}"
        ok = send_email(subj, build_html(r, prev_dir, event, mtf=mtf))
        print(f"  {prev_dir} → {cur_dir} | email: {'OK' if ok else 'FAIL'}")
        state["last_alerted_ts"]  = now_ts
        state["last_alerted_dir"] = cur_dir
    elif same_dir_cooldown:
        remain = int(COOLDOWN_MIN - minutes_since_last)
        print(f"  {cur_dir} 訊號再現，但冷卻中（距上次通知 {int(minutes_since_last)}min，還需等 {remain}min）")
    else:
        print(f"  unchanged ({cur_dir}), no email")

    if daily_report:
        subj_d = f"BTC Scalp Daily {now.strftime('%m/%d')} -- ${r['price']:,.0f}"
        ok = send_email(subj_d, build_daily_html(state["history"], r, mtf=mtf))
        print(f"  daily digest: {'OK' if ok else 'FAIL'}")

    state["direction"] = cur_dir
    state["score"]     = r["score"]
    state["since"]     = r["time"] if cur_dir != "NEUTRAL" else None
    save_state(state)

if __name__ == "__main__":
    import sys
    main(daily_report="--daily" in sys.argv)
