import os
import sys
import time
import json
import asyncio
import requests
import websockets
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Header, Depends, Body
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── CONFIGURATION & STATE ─────────────────────────────────────────────────────

CONFIG_FILE = "config.json"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

# Default settings
DEFAULT_CONFIG = {
    "symbol": "BTCUSDT",
    "distance_limit": 200.0,
    "volume_threshold": 1000.0,
    "cooldown_seconds": 180,
    "telegram_token": os.environ.get("TELEGRAM_TOKEN", ""),
    "chat_id": os.environ.get("CHAT_ID", ""),
    "bot_enabled": True
}

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                merged = DEFAULT_CONFIG.copy()
                merged.update(data)
                return merged
        except Exception as e:
            print(f"[CONFIG] Error loading {CONFIG_FILE}: {e}")
    return DEFAULT_CONFIG.copy()

def save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        print("[CONFIG] Saved settings to config.json")
    except Exception as e:
        print(f"[CONFIG] Error saving {CONFIG_FILE}: {e}")

bot_config = load_config()

# Live Bot State
bot_state = {
    "current_price": 0.0,
    "last_update_ts": 0,
    "is_connected": False,
    "detected_walls": [],  # List of dicts: {id, side, price, qty, usdt_val, distance, ts, telegram_sent}
    "last_alerts": {}      # {(side, price_rounded): timestamp}
}

app = FastAPI(title="Binance Futures Wall Alert Bot")

# ── MODELS ───────────────────────────────────────────────────────────────────

class ConfigModel(BaseModel):
    symbol: str
    distance_limit: float
    volume_threshold: float
    cooldown_seconds: int
    telegram_token: str
    chat_id: str
    bot_enabled: bool

class AuthCheckModel(BaseModel):
    password: str

# ── TELEGRAM ALERT HELPER ────────────────────────────────────────────────────

def send_telegram_alert(symbol: str, wall_side: str, wall_price: float, wall_qty: float, current_price: float, total_range_qty: float = 0.0) -> bool:
    token = bot_config.get("telegram_token", "").strip()
    chat_id = bot_config.get("chat_id", "").strip()
    
    if not token or not chat_id:
        print(f"[ALERT] Telegram token/chat_id missing. Alert not sent to Telegram. (Wall: {wall_side} ${wall_price:,.2f} {wall_qty:,.2f} BTC)")
        return False

    telegram_url = f"https://api.telegram.org/bot{token}/sendMessage"
    distance = abs(wall_price - current_price)
    pct_distance = (distance / current_price) * 100 if current_price > 0 else 0
    usdt_val = wall_price * wall_qty
    
    emoji = "🟢 <b>ALIM DUVARI (BID)</b>" if wall_side == "BUY" else "🔴 <b>SATIM DUVARI (ASK)</b>"
    
    msg_lines = [
        f"🚨 <b>BINANCE FUTURES WALL ALERT!</b> 🚨",
        f"",
        f"<b>Parite:</b> #{symbol}",
        f"<b>Taraf:</b> {emoji}",
        f"<b>Duvar Fiyatı:</b> <code>${wall_price:,.2f}</code>",
        f"<b>Miktar:</b> <b>{wall_qty:,.2f} BTC</b> (~${usdt_val/1e6:,.2f}M USDT)",
        f"<b>Mevcut Fiyat:</b> <code>${current_price:,.2f}</code>",
        f"<b>Mesafe:</b> ${distance:,.2f} (%{pct_distance:.2f})",
    ]

    if total_range_qty > wall_qty:
        msg_lines.append(f"<b>±${bot_config['distance_limit']:,.0f} İçi Toplam Derinlik:</b> {total_range_qty:,.2f} BTC")

    msg_lines.append(f"\n⏰ <i>{time.strftime('%H:%M:%S %d.%m.%Y')}</i>")
    
    payload = {
        "chat_id": chat_id,
        "text": "\n".join(msg_lines),
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    try:
        res = requests.post(telegram_url, json=payload, timeout=10)
        if res.status_code == 200:
            print(f"[OK] Telegram alert sent for {wall_side} ${wall_price:,.2f} ({wall_qty:,.2f} BTC)")
            return True
        else:
            print(f"[ERROR] Telegram API returned {res.status_code}: {res.text}")
            return False
    except Exception as e:
        print(f"[ERROR] Failed to send Telegram alert: {e}")
        return False

# ── ORDERBOOK ENGINE ─────────────────────────────────────────────────────────

def process_orderbook(bids: list, asks: list, current_price: float):
    if not bot_config.get("bot_enabled", True):
        return

    symbol = bot_config.get("symbol", "BTCUSDT").upper()
    distance_limit = float(bot_config.get("distance_limit", 200.0))
    volume_threshold = float(bot_config.get("volume_threshold", 1000.0))
    cooldown_sec = int(bot_config.get("cooldown_seconds", 180))
    
    bot_state["current_price"] = current_price
    bot_state["last_update_ts"] = time.time()
    now = time.time()
    
    # Process Bids
    tot_bid_vol = 0.0
    for price_str, qty_str in bids:
        price = float(price_str)
        qty = float(qty_str)
        if current_price - price <= distance_limit and price <= current_price:
            tot_bid_vol += qty
            if qty >= volume_threshold:
                wall_key = ("BUY", round(price, 1))
                last_time = bot_state["last_alerts"].get(wall_key, 0)
                if now - last_time > cooldown_sec:
                    sent = send_telegram_alert(symbol, "BUY", price, qty, current_price, tot_bid_vol)
                    bot_state["last_alerts"][wall_key] = now
                    # Add to detected walls feed
                    wall_record = {
                        "id": int(now * 1000),
                        "side": "BUY",
                        "price": price,
                        "qty": qty,
                        "usdt_val": price * qty,
                        "distance": current_price - price,
                        "current_price": current_price,
                        "ts": time.strftime("%H:%M:%S"),
                        "telegram_sent": sent
                    }
                    bot_state["detected_walls"].insert(0, wall_record)
                    if len(bot_state["detected_walls"]) > 50:
                        bot_state["detected_walls"] = bot_state["detected_walls"][:50]

    # Process Asks
    tot_ask_vol = 0.0
    for price_str, qty_str in asks:
        price = float(price_str)
        qty = float(qty_str)
        if price - current_price <= distance_limit and price >= current_price:
            tot_ask_vol += qty
            if qty >= volume_threshold:
                wall_key = ("SELL", round(price, 1))
                last_time = bot_state["last_alerts"].get(wall_key, 0)
                if now - last_time > cooldown_sec:
                    sent = send_telegram_alert(symbol, "SELL", price, qty, current_price, tot_ask_vol)
                    bot_state["last_alerts"][wall_key] = now
                    wall_record = {
                        "id": int(now * 1000),
                        "side": "SELL",
                        "price": price,
                        "qty": qty,
                        "usdt_val": price * qty,
                        "distance": price - current_price,
                        "current_price": current_price,
                        "ts": time.strftime("%H:%M:%S"),
                        "telegram_sent": sent
                    }
                    bot_state["detected_walls"].insert(0, wall_record)
                    if len(bot_state["detected_walls"]) > 50:
                        bot_state["detected_walls"] = bot_state["detected_walls"][:50]

# ── BACKGROUND WEBSOCKET LISTENER ───────────────────────────────────────────

async def binance_ws_loop():
    while True:
        try:
            symbol = bot_config.get("symbol", "BTCUSDT").lower()
            ws_url = f"wss://fstream.binance.com/ws/{symbol}@depth20@100ms"
            print(f"[WEBSOCKET] Connecting to {ws_url}...")
            bot_state["is_connected"] = True
            
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                while True:
                    # Check if symbol changed in config
                    current_sub_symbol = bot_config.get("symbol", "BTCUSDT").lower()
                    if current_sub_symbol != symbol:
                        print(f"[WEBSOCKET] Symbol changed from {symbol} to {current_sub_symbol}. Reconnecting...")
                        break

                    msg = await ws.recv()
                    data = json.loads(msg)
                    bids = data.get("b", [])
                    asks = data.get("a", [])
                    if bids and asks:
                        best_bid = float(bids[0][0])
                        best_ask = float(asks[0][0])
                        current_price = (best_bid + best_ask) / 2.0
                        process_orderbook(bids, asks, current_price)
                        
        except Exception as e:
            bot_state["is_connected"] = False
            print(f"[WEBSOCKET ERROR] {e}. Retrying in 5 seconds...")
            await asyncio.sleep(5)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(binance_ws_loop())

# ── API ENDPOINTS ─────────────────────────────────────────────────────────────

def verify_password(x_admin_password: Optional[str] = Header(None)):
    if ADMIN_PASSWORD and x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Geçersiz şifre! (Invalid Admin Password)")
    return True

@app.post("/api/verify-auth")
def verify_auth_endpoint(data: AuthCheckModel):
    if ADMIN_PASSWORD and data.password != ADMIN_PASSWORD:
        return JSONResponse(status_code=401, content={"success": False, "message": "Hatalı şifre!"})
    return {"success": True, "message": "Giriş başarılı!"}

@app.get("/api/status")
def get_status():
    return {
        "symbol": bot_config.get("symbol", "BTCUSDT"),
        "current_price": bot_state["current_price"],
        "is_connected": bot_state["is_connected"],
        "bot_enabled": bot_config.get("bot_enabled", True),
        "distance_limit": bot_config.get("distance_limit", 200.0),
        "volume_threshold": bot_config.get("volume_threshold", 1000.0),
        "cooldown_seconds": bot_config.get("cooldown_seconds", 180),
        "detected_walls": bot_state["detected_walls"]
    }

@app.get("/api/config")
def get_config(auth: bool = Depends(verify_password)):
    # Mask telegram token for security preview
    cfg_copy = bot_config.copy()
    if cfg_copy.get("telegram_token"):
        tok = cfg_copy["telegram_token"]
        if len(tok) > 10:
            cfg_copy["telegram_token_masked"] = tok[:4] + "..." + tok[-4:]
        else:
            cfg_copy["telegram_token_masked"] = "***"
    return cfg_copy

@app.post("/api/config")
def update_config(new_cfg: ConfigModel, auth: bool = Depends(verify_password)):
    global bot_config
    bot_config["symbol"] = new_cfg.symbol.strip().upper()
    bot_config["distance_limit"] = new_cfg.distance_limit
    bot_config["volume_threshold"] = new_cfg.volume_threshold
    bot_config["cooldown_seconds"] = new_cfg.cooldown_seconds
    bot_config["telegram_token"] = new_cfg.telegram_token.strip()
    bot_config["chat_id"] = new_cfg.chat_id.strip()
    bot_config["bot_enabled"] = new_cfg.bot_enabled

    save_config(bot_config)
    return {"success": True, "message": "Ayarlar başarıyla güncellendi!", "config": bot_config}

@app.post("/api/test-telegram")
def test_telegram(auth: bool = Depends(verify_password)):
    symbol = bot_config.get("symbol", "BTCUSDT")
    current_price = bot_state["current_price"] if bot_state["current_price"] > 0 else 65000.0
    sent = send_telegram_alert(symbol, "BUY", current_price - 50, 1250.0, current_price, 1500.0)
    if sent:
        return {"success": True, "message": "Test mesajı Telegram'a başarıyla gönderildi!"}
    else:
        return {"success": False, "message": "Telegram mesajı gönderilemedi. Token ve Chat ID'nizi kontrol edin."}

# ── WEB DASHBOARD HTML ───────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    return """<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Binance Futures 1000 BTC Wall Alert Bot Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-dark: #090c10;
            --card-bg: rgba(18, 24, 38, 0.75);
            --card-border: rgba(255, 255, 255, 0.08);
            --accent-primary: #3b82f6;
            --accent-green: #10b981;
            --accent-red: #ef4444;
            --text-main: #f3f4f6;
            --text-muted: #9ca3af;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
        }

        body {
            background-color: var(--bg-dark);
            background-image: 
                radial-gradient(at 0% 0%, rgba(59, 130, 246, 0.12) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(16, 185, 129, 0.08) 0px, transparent 50%);
            background-attachment: fixed;
            color: var(--text-main);
            min-height: 100vh;
            padding: 24px;
        }

        .container {
            max-width: 1300px;
            margin: 0 auto;
        }

        /* Header */
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 20px 28px;
            background: var(--card-bg);
            backdrop-filter: blur(16px);
            border: 1px solid var(--card-border);
            border-radius: 20px;
            margin-bottom: 24px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.3);
        }

        .logo-area {
            display: flex;
            align-items: center;
            gap: 14px;
        }

        .logo-icon {
            width: 44px;
            height: 44px;
            background: linear-gradient(135deg, #3b82f6, #1d4ed8);
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 22px;
            box-shadow: 0 4px 15px rgba(59, 130, 246, 0.4);
        }

        .title-group h1 {
            font-size: 20px;
            font-weight: 700;
            letter-spacing: -0.5px;
        }

        .title-group p {
            font-size: 13px;
            color: var(--text-muted);
        }

        .status-badges {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 8px 16px;
            border-radius: 30px;
            font-size: 13px;
            font-weight: 600;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--card-border);
        }

        .dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
        }

        .dot.green { background-color: var(--accent-green); box-shadow: 0 0 10px var(--accent-green); }
        .dot.red { background-color: var(--accent-red); box-shadow: 0 0 10px var(--accent-red); }

        /* Metric Grid */
        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 20px;
            margin-bottom: 24px;
        }

        .metric-card {
            background: var(--card-bg);
            backdrop-filter: blur(16px);
            border: 1px solid var(--card-border);
            border-radius: 18px;
            padding: 20px 24px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
        }

        .metric-label {
            font-size: 13px;
            color: var(--text-muted);
            margin-bottom: 8px;
            font-weight: 500;
        }

        .metric-value {
            font-size: 26px;
            font-weight: 800;
            letter-spacing: -0.5px;
        }

        .metric-sub {
            font-size: 12px;
            margin-top: 6px;
            color: var(--text-muted);
        }

        /* Layout Grid */
        .main-grid {
            display: grid;
            grid-template-columns: 1fr 420px;
            gap: 24px;
        }

        @media (max-width: 1024px) {
            .main-grid { grid-template-columns: 1fr; }
        }

        .section-card {
            background: var(--card-bg);
            backdrop-filter: blur(16px);
            border: 1px solid var(--card-border);
            border-radius: 20px;
            padding: 24px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.2);
        }

        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 14px;
            border-bottom: 1px solid var(--card-border);
        }

        .section-header h2 {
            font-size: 16px;
            font-weight: 700;
        }

        /* Form Controls */
        .form-group {
            margin-bottom: 18px;
        }

        .form-group label {
            display: block;
            font-size: 13px;
            font-weight: 600;
            color: #d1d5db;
            margin-bottom: 8px;
        }

        .form-control {
            width: 100%;
            padding: 12px 16px;
            background: rgba(10, 14, 23, 0.8);
            border: 1px solid rgba(255, 255, 255, 0.12);
            border-radius: 12px;
            color: #fff;
            font-size: 14px;
            outline: none;
            transition: border-color 0.2s;
        }

        .form-control:focus {
            border-color: var(--accent-primary);
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.2);
        }

        .btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            width: 100%;
            padding: 14px;
            border-radius: 12px;
            font-size: 14px;
            font-weight: 700;
            cursor: pointer;
            border: none;
            transition: all 0.2s;
        }

        .btn-primary {
            background: linear-gradient(135deg, #3b82f6, #2563eb);
            color: #fff;
            box-shadow: 0 4px 15px rgba(59, 130, 246, 0.3);
        }

        .btn-primary:hover {
            opacity: 0.92;
            transform: translateY(-1px);
        }

        .btn-secondary {
            background: rgba(255, 255, 255, 0.08);
            color: #fff;
            margin-top: 10px;
        }

        .btn-secondary:hover {
            background: rgba(255, 255, 255, 0.14);
        }

        /* Toggle Switch */
        .toggle-wrapper {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 14px;
            background: rgba(10, 14, 23, 0.6);
            border-radius: 12px;
            border: 1px solid var(--card-border);
            margin-bottom: 18px;
        }

        .switch {
            position: relative;
            display: inline-block;
            width: 48px;
            height: 26px;
        }

        .switch input { opacity: 0; width: 0; height: 0; }

        .slider {
            position: absolute;
            cursor: pointer;
            top: 0; left: 0; right: 0; bottom: 0;
            background-color: #374151;
            transition: .3s;
            border-radius: 34px;
        }

        .slider:before {
            position: absolute;
            content: "";
            height: 18px;
            width: 18px;
            left: 4px;
            bottom: 4px;
            background-color: white;
            transition: .3s;
            border-radius: 50%;
        }

        input:checked + .slider { background-color: var(--accent-green); }
        input:checked + .slider:before { transform: translateX(22px); }

        /* Table Feed */
        .table-container {
            overflow-x: auto;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }

        th {
            text-align: left;
            padding: 12px 14px;
            color: var(--text-muted);
            border-bottom: 1px solid var(--card-border);
            font-weight: 600;
        }

        td {
            padding: 14px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.04);
        }

        .badge-buy {
            color: var(--accent-green);
            background: rgba(16, 185, 129, 0.12);
            padding: 4px 10px;
            border-radius: 20px;
            font-weight: 700;
            display: inline-block;
        }

        .badge-sell {
            color: var(--accent-red);
            background: rgba(239, 68, 68, 0.12);
            padding: 4px 10px;
            border-radius: 20px;
            font-weight: 700;
            display: inline-block;
        }

        /* Modal Auth Overlay */
        .modal-overlay {
            position: fixed;
            top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0, 0, 0, 0.85);
            backdrop-filter: blur(12px);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 999;
        }

        .modal-box {
            background: #111622;
            border: 1px solid var(--card-border);
            border-radius: 24px;
            padding: 36px;
            width: 100%;
            max-width: 400px;
            box-shadow: 0 20px 50px rgba(0,0,0,0.5);
            text-align: center;
        }

        .modal-box h3 {
            font-size: 20px;
            margin-bottom: 8px;
        }

        .modal-box p {
            font-size: 13px;
            color: var(--text-muted);
            margin-bottom: 24px;
        }

        .alert-toast {
            padding: 12px 16px;
            border-radius: 10px;
            font-size: 13px;
            margin-bottom: 16px;
            display: none;
        }

        .alert-toast.success { background: rgba(16, 185, 129, 0.2); color: var(--accent-green); border: 1px solid var(--accent-green); }
        .alert-toast.error { background: rgba(239, 68, 68, 0.2); color: var(--accent-red); border: 1px solid var(--accent-red); }
    </style>
</head>
<body>

<!-- AUTH MODAL -->
<div id="authModal" class="modal-overlay">
    <div class="modal-box">
        <div style="font-size: 40px; margin-bottom: 12px;">🔐</div>
        <h3>Yönetici Girişi</h3>
        <p>Ayar değiştirmek için Railway'de belirlediğiniz <code>ADMIN_PASSWORD</code> şifresini girin.</p>
        <div id="authAlert" class="alert-toast error"></div>
        <form onsubmit="handleLogin(event)">
            <div class="form-group">
                <input type="password" id="authPassword" class="form-control" placeholder="Şifreniz" required autofocus>
            </div>
            <button type="submit" class="btn btn-primary">Giriş Yap</button>
        </form>
    </div>
</div>

<div class="container">
    <!-- HEADER -->
    <header>
        <div class="logo-area">
            <div class="logo-icon">🧱</div>
            <div class="title-group">
                <h1>Binance 1K BTC Wall Detector</h1>
                <p>Orderbook Depth Monitor & Telegram Alert System</p>
            </div>
        </div>
        <div class="status-badges">
            <div class="badge">
                <div id="wsDot" class="dot red"></div>
                <span id="wsStatus">Bağlanıyor...</span>
            </div>
            <div class="badge">
                <div id="botDot" class="dot green"></div>
                <span id="botStateText">Bot Aktif</span>
            </div>
            <button onclick="openLoginModal()" style="background:none; border:none; color:var(--text-muted); cursor:pointer; font-size:16px;" title="Şifre Değiştir/Kilitle">🔒</button>
        </div>
    </header>

    <!-- METRICS -->
    <div class="metrics-grid">
        <div class="metric-card">
            <div class="metric-label">Anlık Fiyat (<span id="symbolLabel">BTCUSDT</span>)</div>
            <div class="metric-value" id="currentPrice">$0.00</div>
            <div class="metric-sub" id="lastUpdate">Son güncelleme: --</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Duvar Hacim Eşiği</div>
            <div class="metric-value" id="volThresholdVal">1,000 BTC</div>
            <div class="metric-sub">Minimum emir büyüklüğü</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Maksimum Mesafe</div>
            <div class="metric-value" id="distanceLimitVal">$200 USDT</div>
            <div class="metric-sub">Anlık fiyata göre mesafe</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Tespit Edilen Duvarlar</div>
            <div class="metric-value" id="wallsCount">0</div>
            <div class="metric-sub">Son oturumdaki toplam duvar</div>
        </div>
    </div>

    <!-- MAIN GRID -->
    <div class="main-grid">
        <!-- LIVE FEED TABLE -->
        <div class="section-card">
            <div class="section-header">
                <h2>📊 Canlı Tespit Edilen Duvarlar</h2>
                <span style="font-size:12px; color:var(--text-muted);" id="feedCount">0 kayıt</span>
            </div>
            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>Saat</th>
                            <th>Taraf</th>
                            <th>Duvar Fiyatı</th>
                            <th>Miktar (BTC)</th>
                            <th>USDT Değeri</th>
                            <th>Mesafe</th>
                            <th>Telegram</th>
                        </tr>
                    </thead>
                    <tbody id="wallsTableBody">
                        <tr>
                            <td colspan="7" style="text-align:center; color:var(--text-muted); padding:30px;">Henüz 1000 BTC üzeri duvar tespit edilmedi. Tahta canlı izleniyor...</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- SETTINGS PANEL -->
        <div class="section-card">
            <div class="section-header">
                <h2>⚙️ Bot Ayarları</h2>
            </div>
            <div id="settingsToast" class="alert-toast"></div>
            
            <form id="settingsForm" onsubmit="saveSettings(event)">
                <div class="toggle-wrapper">
                    <div>
                        <div style="font-weight:700; font-size:14px;">Bot Durumu</div>
                        <div style="font-size:12px; color:var(--text-muted);">Bildirimleri aç / kapat</div>
                    </div>
                    <label class="switch">
                        <input type="checkbox" id="cfg_bot_enabled" checked>
                        <span class="slider"></span>
                    </label>
                </div>

                <div class="form-group">
                    <label>Takip Edilen Sembol (Symbol)</label>
                    <input type="text" id="cfg_symbol" class="form-control" value="BTCUSDT" required>
                </div>

                <div class="form-group">
                    <label>Fiyata Maksimum Uzaklık (USDT)</label>
                    <input type="number" step="1" id="cfg_distance_limit" class="form-control" value="200" required>
                </div>

                <div class="form-group">
                    <label>Minimum Duvar Büyüklüğü (BTC)</label>
                    <input type="number" step="10" id="cfg_volume_threshold" class="form-control" value="1000" required>
                </div>

                <div class="form-group">
                    <label>Soğuma Süresi - Cooldown (Saniye)</label>
                    <input type="number" step="10" id="cfg_cooldown_seconds" class="form-control" value="180" required>
                </div>

                <div class="form-group">
                    <label>Telegram Bot Token</label>
                    <input type="text" id="cfg_telegram_token" class="form-control" placeholder="123456789:ABCdef..." required>
                </div>

                <div class="form-group">
                    <label>Telegram Chat ID</label>
                    <input type="text" id="cfg_chat_id" class="form-control" placeholder="12345678" required>
                </div>

                <button type="submit" class="btn btn-primary">💾 Ayarları Kaydet</button>
                <button type="button" class="btn btn-secondary" onclick="testTelegram()">🧪 Telegram Test Mesajı Gönder</button>
            </form>
        </div>
    </div>
</div>

<script>
    let savedPassword = localStorage.getItem("admin_password") || "";

    function openLoginModal() {
        document.getElementById("authModal").style.display = "flex";
        document.getElementById("authPassword").focus();
    }

    async function handleLogin(e) {
        if(e) e.preventDefault();
        const pwd = document.getElementById("authPassword").value.trim();
        const alertEl = document.getElementById("authAlert");
        
        try {
            const res = await fetch("/api/verify-auth", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ password: pwd })
            });
            const data = await res.json();
            if(res.ok && data.success) {
                savedPassword = pwd;
                localStorage.setItem("admin_password", pwd);
                document.getElementById("authModal").style.display = "none";
                alertEl.style.display = "none";
                loadConfig();
            } else {
                alertEl.innerText = data.message || "Hatalı şifre!";
                alertEl.style.display = "block";
            }
        } catch(err) {
            alertEl.innerText = "Sunucu hatası!";
            alertEl.style.display = "block";
        }
    }

    async function loadConfig() {
        if(!savedPassword) {
            openLoginModal();
            return;
        }
        try {
            const res = await fetch("/api/config", {
                headers: { "X-Admin-Password": savedPassword }
            });
            if(res.status === 401) {
                openLoginModal();
                return;
            }
            const data = await res.json();
            document.getElementById("cfg_symbol").value = data.symbol || "BTCUSDT";
            document.getElementById("cfg_distance_limit").value = data.distance_limit || 200;
            document.getElementById("cfg_volume_threshold").value = data.volume_threshold || 1000;
            document.getElementById("cfg_cooldown_seconds").value = data.cooldown_seconds || 180;
            document.getElementById("cfg_telegram_token").value = data.telegram_token || "";
            document.getElementById("cfg_chat_id").value = data.chat_id || "";
            document.getElementById("cfg_bot_enabled").checked = data.bot_enabled !== false;
        } catch(err) {
            console.error("Config load error:", err);
        }
    }

    async function saveSettings(e) {
        e.preventDefault();
        const toast = document.getElementById("settingsToast");
        toast.style.display = "none";

        const payload = {
            symbol: document.getElementById("cfg_symbol").value.trim(),
            distance_limit: parseFloat(document.getElementById("cfg_distance_limit").value),
            volume_threshold: parseFloat(document.getElementById("cfg_volume_threshold").value),
            cooldown_seconds: parseInt(document.getElementById("cfg_cooldown_seconds").value),
            telegram_token: document.getElementById("cfg_telegram_token").value.trim(),
            chat_id: document.getElementById("cfg_chat_id").value.trim(),
            bot_enabled: document.getElementById("cfg_bot_enabled").checked
        };

        try {
            const res = await fetch("/api/config", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-Admin-Password": savedPassword
                },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            if(res.ok && data.success) {
                toast.className = "alert-toast success";
                toast.innerText = data.message;
                toast.style.display = "block";
            } else {
                toast.className = "alert-toast error";
                toast.innerText = data.detail || "Kaydedilemedi!";
                toast.style.display = "block";
            }
        } catch(err) {
            toast.className = "alert-toast error";
            toast.innerText = "Ayar kaydedilirken bağlantı hatası!";
            toast.style.display = "block";
        }
    }

    async function testTelegram() {
        const toast = document.getElementById("settingsToast");
        toast.className = "alert-toast";
        toast.innerText = "Test mesajı gönderiliyor...";
        toast.style.display = "block";

        try {
            const res = await fetch("/api/test-telegram", {
                method: "POST",
                headers: { "X-Admin-Password": savedPassword }
            });
            const data = await res.json();
            if(data.success) {
                toast.className = "alert-toast success";
            } else {
                toast.className = "alert-toast error";
            }
            toast.innerText = data.message;
        } catch(err) {
            toast.className = "alert-toast error";
            toast.innerText = "Test sırasında hata oluştu.";
        }
    }

    async function updateDashboard() {
        try {
            const res = await fetch("/api/status");
            if(!res.ok) return;
            const data = await res.json();

            // Labels
            document.getElementById("symbolLabel").innerText = data.symbol;
            document.getElementById("currentPrice").innerText = "$" + data.current_price.toLocaleString("en-US", {minimumFractionDigits:2, maximumFractionDigits:2});
            document.getElementById("lastUpdate").innerText = "Son güncelleme: " + new Date().toLocaleTimeString();
            document.getElementById("volThresholdVal").innerText = data.volume_threshold.toLocaleString() + " BTC";
            document.getElementById("distanceLimitVal").innerText = "$" + data.distance_limit.toLocaleString() + " USDT";

            // Status Badges
            const wsDot = document.getElementById("wsDot");
            const wsStatus = document.getElementById("wsStatus");
            if(data.is_connected) {
                wsDot.className = "dot green";
                wsStatus.innerText = "Canlı Yayın Bağlı";
            } else {
                wsDot.className = "dot red";
                wsStatus.innerText = "Bağlantı Kesildi";
            }

            const botDot = document.getElementById("botDot");
            const botStateText = document.getElementById("botStateText");
            if(data.bot_enabled) {
                botDot.className = "dot green";
                botStateText.innerText = "Bot Aktif";
            } else {
                botDot.className = "dot red";
                botStateText.innerText = "Bot Durduruldu";
            }

            // Walls Feed Table
            const walls = data.detected_walls || [];
            document.getElementById("wallsCount").innerText = walls.length;
            document.getElementById("feedCount").innerText = walls.length + " kayıt";

            const tbody = document.getElementById("wallsTableBody");
            if(walls.length === 0) {
                tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; color:var(--text-muted); padding:30px;">Henüz 1000 BTC üzeri duvar tespit edilmedi. Tahta canlı izleniyor...</td></tr>`;
            } else {
                tbody.innerHTML = walls.map(w => `
                    <tr>
                        <td><code>${w.ts}</code></td>
                        <td><span class="${w.side === 'BUY' ? 'badge-buy' : 'badge-sell'}">${w.side === 'BUY' ? '🟢 ALIM (BID)' : '🔴 SATIM (ASK)'}</span></td>
                        <td><b>$${w.price.toLocaleString("en-US", {minimumFractionDigits:2})}</b></td>
                        <td><b>${w.qty.toLocaleString("en-US", {minimumFractionDigits:2})} BTC</b></td>
                        <td>$${(w.usdt_val/1e6).toFixed(2)}M</td>
                        <td>$${w.distance.toFixed(2)}</td>
                        <td>${w.telegram_sent ? '✅ Gönderildi' : '⚠️ Gönderilemedi'}</td>
                    </tr>
                `).join('');
            }
        } catch(err) {
            console.error("Update error:", err);
        }
    }

    // Auto Init
    if(savedPassword) {
        document.getElementById("authModal").style.display = "none";
        loadConfig();
    } else {
        openLoginModal();
    }

    // Poll live status every 1.5s
    setInterval(updateDashboard, 1500);
    updateDashboard();
</script>

</body>
</html>
""";
