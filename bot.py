"""
Binance Futures Alert Bot
─────────────────────────
Monitors funding rates and 24h price changes for a curated list of
USDT-margined futures pairs.  Sends a single consolidated Telegram
message whenever any pair triggers an alert.

Alert thresholds
  • Funding rate  ≥  0.5 %  or  ≤ -0.5 %
  • 24h price Δ   ≥ 15 %   or  ≤ -15 %

Designed to run as a GitHub Actions cron job every 15 minutes.
"""

import os
import sys
import requests

# ── Configuration ────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

FUNDING_THRESHOLD = 0.5        # %
PRICE_CHANGE_THRESHOLD = 15.0  # %

WATCH_LIST = [
    "AAVEUSDT", "ADAUSDT", "AIXBTUSDT", "ALGOUSDT", "APTUSDT",
    "ARBUSDT", "ASTERUSDT", "ATOMUSDT", "AVAXUSDT", "BCHUSDT",
    "BNBUSDT", "BONKUSDT", "BTCUSDT", "CRVUSDT", "DOGEUSDT",
    "DOTUSDT", "ETCUSDT", "ETHUSDT", "FARTCOINUSDT", "FILUSDT",
    "FLOKIUSDT", "GRASSUSDT", "HBARUSDT", "HYPEUSDT", "INJUSDT",
    "IPUSDT", "JTOUSDT", "JUPUSDT", "KAITOUSDT", "LDOUSDT",
    "LINKUSDT", "LITUSDT", "LTCUSDT", "MOODENGUSDT", "NEARUSDT",
    "ONDOUSDT", "OPUSDT", "ORDIUSDT", "PENGUUSDT", "PEPEUSDT",
    "PNUTUSDT", "POLUSDT", "POPCATUSDT", "PUMPUSDT", "RENDERUSDT",
    "SUSDT", "SHIBUSDT", "SOLUSDT", "STXUSDT", "SUIUSDT",
    "TAOUSDT", "TIAUSDT", "TONUSDT", "TRUMPUSDT", "TRXUSDT",
    "UNIUSDT", "VIRTUALUSDT", "WIFUSDT", "WLDUSDT", "XPLUSDT",
    "XRPUSDT", "ZECUSDT",
]

BINANCE_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
BINANCE_PREMIUM_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


# ── Helpers ──────────────────────────────────────────────────────────

def fetch_json(url: str) -> list[dict]:
    """GET *url* and return the parsed JSON list."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def funding_label(rate: float) -> str:
    """Return a human-readable label for the funding direction."""
    if rate >= FUNDING_THRESHOLD:
        return "Aşırı Long 🟢"
    elif rate <= -FUNDING_THRESHOLD:
        return "Aşırı Short 🔴"
    return ""


def price_label(change: float) -> str:
    """Return a human-readable label for the price direction."""
    if change >= PRICE_CHANGE_THRESHOLD:
        return "Sert Yükseliş 🚀"
    elif change <= -PRICE_CHANGE_THRESHOLD:
        return "Sert Düşüş 📉"
    return ""


def build_alert_message(alerts: list[dict]) -> str:
    """Combine all individual alerts into one Telegram-friendly message."""
    lines: list[str] = ["🚨 <b>Binance Futures Alarm!</b>\n"]

    for a in alerts:
        lines.append(f"━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"🔔 <b>{a['symbol']}</b>")

        if a.get("funding_alert"):
            direction = funding_label(a["funding_rate"])
            lines.append(
                f"  💰 Funding: <b>%{a['funding_rate']:.4f}</b> ({direction})"
            )

        if a.get("price_alert"):
            direction = price_label(a["price_change"])
            lines.append(
                f"  📈 24s Değişim: <b>%{a['price_change']:.2f}</b> ({direction})"
            )

        # Bonus: if BOTH fire, highlight the combo signal
        if a.get("funding_alert") and a.get("price_alert"):
            # Negative funding + negative price → potential Long squeeze setup
            if a["funding_rate"] <= -FUNDING_THRESHOLD and a["price_change"] <= -PRICE_CHANGE_THRESHOLD:
                lines.append("  ⚡ <b>Short Squeeze Potansiyeli!</b> (Düşük Funding + Sert Düşüş)")
            elif a["funding_rate"] >= FUNDING_THRESHOLD and a["price_change"] >= PRICE_CHANGE_THRESHOLD:
                lines.append("  ⚡ <b>Long Squeeze Potansiyeli!</b> (Yüksek Funding + Sert Yükseliş)")

    lines.append(f"\n⏰ Kontrol periyodu: 15 dk")
    return "\n".join(lines)


def send_telegram(message: str) -> None:
    """Send *message* to the configured Telegram chat using HTML parse mode."""
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(TELEGRAM_API_URL, json=payload, timeout=30)
    if resp.status_code != 200:
        print(f"[ERROR] Telegram API responded with {resp.status_code}: {resp.text}")
    else:
        print("[OK] Telegram message sent successfully.")


# ── Main Logic ───────────────────────────────────────────────────────

def main() -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("[ERROR] TELEGRAM_TOKEN or CHAT_ID environment variable is missing.")
        sys.exit(1)

    watch_set = set(WATCH_LIST)

    # 1. Fetch data from Binance  ──────────────────────────────────────
    print("[INFO] Fetching 24h ticker data …")
    ticker_data = fetch_json(BINANCE_TICKER_URL)
    ticker_map: dict[str, dict] = {
        t["symbol"]: t for t in ticker_data if t["symbol"] in watch_set
    }

    print("[INFO] Fetching premium index (funding) data …")
    premium_data = fetch_json(BINANCE_PREMIUM_URL)
    funding_map: dict[str, dict] = {
        p["symbol"]: p for p in premium_data if p["symbol"] in watch_set
    }

    # 2. Evaluate alert conditions  ────────────────────────────────────
    alerts: list[dict] = []

    for symbol in sorted(watch_set):
        funding_rate_pct = 0.0
        price_change_pct = 0.0
        funding_alert = False
        price_alert = False

        # Funding rate
        if symbol in funding_map:
            raw_rate = float(funding_map[symbol].get("lastFundingRate", 0))
            funding_rate_pct = raw_rate * 100  # convert to percentage
            if abs(funding_rate_pct) >= FUNDING_THRESHOLD:
                funding_alert = True

        # 24h price change
        if symbol in ticker_map:
            price_change_pct = float(ticker_map[symbol].get("priceChangePercent", 0))
            if abs(price_change_pct) >= PRICE_CHANGE_THRESHOLD:
                price_alert = True

        if funding_alert or price_alert:
            alerts.append({
                "symbol": symbol,
                "funding_rate": funding_rate_pct,
                "price_change": price_change_pct,
                "funding_alert": funding_alert,
                "price_alert": price_alert,
            })

    # 3. Send Telegram notification  ───────────────────────────────────
    if not alerts:
        print("[INFO] No alerts triggered. Nothing to send.")
        return

    print(f"[INFO] {len(alerts)} alert(s) triggered. Sending Telegram message …")
    message = build_alert_message(alerts)
    send_telegram(message)


if __name__ == "__main__":
    main()
