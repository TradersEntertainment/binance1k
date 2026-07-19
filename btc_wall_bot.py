import os
import sys
import time
import requests
import asyncio
import json

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
try:
    import websockets
except ImportError:
    websockets = None

# Configuration defaults
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
SYMBOL = "BTCUSDT"
DISTANCE_LIMIT = 200.0  # USDT distance from current price
VOLUME_THRESHOLD = 1000.0  # BTC minimum threshold for wall alert
COOLDOWN_SECONDS = 180  # Cooldown between repeat alerts for the same wall price level

# Endpoint URLs
REST_DEPTH_URL = f"https://fapi.binance.com/fapi/v1/depth?symbol={SYMBOL}&limit=1000"
REST_PRICE_URL = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={SYMBOL}"
WS_URL = f"wss://fstream.binance.com/ws/{SYMBOL.lower()}@depth20@100ms"

# State tracking for cooldowns: {(side, price_rounded): last_notified_timestamp}
last_alerts = {}

def send_telegram_alert(symbol: str, wall_side: str, wall_price: float, wall_qty: float, current_price: float, total_range_qty: float = 0.0):
    """Sends a formatted notification to Telegram."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("[ALERT TRIGGERED] Telegram credentials missing, printing to console instead:")
        print(f"  {wall_side} Wall | Price: ${wall_price:,.2f} | Size: {wall_qty:,.2f} BTC | Current: ${current_price:,.2f}")
        return

    telegram_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    distance = abs(wall_price - current_price)
    pct_distance = (distance / current_price) * 100
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
        msg_lines.append(f"<b>±$200 İçi Toplam Derinlik:</b> {total_range_qty:,.2f} BTC")

    msg_lines.append(f"\n⏰ <i>{time.strftime('%H:%M:%S %d.%m.%Y')}</i>")
    
    payload = {
        "chat_id": CHAT_ID,
        "text": "\n".join(msg_lines),
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    try:
        res = requests.post(telegram_url, json=payload, timeout=10)
        if res.status_code == 200:
            print(f"[OK] Telegram alert sent for {wall_side} wall at ${wall_price:,.2f} ({wall_qty:,.2f} BTC)")
        else:
            print(f"[ERROR] Telegram API failed ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"[ERROR] Failed to send Telegram alert: {e}")

def process_orderbook(bids: list, asks: list, current_price: float):
    """
    Analyzes orderbook bids and asks for walls >= VOLUME_THRESHOLD within DISTANCE_LIMIT of current_price.
    bids: list of [price_str, qty_str]
    asks: list of [price_str, qty_str]
    """
    now = time.time()
    
    # Analyze Bids (Buy orders)
    tot_bid_vol = 0.0
    for price_str, qty_str in bids:
        price = float(price_str)
        qty = float(qty_str)
        if current_price - price <= DISTANCE_LIMIT and price <= current_price:
            tot_bid_vol += qty
            if qty >= VOLUME_THRESHOLD:
                wall_key = ("BUY", round(price, 1))
                if wall_key not in last_alerts or (now - last_alerts[wall_key]) > COOLDOWN_SECONDS:
                    send_telegram_alert(SYMBOL, "BUY", price, qty, current_price, tot_bid_vol)
                    last_alerts[wall_key] = now

    # Analyze Asks (Sell orders)
    tot_ask_vol = 0.0
    for price_str, qty_str in asks:
        price = float(price_str)
        qty = float(qty_str)
        if price - current_price <= DISTANCE_LIMIT and price >= current_price:
            tot_ask_vol += qty
            if qty >= VOLUME_THRESHOLD:
                wall_key = ("SELL", round(price, 1))
                if wall_key not in last_alerts or (now - last_alerts[wall_key]) > COOLDOWN_SECONDS:
                    send_telegram_alert(SYMBOL, "SELL", price, qty, current_price, tot_ask_vol)
                    last_alerts[wall_key] = now

def check_depth_rest():
    """Poll depth via REST API."""
    try:
        resp = requests.get(REST_DEPTH_URL, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        
        if bids and asks:
            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
            current_price = (best_bid + best_ask) / 2.0
            process_orderbook(bids, asks, current_price)
            return current_price, len(bids), len(asks)
    except Exception as e:
        print(f"[REST ERROR] {e}")
    return None, 0, 0

async def stream_depth_ws():
    """Stream depth via WebSocket."""
    if not websockets:
        print("[WS ERROR] websockets module not installed. Falling back to REST polling.")
        return
        
    print(f"[INFO] Connecting to Binance WebSocket: {WS_URL}")
    async for websocket in websockets.connect(WS_URL):
        try:
            async for message in websocket:
                data = json.loads(message)
                bids = data.get("b", [])
                asks = data.get("a", [])
                if bids and asks:
                    best_bid = float(bids[0][0])
                    best_ask = float(asks[0][0])
                    current_price = (best_bid + best_ask) / 2.0
                    process_orderbook(bids, asks, current_price)
        except websockets.ConnectionClosed as e:
            print(f"[WS DISCONNECTED] {e}. Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"[WS ERROR] {e}")
            await asyncio.sleep(5)

def main():
    print("==================================================")
    print("   Binance Futures BTCUSDT 1000 BTC Wall Bot   ")
    print("==================================================")
    print(f"Symbol:           {SYMBOL}")
    print(f"Distance limit:   +/{DISTANCE_LIMIT} USDT")
    print(f"Wall threshold:   >= {VOLUME_THRESHOLD} BTC")
    print(f"Cooldown:         {COOLDOWN_SECONDS} sec per wall")
    print("--------------------------------------------------")
    
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("⚠️ WARNING: TELEGRAM_TOKEN or CHAT_ID environment variables are not set!")
        print("   Set them using:")
        print("   export TELEGRAM_TOKEN='your_token'")
        print("   export CHAT_ID='your_chat_id'")
        print("   (Or edit TELEGRAM_TOKEN / CHAT_ID in the script)")
    
    # Test REST check first
    print("\n[INFO] Performing initial REST check...")
    price, bids_cnt, asks_cnt = check_depth_rest()
    if price:
        print(f"[INFO] Success! BTC Price: ${price:,.2f} | Bids: {bids_cnt} levels | Asks: {asks_cnt} levels")
    
    # Check if websockets library is available
    if websockets:
        print("\n[INFO] Starting real-time WebSocket listener...")
        try:
            asyncio.run(stream_depth_ws())
        except KeyboardInterrupt:
            print("\n[INFO] Bot stopped by user.")
    else:
        print("\n[INFO] 'websockets' library not found. Running REST polling loop every 3 seconds...")
        try:
            while True:
                check_depth_rest()
                time.sleep(3)
        except KeyboardInterrupt:
            print("\n[INFO] Bot stopped by user.")

if __name__ == "__main__":
    main()
