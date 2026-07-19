import os
import sys
import time
import json
import asyncio
import requests
from typing import Optional
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ==============================================================================
# CONFIGURATION
# ==============================================================================

CONFIG_FILE = "config.json"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

DEFAULT_CONFIG = {
    "symbol": "BTCUSDT",
    "distance_limit": 200.0,
    "volume_threshold": 1000.0,
    "cluster_gap": 10.0,
    "cooldown_seconds": 180,
    "telegram_token": os.environ.get("TELEGRAM_TOKEN", ""),
    "chat_id": os.environ.get("CHAT_ID", ""),
    "bot_enabled": True,
}


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                merged = DEFAULT_CONFIG.copy()
                merged.update(data)
                return merged
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[CONFIG] Save error: {e}")


config = load_config()

# ==============================================================================
# STATE
# ==============================================================================

state = {
    "current_price": 0.0,
    "is_connected": False,
    "start_time": time.time(),
    "depth_bids": [],
    "depth_asks": [],
    "total_bid_depth": 0.0,
    "total_ask_depth": 0.0,
    "active_walls": {},
    "wall_history": [],
    "detected_walls_feed": [],
    "walls_detected_today": 0,
    "walls_eaten_today": 0,
    "walls_pulled_today": 0,
    "ath_wall_qty": 0.0,
    "ath_wall_price": 0.0,
    "ath_wall_side": "",
    "total_alerts_sent": 0,
    "last_scan_ts": 0,
    "alert_cooldowns": {},
}

# ==============================================================================
# FASTAPI APP & MODELS
# ==============================================================================

app = FastAPI(title="Binance 1K BTC Wall Detector")


class ConfigUpdate(BaseModel):
    symbol: str
    distance_limit: float
    volume_threshold: float
    cluster_gap: float
    cooldown_seconds: int
    telegram_token: str
    chat_id: str
    bot_enabled: bool


class AuthCheck(BaseModel):
    password: str


# ==============================================================================
# TELEGRAM HELPERS
# ==============================================================================


def _send_tg(text: str) -> bool:
    token = config.get("telegram_token", "").strip()
    chat_id = config.get("chat_id", "").strip()
    if not token or not chat_id:
        print(f"[TG] No credentials. Message not sent.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
        if r.status_code == 200:
            state["total_alerts_sent"] += 1
            return True
        print(f"[TG ERROR] {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[TG ERROR] {e}")
    return False


def send_wall_alert(wall: dict, current_price: float) -> bool:
    symbol = config.get("symbol", "BTCUSDT")
    side_emoji = "\U0001f7e2 <b>ALIM DUVARI (BID)</b>" if wall["side"] == "BUY" else "\U0001f534 <b>SATIM DUVARI (ASK)</b>"
    dist = abs(wall["price"] - current_price)
    pct = (dist / current_price * 100) if current_price > 0 else 0
    usdt_m = wall["price"] * wall["qty"] / 1e6

    lines = [
        "\U0001f6a8 <b>BINANCE FUTURES WALL ALERT!</b> \U0001f6a8",
        "",
        f"<b>Parite:</b> #{symbol}",
        f"<b>Taraf:</b> {side_emoji}",
    ]

    if wall.get("type") == "cluster":
        lines.append(f"\U0001f9e9 <b>Tip:</b> Cluster ({wall.get('levels_count', '?')} seviye)")
        if wall.get("price_range"):
            lines.append(f"<b>Aral\u0131k:</b> {wall['price_range']}")

    lines += [
        f"<b>Duvar Fiyat\u0131:</b> <code>${wall['price']:,.2f}</code>",
        f"<b>Miktar:</b> <b>{wall['qty']:,.2f} BTC</b> (~${usdt_m:,.2f}M)",
        f"<b>Mevcut Fiyat:</b> <code>${current_price:,.2f}</code>",
        f"<b>Mesafe:</b> ${dist:,.2f} (%{pct:.2f})",
        f"\n\u23f0 <i>{time.strftime('%H:%M:%S %d.%m.%Y')}</i>",
    ]
    sent = _send_tg("\n".join(lines))
    if sent:
        print(f"[ALERT] Wall {wall['side']} ${wall['price']:,.0f} {wall['qty']:,.0f} BTC")
    return sent


def send_lifecycle_alert(wall: dict, current_price: float, event: str) -> bool:
    symbol = config.get("symbol", "BTCUSDT")
    side_label = "ALIM (BID)" if wall["side"] == "BUY" else "SATIM (ASK)"
    lifetime = time.time() - wall.get("first_seen", time.time())
    mins = int(lifetime // 60)
    secs = int(lifetime % 60)
    time_str = f"{mins}dk {secs}sn" if mins > 0 else f"{secs}sn"
    usdt_m = wall["price"] * wall.get("original_qty", wall["qty"]) / 1e6

    if event == "eaten":
        lines = [
            "\U0001f525 <b>DUVAR YEN\u0130LD\u0130!</b> \U0001f525",
            "",
            f"<b>Parite:</b> #{symbol}",
            f"<b>Taraf:</b> {side_label}",
            f"<b>Duvar Fiyat\u0131:</b> <code>${wall['price']:,.2f}</code>",
            f"<b>Miktar:</b> {wall.get('original_qty', wall['qty']):,.2f} BTC (~${usdt_m:,.2f}M)",
            f"<b>Ya\u015fam S\u00fcresi:</b> {time_str}",
            f"<b>Mevcut Fiyat:</b> <code>${current_price:,.2f}</code>",
            "",
            "\U0001f4a5 Fiyat duvar\u0131 yiyerek ge\u00e7ti!",
            f"\n\u23f0 <i>{time.strftime('%H:%M:%S %d.%m.%Y')}</i>",
        ]
    else:
        lines = [
            "\U0001f47b <b>DUVAR \u00c7EK\u0130LD\u0130! (SPOOF?)</b> \U0001f47b",
            "",
            f"<b>Parite:</b> #{symbol}",
            f"<b>Taraf:</b> {side_label}",
            f"<b>Duvar Fiyat\u0131:</b> <code>${wall['price']:,.2f}</code>",
            f"<b>Miktar:</b> {wall.get('original_qty', wall['qty']):,.2f} BTC (~${usdt_m:,.2f}M)",
            f"<b>Ya\u015fam S\u00fcresi:</b> {time_str}",
            f"<b>Mevcut Fiyat:</b> <code>${current_price:,.2f}</code>",
            "",
            "\u26a0\ufe0f Fiyat ula\u015fmadan emir \u00e7ekildi!",
            f"\n\u23f0 <i>{time.strftime('%H:%M:%S %d.%m.%Y')}</i>",
        ]

    sent = _send_tg("\n".join(lines))
    if sent:
        print(f"[LIFECYCLE] {event.upper()} - {wall['side']} ${wall['price']:,.0f}")
    return sent


# ==============================================================================
# WALL DETECTION ENGINE
# ==============================================================================


def _wall_key(side: str, price: float) -> str:
    return f"{side}_{int(price / 10) * 10}"


def add_to_feed(wall: dict, current_price: float, event: str):
    record = {
        "id": int(time.time() * 1000),
        "side": wall["side"],
        "type": wall.get("type", "single"),
        "price": wall["price"],
        "qty": wall.get("original_qty", wall["qty"]),
        "usdt_val": wall["price"] * wall.get("original_qty", wall["qty"]),
        "distance": abs(wall["price"] - current_price),
        "current_price": current_price,
        "ts": time.strftime("%H:%M:%S"),
        "event": event,
        "levels_count": wall.get("levels_count", 1),
    }
    state["detected_walls_feed"].insert(0, record)
    if len(state["detected_walls_feed"]) > 50:
        state["detected_walls_feed"] = state["detected_walls_feed"][:50]


def add_to_history(wall: dict, current_price: float, event: str):
    lifetime = time.time() - wall.get("first_seen", time.time())
    record = {
        "id": int(time.time() * 1000),
        "side": wall["side"],
        "type": wall.get("type", "single"),
        "price": wall["price"],
        "qty": wall.get("original_qty", wall["qty"]),
        "event": event,
        "ts": time.strftime("%H:%M:%S"),
        "lifetime_seconds": int(lifetime),
        "current_price": current_price,
        "levels_count": wall.get("levels_count", 1),
    }
    state["wall_history"].insert(0, record)
    if len(state["wall_history"]) > 100:
        state["wall_history"] = state["wall_history"][:100]


def detect_and_track_walls(bids: list, asks: list, current_price: float):
    threshold = float(config.get("volume_threshold", 1000.0))
    cluster_gap = float(config.get("cluster_gap", 10.0))
    cooldown = int(config.get("cooldown_seconds", 180))
    now = time.time()

    current_walls = {}

    for side, levels in [("BUY", bids), ("SELL", asks)]:
        # -- Single walls --
        for price, qty in levels:
            if qty >= threshold:
                key = _wall_key(side, price)
                current_walls[key] = {
                    "type": "single",
                    "side": side,
                    "price": price,
                    "qty": qty,
                    "usdt_val": price * qty,
                    "levels_count": 1,
                }

        # -- Clustered walls --
        bins: dict = {}
        for price, qty in levels:
            bk = round(price / cluster_gap) * cluster_gap
            if bk not in bins:
                bins[bk] = {"total": 0.0, "levels": [], "max_single": 0.0}
            bins[bk]["total"] += qty
            bins[bk]["levels"].append((price, qty))
            bins[bk]["max_single"] = max(bins[bk]["max_single"], qty)

        for bk, bd in bins.items():
            if bd["total"] >= threshold and bd["max_single"] < threshold and len(bd["levels"]) > 1:
                avg_p = sum(p * q for p, q in bd["levels"]) / bd["total"]
                prices = [p for p, _ in bd["levels"]]
                key = f"C_{side}_{int(avg_p / 10) * 10}"
                current_walls[key] = {
                    "type": "cluster",
                    "side": side,
                    "price": round(avg_p, 2),
                    "qty": round(bd["total"], 4),
                    "usdt_val": avg_p * bd["total"],
                    "levels_count": len(bd["levels"]),
                    "price_range": f"${min(prices):,.0f}-${max(prices):,.0f}",
                }

    # -- NEW walls --
    for key, wall in current_walls.items():
        if key not in state["active_walls"]:
            last_cd = state["alert_cooldowns"].get(key, 0)
            if now - last_cd > cooldown:
                wall["first_seen"] = now
                wall["last_seen"] = now
                wall["original_qty"] = wall["qty"]
                wall["detection_price"] = current_price
                wall["missing_count"] = 0
                state["active_walls"][key] = wall
                state["alert_cooldowns"][key] = now

                tg_sent = send_wall_alert(wall, current_price)
                state["walls_detected_today"] += 1
                if wall["qty"] > state["ath_wall_qty"]:
                    state["ath_wall_qty"] = wall["qty"]
                    state["ath_wall_price"] = wall["price"]
                    state["ath_wall_side"] = wall["side"]

                wall["telegram_sent"] = tg_sent
                add_to_feed(wall, current_price, "detected")
                add_to_history(wall, current_price, "DETECTED")
        else:
            aw = state["active_walls"][key]
            aw["last_seen"] = now
            aw["qty"] = wall["qty"]
            aw["missing_count"] = 0

    # -- DISAPPEARED walls --
    gone = []
    for key, wall in state["active_walls"].items():
        if key not in current_walls:
            wall["missing_count"] = wall.get("missing_count", 0) + 1
            if wall["missing_count"] >= 3:
                is_eaten = False
                if wall["side"] == "BUY" and current_price <= wall["price"] * 1.001:
                    is_eaten = True
                elif wall["side"] == "SELL" and current_price >= wall["price"] * 0.999:
                    is_eaten = True

                if is_eaten:
                    evt = "eaten"
                    state["walls_eaten_today"] += 1
                else:
                    evt = "pulled"
                    state["walls_pulled_today"] += 1

                send_lifecycle_alert(wall, current_price, evt)
                add_to_history(wall, current_price, evt.upper())
                add_to_feed(wall, current_price, evt)
                gone.append(key)

    for k in gone:
        del state["active_walls"][k]


# ==============================================================================
# BACKGROUND SCANNING LOOP
# ==============================================================================


async def depth_scan_loop():
    await asyncio.sleep(2)
    print("[SCANNER] Depth scanner started.")
    while True:
        try:
            symbol = config.get("symbol", "BTCUSDT").upper()
            url = f"https://fapi.binance.com/fapi/v1/depth?symbol={symbol}&limit=1000"
            resp = await asyncio.to_thread(requests.get, url, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            bids_raw = data.get("bids", [])
            asks_raw = data.get("asks", [])

            if bids_raw and asks_raw:
                best_bid = float(bids_raw[0][0])
                best_ask = float(asks_raw[0][0])
                cp = round((best_bid + best_ask) / 2.0, 2)
                state["current_price"] = cp
                state["is_connected"] = True
                state["last_scan_ts"] = time.time()

                dist = float(config.get("distance_limit", 200.0))
                fb, fa = [], []
                tb, ta = 0.0, 0.0

                for ps, qs in bids_raw:
                    p, q = float(ps), float(qs)
                    if cp - p <= dist and p <= cp:
                        fb.append([p, q])
                        tb += q

                for ps, qs in asks_raw:
                    p, q = float(ps), float(qs)
                    if p - cp <= dist and p >= cp:
                        fa.append([p, q])
                        ta += q

                state["depth_bids"] = fb
                state["depth_asks"] = fa
                state["total_bid_depth"] = round(tb, 4)
                state["total_ask_depth"] = round(ta, 4)

                if config.get("bot_enabled", True):
                    detect_and_track_walls(fb, fa, cp)

        except Exception as e:
            state["is_connected"] = False
            print(f"[SCAN ERR] {e}")

        await asyncio.sleep(3)


@app.on_event("startup")
async def startup():
    asyncio.create_task(depth_scan_loop())


# ==============================================================================
# API ENDPOINTS
# ==============================================================================


def verify_pw(x_admin_password: Optional[str] = Header(None)):
    if ADMIN_PASSWORD and x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


@app.post("/api/verify-auth")
def api_verify_auth(data: AuthCheck):
    if ADMIN_PASSWORD and data.password != ADMIN_PASSWORD:
        return JSONResponse(status_code=401, content={"success": False, "message": "Hatali sifre!"})
    return {"success": True}


@app.get("/api/status")
def api_status():
    uptime = int(time.time() - state["start_time"])
    return {
        "symbol": config.get("symbol", "BTCUSDT"),
        "current_price": state["current_price"],
        "is_connected": state["is_connected"],
        "bot_enabled": config.get("bot_enabled", True),
        "distance_limit": config.get("distance_limit", 200.0),
        "volume_threshold": config.get("volume_threshold", 1000.0),
        "cluster_gap": config.get("cluster_gap", 10.0),
        "cooldown_seconds": config.get("cooldown_seconds", 180),
        "detected_walls_feed": state["detected_walls_feed"],
        "uptime_seconds": uptime,
        "walls_detected_today": state["walls_detected_today"],
        "walls_eaten_today": state["walls_eaten_today"],
        "walls_pulled_today": state["walls_pulled_today"],
        "ath_wall_qty": state["ath_wall_qty"],
        "ath_wall_price": state["ath_wall_price"],
        "ath_wall_side": state["ath_wall_side"],
        "total_alerts_sent": state["total_alerts_sent"],
        "total_bid_depth": state["total_bid_depth"],
        "total_ask_depth": state["total_ask_depth"],
        "active_walls_count": len(state["active_walls"]),
    }


@app.get("/api/depth")
def api_depth():
    active = [
        {"side": w["side"], "price": w["price"], "qty": w["qty"], "type": w.get("type", "single")}
        for w in state["active_walls"].values()
    ]
    return {
        "bids": state["depth_bids"],
        "asks": state["depth_asks"],
        "current_price": state["current_price"],
        "active_walls": active,
    }


@app.get("/api/wall-history")
def api_wall_history():
    return {"history": state["wall_history"]}


@app.get("/api/config")
def api_get_config(auth: bool = Depends(verify_pw)):
    return config


@app.post("/api/config")
def api_set_config(cfg: ConfigUpdate, auth: bool = Depends(verify_pw)):
    config["symbol"] = cfg.symbol.strip().upper()
    config["distance_limit"] = cfg.distance_limit
    config["volume_threshold"] = cfg.volume_threshold
    config["cluster_gap"] = cfg.cluster_gap
    config["cooldown_seconds"] = cfg.cooldown_seconds
    config["telegram_token"] = cfg.telegram_token.strip()
    config["chat_id"] = cfg.chat_id.strip()
    config["bot_enabled"] = cfg.bot_enabled
    save_config(config)
    return {"success": True, "message": "Ayarlar kaydedildi!"}


@app.post("/api/test-telegram")
def api_test_tg(auth: bool = Depends(verify_pw)):
    cp = state["current_price"] if state["current_price"] > 0 else 100000.0
    wall = {"side": "BUY", "type": "single", "price": cp - 80, "qty": 1250.0, "usdt_val": 0, "levels_count": 1}
    ok = send_wall_alert(wall, cp)
    return {"success": ok, "message": "Test mesaji gonderildi!" if ok else "Gonderilemedi. Token/ChatID kontrol edin."}


# ==============================================================================
# HTML DASHBOARD
# ==============================================================================

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Binance 1K BTC Wall Detector</title>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#070a0f;--card:rgba(14,19,32,0.8);--border:rgba(255,255,255,0.07);--blue:#3b82f6;--green:#10b981;--red:#ef4444;--amber:#f59e0b;--purple:#a855f7;--text:#f1f5f9;--muted:#64748b;--dim:rgba(255,255,255,0.04)}
*{box-sizing:border-box;margin:0;padding:0;font-family:'Plus Jakarta Sans',-apple-system,sans-serif}
body{background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden}
body::before{content:'';position:fixed;top:0;left:0;width:100%;height:100%;background:radial-gradient(ellipse at 10% 0%,rgba(59,130,246,0.08),transparent 50%),radial-gradient(ellipse at 90% 100%,rgba(16,185,129,0.06),transparent 50%);pointer-events:none;z-index:0}
.wrap{max-width:1400px;margin:0 auto;padding:16px;position:relative;z-index:1}

/* Header */
.hdr{display:flex;justify-content:space-between;align-items:center;padding:16px 24px;background:var(--card);border:1px solid var(--border);border-radius:16px;margin-bottom:16px;backdrop-filter:blur(12px)}
.hdr-left{display:flex;align-items:center;gap:12px}
.logo{width:40px;height:40px;background:linear-gradient(135deg,#3b82f6,#1d4ed8);border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:20px;box-shadow:0 0 20px rgba(59,130,246,0.3)}
.hdr h1{font-size:18px;font-weight:700;letter-spacing:-0.5px}
.hdr-sub{font-size:12px;color:var(--muted)}
.hdr-right{display:flex;align-items:center;gap:10px}
.pill{display:flex;align-items:center;gap:6px;padding:6px 14px;border-radius:20px;font-size:12px;font-weight:600;background:rgba(255,255,255,0.04);border:1px solid var(--border)}
.dot{width:7px;height:7px;border-radius:50%}
.dot-g{background:var(--green);box-shadow:0 0 8px var(--green)}
.dot-r{background:var(--red);box-shadow:0 0 8px var(--red)}
.dot-a{background:var(--amber);box-shadow:0 0 8px var(--amber)}
.hdr-btn{background:none;border:1px solid var(--border);color:var(--muted);padding:6px 12px;border-radius:10px;cursor:pointer;font-size:13px;transition:.2s}
.hdr-btn:hover{background:rgba(255,255,255,0.06);color:#fff}

/* Metrics */
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:16px}
.mc{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px 18px;backdrop-filter:blur(12px)}
.mc-label{font-size:11px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px}
.mc-val{font-size:22px;font-weight:800;letter-spacing:-0.5px}
.mc-sub{font-size:11px;color:var(--muted);margin-top:4px}
.mc-val.green{color:var(--green)}.mc-val.red{color:var(--red)}.mc-val.amber{color:var(--amber)}.mc-val.blue{color:var(--blue)}

/* Depth Chart */
.depth-card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:16px;margin-bottom:16px;backdrop-filter:blur(12px)}
.depth-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.depth-header h2{font-size:15px;font-weight:700}
#depthCanvas{width:100%;border-radius:10px;cursor:crosshair}

/* Tabs */
.tabs{display:flex;gap:4px;margin-bottom:16px;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:4px;backdrop-filter:blur(12px)}
.tab{flex:1;padding:10px;text-align:center;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;transition:.2s;color:var(--muted)}
.tab.active{background:rgba(59,130,246,0.15);color:var(--blue)}
.tab:hover:not(.active){background:var(--dim);color:var(--text)}
.tab-pane{display:none}.tab-pane.active{display:block}

/* Grid */
.grid2{display:grid;grid-template-columns:1fr 380px;gap:16px}
@media(max-width:1024px){.grid2{grid-template-columns:1fr}}

/* Table */
.card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:16px;backdrop-filter:blur(12px)}
.card-h{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid var(--border)}
.card-h h2{font-size:15px;font-weight:700}
.tbl-wrap{overflow-x:auto;max-height:420px;overflow-y:auto}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:10px 12px;color:var(--muted);border-bottom:1px solid var(--border);font-weight:600;position:sticky;top:0;background:rgba(14,19,32,0.95);z-index:1}
td{padding:10px 12px;border-bottom:1px solid var(--dim)}
.bg-buy{color:var(--green);background:rgba(16,185,129,0.1);padding:3px 8px;border-radius:12px;font-weight:700;font-size:11px;display:inline-block}
.bg-sell{color:var(--red);background:rgba(239,68,68,0.1);padding:3px 8px;border-radius:12px;font-weight:700;font-size:11px;display:inline-block}
.bg-eaten{color:var(--amber);background:rgba(245,158,11,0.1);padding:3px 8px;border-radius:12px;font-weight:700;font-size:11px}
.bg-pulled{color:var(--purple);background:rgba(168,85,247,0.1);padding:3px 8px;border-radius:12px;font-weight:700;font-size:11px}
.bg-detected{color:var(--blue);background:rgba(59,130,246,0.1);padding:3px 8px;border-radius:12px;font-weight:700;font-size:11px}

/* Timeline */
.tl{position:relative;padding-left:22px;max-height:500px;overflow-y:auto}
.tl::before{content:'';position:absolute;left:7px;top:0;bottom:0;width:2px;background:rgba(255,255,255,0.06);border-radius:2px}
.tl-item{position:relative;padding-bottom:18px;animation:fadeSlideIn .3s ease}
@keyframes fadeSlideIn{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:translateY(0)}}
.tl-dot{width:14px;height:14px;border-radius:50%;position:absolute;left:-22px;top:2px;z-index:1;border:2px solid var(--bg)}
.tl-dot.det{background:var(--green);box-shadow:0 0 6px var(--green)}
.tl-dot.eat{background:var(--amber);box-shadow:0 0 6px var(--amber)}
.tl-dot.pull{background:var(--purple);box-shadow:0 0 6px var(--purple)}
.tl-time{font-size:11px;color:var(--muted);font-weight:600;margin-bottom:3px}
.tl-title{font-size:13px;font-weight:700;margin-bottom:2px}
.tl-desc{font-size:12px;color:var(--muted)}

/* Settings */
.form-group{margin-bottom:14px}
.form-group label{display:block;font-size:12px;font-weight:600;color:#94a3b8;margin-bottom:6px}
.fc{width:100%;padding:10px 14px;background:rgba(7,10,15,0.7);border:1px solid rgba(255,255,255,0.1);border-radius:10px;color:#fff;font-size:13px;outline:none;transition:.2s}
.fc:focus{border-color:var(--blue);box-shadow:0 0 0 2px rgba(59,130,246,0.2)}
.btn{display:flex;align-items:center;justify-content:center;gap:6px;width:100%;padding:12px;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;border:none;transition:.2s}
.btn-p{background:linear-gradient(135deg,#3b82f6,#2563eb);color:#fff;box-shadow:0 4px 12px rgba(59,130,246,0.25)}
.btn-p:hover{opacity:.9;transform:translateY(-1px)}
.btn-s{background:rgba(255,255,255,0.06);color:#fff;margin-top:8px}
.btn-s:hover{background:rgba(255,255,255,0.1)}

/* Toggle */
.tog{display:flex;align-items:center;justify-content:space-between;padding:12px;background:rgba(7,10,15,0.5);border-radius:10px;border:1px solid var(--border);margin-bottom:14px}
.sw{position:relative;width:44px;height:24px;display:inline-block}
.sw input{opacity:0;width:0;height:0}
.sl{position:absolute;cursor:pointer;inset:0;background:#374151;transition:.3s;border-radius:24px}
.sl::before{content:'';position:absolute;height:16px;width:16px;left:4px;bottom:4px;background:#fff;transition:.3s;border-radius:50%}
input:checked+.sl{background:var(--green)}
input:checked+.sl::before{transform:translateX(20px)}

/* Toast */
.toast{padding:10px 14px;border-radius:8px;font-size:12px;margin-bottom:12px;display:none;font-weight:600}
.toast.ok{background:rgba(16,185,129,0.15);color:var(--green);border:1px solid rgba(16,185,129,0.3)}
.toast.err{background:rgba(239,68,68,0.15);color:var(--red);border:1px solid rgba(239,68,68,0.3)}

/* Modal */
.modal{position:fixed;inset:0;background:rgba(0,0,0,0.85);backdrop-filter:blur(10px);display:flex;align-items:center;justify-content:center;z-index:999}
.modal-box{background:#0f1320;border:1px solid var(--border);border-radius:20px;padding:32px;width:100%;max-width:380px;text-align:center;box-shadow:0 20px 40px rgba(0,0,0,0.5)}
.modal-box h3{font-size:18px;margin-bottom:6px}
.modal-box p{font-size:12px;color:var(--muted);margin-bottom:20px}

.sound-toggle{position:fixed;bottom:20px;right:20px;z-index:50;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:8px 14px;cursor:pointer;font-size:12px;font-weight:600;color:var(--muted);backdrop-filter:blur(12px);transition:.2s}
.sound-toggle:hover{color:#fff;background:rgba(255,255,255,0.08)}
</style>
</head>
<body>

<div id="authModal" class="modal">
<div class="modal-box">
<div style="font-size:36px;margin-bottom:10px">&#x1f512;</div>
<h3>Yonetici Girisi</h3>
<p>ADMIN_PASSWORD sifrenizi girin</p>
<div id="authErr" class="toast err"></div>
<form onsubmit="doLogin(event)">
<div class="form-group"><input type="password" id="authPwd" class="fc" placeholder="Sifre" required autofocus></div>
<button type="submit" class="btn btn-p">Giris Yap</button>
</form>
</div>
</div>

<div class="wrap">

<div class="hdr">
<div class="hdr-left">
<div class="logo">&#x1f9f1;</div>
<div><h1>Binance 1K BTC Wall Detector</h1><div class="hdr-sub">Orderbook Depth Monitor & Telegram Alert System</div></div>
</div>
<div class="hdr-right">
<div class="pill"><div id="wsDot" class="dot dot-r"></div><span id="wsLabel">Baglaniyor...</span></div>
<div class="pill"><div id="botDot" class="dot dot-g"></div><span id="botLabel">Bot Aktif</span></div>
<div class="pill" id="uptimePill">Uptime: --</div>
<button class="hdr-btn" onclick="openLogin()">&#x1f513;</button>
</div>
</div>

<div class="metrics">
<div class="mc"><div class="mc-label">Anlik Fiyat (<span id="symLbl">BTCUSDT</span>)</div><div class="mc-val" id="mPrice">$0.00</div><div class="mc-sub" id="mPriceSub">--</div></div>
<div class="mc"><div class="mc-label">Tespit / Yenilen / Cekilen</div><div class="mc-val" id="mCounts">0 / 0 / 0</div><div class="mc-sub">Bugunun duvar istatistikleri</div></div>
<div class="mc"><div class="mc-label">ATH Duvar</div><div class="mc-val amber" id="mAth">--</div><div class="mc-sub" id="mAthSub">En buyuk tespit</div></div>
<div class="mc"><div class="mc-label">Bid Derinligi</div><div class="mc-val green" id="mBidD">0 BTC</div><div class="mc-sub">Toplam alim emri</div></div>
<div class="mc"><div class="mc-label">Ask Derinligi</div><div class="mc-val red" id="mAskD">0 BTC</div><div class="mc-sub">Toplam satim emri</div></div>
<div class="mc"><div class="mc-label">Aktif Duvarlar</div><div class="mc-val blue" id="mActive">0</div><div class="mc-sub">Su an takip edilen</div></div>
</div>

<div class="depth-card">
<div class="depth-header"><h2>&#x1f4ca; Canli Derinlik Grafigi</h2><span style="font-size:12px;color:var(--muted)" id="depthInfo">--</span></div>
<canvas id="depthCanvas" height="280"></canvas>
</div>

<div class="tabs">
<div class="tab active" onclick="switchTab(0)">&#x1f4cb; Canli Duvar Akisi</div>
<div class="tab" onclick="switchTab(1)">&#x23f3; Duvar Gecmisi</div>
<div class="tab" onclick="switchTab(2)">&#x2699;&#xfe0f; Ayarlar</div>
</div>

<div id="tabContent">
<div class="tab-pane active" id="pane0">
<div class="card">
<div class="card-h"><h2>Tespit Edilen Duvarlar</h2><span style="font-size:12px;color:var(--muted)" id="feedCnt">0</span></div>
<div class="tbl-wrap">
<table><thead><tr><th>Saat</th><th>Olay</th><th>Taraf</th><th>Tip</th><th>Fiyat</th><th>Miktar</th><th>USDT</th><th>Mesafe</th></tr></thead>
<tbody id="feedBody"><tr><td colspan="8" style="text-align:center;color:var(--muted);padding:30px">Tahta canli izleniyor...</td></tr></tbody>
</table>
</div>
</div>
</div>

<div class="tab-pane" id="pane1">
<div class="grid2">
<div class="card">
<div class="card-h"><h2>&#x23f3; Duvar Yasam Dongusu</h2></div>
<div class="tl" id="timeline"><div style="text-align:center;color:var(--muted);padding:20px">Henuz duvar olayı yok...</div></div>
</div>
<div class="card">
<div class="card-h"><h2>&#x1f4ca; Istatistikler</h2></div>
<div id="statsContent" style="font-size:13px;color:var(--muted)">Yukleniyor...</div>
</div>
</div>
</div>

<div class="tab-pane" id="pane2">
<div class="card" style="max-width:600px">
<div class="card-h"><h2>&#x2699;&#xfe0f; Bot Ayarlari</h2></div>
<div id="cfgToast" class="toast"></div>
<form id="cfgForm" onsubmit="saveCfg(event)">
<div class="tog"><div><div style="font-weight:700;font-size:13px">Bot Durumu</div><div style="font-size:11px;color:var(--muted)">Bildirim gonder / durdur</div></div><label class="sw"><input type="checkbox" id="c_enabled" checked><span class="sl"></span></label></div>
<div class="form-group"><label>Sembol</label><input type="text" id="c_sym" class="fc" value="BTCUSDT"></div>
<div class="form-group"><label>Fiyata Maks. Uzaklik (USDT)</label><input type="number" step="1" id="c_dist" class="fc" value="200"></div>
<div class="form-group"><label>Min. Duvar Buyuklugu (BTC)</label><input type="number" step="10" id="c_vol" class="fc" value="1000"></div>
<div class="form-group"><label>Cluster Gap - Kumeleme Araligi (USDT)</label><input type="number" step="1" id="c_gap" class="fc" value="10"></div>
<div class="form-group"><label>Cooldown (Saniye)</label><input type="number" step="10" id="c_cd" class="fc" value="180"></div>
<div class="form-group"><label>Telegram Bot Token</label><input type="text" id="c_tok" class="fc" placeholder="123456789:ABC..."></div>
<div class="form-group"><label>Telegram Chat ID</label><input type="text" id="c_cid" class="fc" placeholder="12345678"></div>
<button type="submit" class="btn btn-p">&#x1f4be; Ayarlari Kaydet</button>
<button type="button" class="btn btn-s" onclick="testTg()">&#x1f9ea; Telegram Test Gonder</button>
</form>
</div>
</div>
</div>

</div>

<div class="sound-toggle" id="soundBtn" onclick="toggleSound()">&#x1f50a; Ses: Acik</div>

<script>
let pwd=localStorage.getItem('ap')||'';
let soundOn=localStorage.getItem('sound')!=='off';
let lastWallCount=0;
let lastDepthData=null;
let audioCtx=null;

function openLogin(){document.getElementById('authModal').style.display='flex';document.getElementById('authPwd').focus()}
async function doLogin(e){
 e.preventDefault();
 const p=document.getElementById('authPwd').value.trim();
 const r=await fetch('/api/verify-auth',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:p})});
 if(r.ok){pwd=p;localStorage.setItem('ap',p);document.getElementById('authModal').style.display='none';loadCfg()}
 else{const el=document.getElementById('authErr');el.textContent='Hatali sifre!';el.style.display='block'}
}

async function loadCfg(){
 if(!pwd){openLogin();return}
 const r=await fetch('/api/config',{headers:{'X-Admin-Password':pwd}});
 if(r.status===401){openLogin();return}
 const d=await r.json();
 document.getElementById('c_sym').value=d.symbol||'BTCUSDT';
 document.getElementById('c_dist').value=d.distance_limit||200;
 document.getElementById('c_vol').value=d.volume_threshold||1000;
 document.getElementById('c_gap').value=d.cluster_gap||10;
 document.getElementById('c_cd').value=d.cooldown_seconds||180;
 document.getElementById('c_tok').value=d.telegram_token||'';
 document.getElementById('c_cid').value=d.chat_id||'';
 document.getElementById('c_enabled').checked=d.bot_enabled!==false;
}

async function saveCfg(e){
 e.preventDefault();const t=document.getElementById('cfgToast');t.style.display='none';
 const body={symbol:document.getElementById('c_sym').value.trim(),distance_limit:+document.getElementById('c_dist').value,volume_threshold:+document.getElementById('c_vol').value,cluster_gap:+document.getElementById('c_gap').value,cooldown_seconds:+document.getElementById('c_cd').value,telegram_token:document.getElementById('c_tok').value.trim(),chat_id:document.getElementById('c_cid').value.trim(),bot_enabled:document.getElementById('c_enabled').checked};
 const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json','X-Admin-Password':pwd},body:JSON.stringify(body)});
 const d=await r.json();
 t.className=r.ok?'toast ok':'toast err';t.textContent=d.message||'Hata!';t.style.display='block';
}

async function testTg(){
 const t=document.getElementById('cfgToast');t.className='toast';t.textContent='Gonderiliyor...';t.style.display='block';
 const r=await fetch('/api/test-telegram',{method:'POST',headers:{'X-Admin-Password':pwd}});
 const d=await r.json();t.className=d.success?'toast ok':'toast err';t.textContent=d.message;
}

function switchTab(i){
 document.querySelectorAll('.tab').forEach((t,j)=>{t.classList.toggle('active',j===i)});
 document.querySelectorAll('.tab-pane').forEach((p,j)=>{p.classList.toggle('active',j===i)});
}

function toggleSound(){
 soundOn=!soundOn;localStorage.setItem('sound',soundOn?'on':'off');
 document.getElementById('soundBtn').innerHTML=soundOn?'&#x1f50a; Ses: Acik':'&#x1f507; Ses: Kapali';
}

function playBeep(){
 if(!soundOn)return;
 try{
  if(!audioCtx)audioCtx=new(window.AudioContext||window.webkitAudioContext)();
  const o=audioCtx.createOscillator(),g=audioCtx.createGain();
  o.connect(g);g.connect(audioCtx.destination);
  o.type='sine';o.frequency.setValueAtTime(880,audioCtx.currentTime);
  g.gain.setValueAtTime(0.25,audioCtx.currentTime);
  g.gain.exponentialRampToValueAtTime(0.01,audioCtx.currentTime+0.4);
  o.start();o.stop(audioCtx.currentTime+0.4);
 }catch(e){}
}

function notifyBrowser(title,body){
 if('Notification' in window&&Notification.permission==='granted'){
  try{new Notification(title,{body,icon:'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">&#x1f9f1;</text></svg>'})}catch(e){}
 }
}

function fmtUptime(s){const h=Math.floor(s/3600),m=Math.floor(s%3600/60),sec=s%60;return(h>0?h+'s ':'')+(m>0?m+'dk ':'')+sec+'sn'}
function fmtNum(n){return n.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}

// === DEPTH CHART ===
function drawDepth(data){
 const canvas=document.getElementById('depthCanvas');
 const ctx=canvas.getContext('2d');
 canvas.width=canvas.parentElement.clientWidth-32;
 canvas.height=280;
 const W=canvas.width,H=canvas.height;
 const pad={t:20,r:10,b:30,l:10};
 const cW=W-pad.l-pad.r,cH=H-pad.t-pad.b;

 ctx.fillStyle='#080b12';ctx.fillRect(0,0,W,H);

 const bids=data.bids||[];const asks=data.asks||[];
 const cp=data.current_price;const walls=data.active_walls||[];

 if(!bids.length&&!asks.length){
  ctx.fillStyle='#64748b';ctx.font='13px sans-serif';ctx.textAlign='center';
  ctx.fillText('Derinlik verisi bekleniyor...',W/2,H/2);return;
 }

 // cumulative
 let cumB=[],cumA=[],cq=0;
 for(const[p,q]of bids){cq+=q;cumB.push({p,cq})}
 cq=0;for(const[p,q]of asks){cq+=q;cumA.push({p,cq})}

 const allP=[...bids.map(b=>b[0]),...asks.map(a=>a[0])];
 const mnP=Math.min(...allP),mxP=Math.max(...allP);
 const mxQ=Math.max(cumB.length?cumB[cumB.length-1].cq:1,cumA.length?cumA[cumA.length-1].cq:1)||1;
 const pRange=mxP-mnP||1;

 const x=p=>pad.l+((p-mnP)/pRange)*cW;
 const y=q=>pad.t+cH-(q/mxQ)*cH;

 // Grid
 ctx.strokeStyle='rgba(255,255,255,0.04)';ctx.lineWidth=1;
 for(let i=0;i<=4;i++){
  const gy=pad.t+(cH/4)*i;
  ctx.beginPath();ctx.moveTo(pad.l,gy);ctx.lineTo(W-pad.r,gy);ctx.stroke();
  ctx.fillStyle='rgba(255,255,255,0.2)';ctx.font='9px monospace';ctx.textAlign='left';
  ctx.fillText(((4-i)/4*mxQ).toFixed(1),pad.l+4,gy-2);
 }

 // Bids area
 if(cumB.length){
  ctx.beginPath();ctx.moveTo(x(cumB[0].p),y(0));
  for(let i=0;i<cumB.length;i++){
   ctx.lineTo(x(cumB[i].p),y(cumB[i].cq));
   if(i<cumB.length-1)ctx.lineTo(x(cumB[i+1].p),y(cumB[i].cq));
  }
  ctx.lineTo(x(cumB[cumB.length-1].p),y(0));ctx.closePath();
  const g1=ctx.createLinearGradient(0,pad.t,0,pad.t+cH);
  g1.addColorStop(0,'rgba(16,185,129,0.45)');g1.addColorStop(1,'rgba(16,185,129,0.02)');
  ctx.fillStyle=g1;ctx.fill();
  ctx.strokeStyle='rgba(16,185,129,0.7)';ctx.lineWidth=1.5;
  ctx.beginPath();ctx.moveTo(x(cumB[0].p),y(cumB[0].cq));
  for(let i=1;i<cumB.length;i++){ctx.lineTo(x(cumB[i].p),y(cumB[i-1].cq));ctx.lineTo(x(cumB[i].p),y(cumB[i].cq))}
  ctx.stroke();
 }

 // Asks area
 if(cumA.length){
  ctx.beginPath();ctx.moveTo(x(cumA[0].p),y(0));
  for(let i=0;i<cumA.length;i++){
   ctx.lineTo(x(cumA[i].p),y(cumA[i].cq));
   if(i<cumA.length-1)ctx.lineTo(x(cumA[i+1].p),y(cumA[i].cq));
  }
  ctx.lineTo(x(cumA[cumA.length-1].p),y(0));ctx.closePath();
  const g2=ctx.createLinearGradient(0,pad.t,0,pad.t+cH);
  g2.addColorStop(0,'rgba(239,68,68,0.4)');g2.addColorStop(1,'rgba(239,68,68,0.02)');
  ctx.fillStyle=g2;ctx.fill();
  ctx.strokeStyle='rgba(239,68,68,0.65)';ctx.lineWidth=1.5;
  ctx.beginPath();ctx.moveTo(x(cumA[0].p),y(cumA[0].cq));
  for(let i=1;i<cumA.length;i++){ctx.lineTo(x(cumA[i].p),y(cumA[i-1].cq));ctx.lineTo(x(cumA[i].p),y(cumA[i].cq))}
  ctx.stroke();
 }

 // Wall markers
 for(const w of walls){
  const wx=x(w.price);
  const clr=w.side==='BUY'?'rgba(16,185,129,0.9)':'rgba(239,68,68,0.9)';
  const glow=w.side==='BUY'?'rgba(16,185,129,0.2)':'rgba(239,68,68,0.2)';
  ctx.fillStyle=glow;ctx.fillRect(wx-8,pad.t,16,cH);
  ctx.fillStyle=clr;ctx.fillRect(wx-1.5,pad.t,3,cH);
  ctx.save();ctx.font='bold 10px sans-serif';ctx.fillStyle='#fff';ctx.textAlign='center';
  ctx.shadowColor=clr;ctx.shadowBlur=6;
  ctx.fillText(w.qty.toFixed(0)+' BTC',wx,pad.t+14);
  ctx.restore();
 }

 // Current price line
 const cpx=x(cp);
 ctx.strokeStyle='rgba(255,255,255,0.35)';ctx.setLineDash([4,4]);ctx.lineWidth=1;
 ctx.beginPath();ctx.moveTo(cpx,pad.t);ctx.lineTo(cpx,pad.t+cH);ctx.stroke();ctx.setLineDash([]);
 ctx.fillStyle='rgba(255,255,255,0.8)';ctx.font='bold 11px sans-serif';ctx.textAlign='center';
 ctx.fillText('$'+cp.toLocaleString(),cpx,pad.t+cH+14);

 // Edge labels
 ctx.fillStyle='rgba(255,255,255,0.3)';ctx.font='10px monospace';
 ctx.textAlign='left';ctx.fillText('$'+mnP.toLocaleString(),pad.l,pad.t+cH+14);
 ctx.textAlign='right';ctx.fillText('$'+mxP.toLocaleString(),W-pad.r,pad.t+cH+14);
}

// Hover crosshair
const depthCanvas=document.getElementById('depthCanvas');
depthCanvas.addEventListener('mousemove',function(e){
 if(!lastDepthData)return;
 drawDepth(lastDepthData);
 const rect=depthCanvas.getBoundingClientRect();
 const mx=e.clientX-rect.left,my=e.clientY-rect.top;
 const ctx=depthCanvas.getContext('2d');
 const W=depthCanvas.width,H=depthCanvas.height;
 const pad={t:20,r:10,b:30,l:10};
 const cW=W-pad.l-pad.r,cH=H-pad.t-pad.b;
 if(mx<pad.l||mx>W-pad.r||my<pad.t||my>pad.t+cH)return;

 ctx.strokeStyle='rgba(255,255,255,0.15)';ctx.setLineDash([3,3]);ctx.lineWidth=1;
 ctx.beginPath();ctx.moveTo(mx,pad.t);ctx.lineTo(mx,pad.t+cH);ctx.stroke();
 ctx.beginPath();ctx.moveTo(pad.l,my);ctx.lineTo(W-pad.r,my);ctx.stroke();
 ctx.setLineDash([]);

 const bids=lastDepthData.bids||[];const asks=lastDepthData.asks||[];
 const allP=[...bids.map(b=>b[0]),...asks.map(a=>a[0])];
 if(!allP.length)return;
 const mnP=Math.min(...allP),mxP=Math.max(...allP);
 const pRange=mxP-mnP||1;
 const hoverP=mnP+((mx-pad.l)/cW)*pRange;

 ctx.fillStyle='rgba(0,0,0,0.75)';
 const bw=130,bh=22,bx=Math.min(mx+12,W-bw-10),by=Math.max(my-28,pad.t);
 ctx.beginPath();ctx.roundRect(bx,by,bw,bh,4);ctx.fill();
 ctx.fillStyle='#fff';ctx.font='11px monospace';ctx.textAlign='left';
 ctx.fillText('$'+hoverP.toFixed(2),bx+6,by+15);
});
depthCanvas.addEventListener('mouseleave',function(){if(lastDepthData)drawDepth(lastDepthData)});
window.addEventListener('resize',function(){if(lastDepthData)drawDepth(lastDepthData)});

// === MAIN UPDATE LOOPS ===
async function updateStatus(){
 try{
  const r=await fetch('/api/status');if(!r.ok)return;const d=await r.json();

  document.getElementById('symLbl').textContent=d.symbol;
  document.getElementById('mPrice').textContent='$'+fmtNum(d.current_price);
  document.getElementById('mPriceSub').textContent='Son: '+new Date().toLocaleTimeString();
  document.getElementById('mCounts').textContent=d.walls_detected_today+' / '+d.walls_eaten_today+' / '+d.walls_pulled_today;
  document.getElementById('mBidD').textContent=d.total_bid_depth.toFixed(2)+' BTC';
  document.getElementById('mAskD').textContent=d.total_ask_depth.toFixed(2)+' BTC';
  document.getElementById('mActive').textContent=d.active_walls_count;
  document.getElementById('uptimePill').textContent='Uptime: '+fmtUptime(d.uptime_seconds);

  if(d.ath_wall_qty>0){
   document.getElementById('mAth').textContent=d.ath_wall_qty.toLocaleString()+' BTC';
   document.getElementById('mAthSub').textContent='$'+fmtNum(d.ath_wall_price)+' ('+d.ath_wall_side+')';
  }

  const wd=document.getElementById('wsDot'),wl=document.getElementById('wsLabel');
  if(d.is_connected){wd.className='dot dot-g';wl.textContent='Canli Bagli'}
  else{wd.className='dot dot-r';wl.textContent='Baglanti Kesildi'}

  const bd=document.getElementById('botDot'),bl=document.getElementById('botLabel');
  if(d.bot_enabled){bd.className='dot dot-g';bl.textContent='Bot Aktif'}
  else{bd.className='dot dot-r';bl.textContent='Bot Durduruldu'}

  // Feed table
  const feed=d.detected_walls_feed||[];
  document.getElementById('feedCnt').textContent=feed.length+' kayit';
  const tb=document.getElementById('feedBody');
  if(!feed.length){
   tb.innerHTML='<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:30px">Tahta canli izleniyor...</td></tr>';
  }else{
   tb.innerHTML=feed.map(w=>{
    const evtCls=w.event==='detected'?'bg-detected':w.event==='eaten'?'bg-eaten':'bg-pulled';
    const evtTxt=w.event==='detected'?'TESPIT':w.event==='eaten'?'YENILDI':'CEKILDI';
    const sideCls=w.side==='BUY'?'bg-buy':'bg-sell';
    const sideTxt=w.side==='BUY'?'ALIM':'SATIM';
    const typeTxt=w.type==='cluster'?'Cluster('+w.levels_count+')':'Tekil';
    return '<tr><td><code>'+w.ts+'</code></td><td><span class="'+evtCls+'">'+evtTxt+'</span></td><td><span class="'+sideCls+'">'+sideTxt+'</span></td><td>'+typeTxt+'</td><td><b>$'+fmtNum(w.price)+'</b></td><td><b>'+fmtNum(w.qty)+' BTC</b></td><td>$'+(w.usdt_val/1e6).toFixed(2)+'M</td><td>$'+w.distance.toFixed(2)+'</td></tr>';
   }).join('');
  }

  // New wall notification
  const newCount=d.walls_detected_today+d.walls_eaten_today+d.walls_pulled_today;
  if(newCount>lastWallCount&&lastWallCount>0){
   playBeep();
   if(feed.length>0){
    const latest=feed[0];
    const evtLabel=latest.event==='detected'?'DUVAR TESPIT':latest.event==='eaten'?'DUVAR YENILDI':'DUVAR CEKILDI';
    notifyBrowser(evtLabel,latest.side+' $'+fmtNum(latest.price)+' - '+fmtNum(latest.qty)+' BTC');
   }
  }
  lastWallCount=newCount;

 }catch(e){console.error(e)}
}

async function updateDepth(){
 try{
  const r=await fetch('/api/depth');if(!r.ok)return;
  const d=await r.json();lastDepthData=d;
  drawDepth(d);
  const info=d.bids.length+' bid + '+d.asks.length+' ask seviyesi';
  document.getElementById('depthInfo').textContent=info;
 }catch(e){}
}

async function updateTimeline(){
 try{
  const r=await fetch('/api/wall-history');if(!r.ok)return;
  const d=await r.json();const h=d.history||[];
  const el=document.getElementById('timeline');
  if(!h.length){el.innerHTML='<div style="text-align:center;color:var(--muted);padding:20px">Henuz duvar olayi yok...</div>';return}
  el.innerHTML=h.slice(0,30).map(w=>{
   let dotCls='det',icon='&#x1f7e2;',label='TESPIT EDILDI';
   if(w.event==='EATEN'){dotCls='eat';icon='&#x1f525;';label='YENILDI'}
   else if(w.event==='PULLED'){dotCls='pull';icon='&#x1f47b;';label='CEKILDI (SPOOF?)'}
   const sideTxt=w.side==='BUY'?'ALIM':'SATIM';
   const ltStr=w.lifetime_seconds>0?(w.lifetime_seconds>=60?Math.floor(w.lifetime_seconds/60)+'dk '+(w.lifetime_seconds%60)+'sn':w.lifetime_seconds+'sn'):'--';
   return '<div class="tl-item"><div class="tl-dot '+dotCls+'"></div><div><div class="tl-time">'+w.ts+'</div><div class="tl-title">'+icon+' '+sideTxt+' DUVARI '+label+'</div><div class="tl-desc">$'+fmtNum(w.price)+' | '+fmtNum(w.qty)+' BTC'+(w.type==='cluster'?' (Cluster '+w.levels_count+' seviye)':'')+(w.event!=='DETECTED'?' | Sure: '+ltStr:'')+'</div></div></div>';
  }).join('');

  // Stats
  const detected=h.filter(x=>x.event==='DETECTED').length;
  const eaten=h.filter(x=>x.event==='EATEN').length;
  const pulled=h.filter(x=>x.event==='PULLED').length;
  const avgQty=h.length?h.reduce((a,x)=>a+x.qty,0)/h.length:0;
  const maxW=h.reduce((a,x)=>x.qty>a.qty?x:a,{qty:0});
  let statsHtml='<div style="display:grid;gap:12px">';
  statsHtml+='<div><b>Toplam Olay:</b> '+h.length+'</div>';
  statsHtml+='<div><b style="color:var(--green)">Tespit:</b> '+detected+' | <b style="color:var(--amber)">Yenilen:</b> '+eaten+' | <b style="color:var(--purple)">Cekilen:</b> '+pulled+'</div>';
  if(eaten+pulled>0){
   const spoofRate=pulled/(eaten+pulled)*100;
   statsHtml+='<div><b>Spoof Orani:</b> %'+spoofRate.toFixed(1)+'</div>';
  }
  statsHtml+='<div><b>Ort. Duvar:</b> '+avgQty.toFixed(0)+' BTC</div>';
  if(maxW.qty>0)statsHtml+='<div><b>Max Duvar:</b> '+fmtNum(maxW.qty)+' BTC ($'+fmtNum(maxW.price)+')</div>';
  if(eaten>0){
   const eatenWalls=h.filter(x=>x.event==='EATEN'&&x.lifetime_seconds>0);
   if(eatenWalls.length){const avgLife=eatenWalls.reduce((a,x)=>a+x.lifetime_seconds,0)/eatenWalls.length;statsHtml+='<div><b>Ort. Yenilme Suresi:</b> '+Math.floor(avgLife)+'sn</div>'}
  }
  statsHtml+='</div>';
  document.getElementById('statsContent').innerHTML=statsHtml;

 }catch(e){}
}

// Init
if(pwd){document.getElementById('authModal').style.display='none';loadCfg()}
else{openLogin()}

if('Notification' in window&&Notification.permission==='default'){Notification.requestPermission()}

updateStatus();updateDepth();updateTimeline();
setInterval(updateStatus,1500);
setInterval(updateDepth,2500);
setInterval(updateTimeline,5000);
document.getElementById('soundBtn').innerHTML=soundOn?'&#x1f50a; Ses: Acik':'&#x1f507; Ses: Kapali';
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    return DASHBOARD_HTML
