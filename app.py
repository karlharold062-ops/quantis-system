import os
import ccxt
import time
import logging
import requests
import threading
from datetime import datetime, timezone
from flask import Flask, jsonify

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | QUANTIS | %(levelname)s | %(message)s"
)
logger = logging.getLogger("quantis")

# ===================== CONFIG DYNAMIQUE =====================
SYMBOLS = ["ZEC/USDT", "SOL/USDT", "ETH/USDT", "DOGE/USDT", "BTC/USDT"]
MAX_ACTIVE_PAIRS = 2 
TRADE_START_HOUR_GMT = 9
TRADE_END_HOUR_GMT = 22
TRADE_MINUTE_WINDOW = 2
MIN_CONFLUENCE_SCORE = 75 
USE_LIMIT_ORDER = True  

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
WUNDERTRADE_WEBHOOK_URL = os.getenv("WUNDERTRADE_WEBHOOK_URL")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# ===================== TECHNICAL ANALYZER =====================
class TechnicalAnalyzer7:
    def __init__(self, exchange):
        self.exchange = exchange

    def analyze_indicators(self, symbol: str, timeframe="1d"):
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
            closes = [c[4] for c in ohlcv]
            highs = [c[2] for c in ohlcv]
            lows = [c[3] for c in ohlcv]

            rsi = self._rsi(closes)
            ema12 = sum(closes[-12:])/12
            ema26 = sum(closes[-26:])/26
            macd = ema12 - ema26
            
            ema20 = sum(closes[-20:])/20
            sma50 = sum(closes[-50:])/50
            
            sma20 = sum(closes[-20:])/20
            std20 = (sum([(x-sma20)**2 for x in closes[-20:]])/20)**0.5
            
            atr = max(highs[-14:]) - min(lows[-14:])
            atr_percent = (atr / closes[-1]) * 100
            tenkan = (max(highs[-9:]) + min(lows[-9:]))/2
            kijun = (max(highs[-26:]) + min(lows[-26:]))/2

            votes = 0
            votes += 15 if rsi > 50 else -15
            votes += 15 if macd > 0 else -15
            votes += 10 if closes[-1] > ema20 else -10
            votes += 10 if closes[-1] > sma50 else -10
            votes += 15 if tenkan > kijun else -15
            votes += 15 if closes[-1] > ema12 else -10 
            
            confluence_score = abs(votes) + 20

            return {
                "price": closes[-1],
                "direction": "bullish" if votes > 0 else "bearish",
                "atr_percent": atr_percent,
                "confluence_score": confluence_score
            }
        except Exception as e:
            logger.error(f"Erreur Analyse {timeframe}: {e}")
            return None

    def _rsi(self, closes, period=14):
        if len(closes) < period: return 50
        deltas = [closes[i+1]-closes[i] for i in range(len(closes)-1)]
        up = sum([x for x in deltas[:period] if x > 0]) / period
        down = -sum([x for x in deltas[:period] if x < 0]) / period
        return 100 - 100/(1 + up/down if down != 0 else 1)

# ===================== ANALYZERS =====================
class SentimentWhaleAnalyzer:
    def analyze(self, symbol):
        return {"bias": "neutral"}

class OrderBookAnalyzer:
    def __init__(self, exchange): self.exchange = exchange
    def analyze(self, symbol):
        try:
            ob = self.exchange.fetch_order_book(symbol)
            imbalance = sum([b[1] for b in ob['bids'][:5]]) - sum([a[1] for a in ob['asks'][:5]])
            return "buy" if imbalance > 0 else "sell"
        except: return "neutral"

# ===================== ENGINE QUANTIS =====================
class QuantisEngine:
    def __init__(self):
        self.exchange = ccxt.bybit({"apiKey": BYBIT_API_KEY, "secret": BYBIT_API_SECRET})
        self.tech = TechnicalAnalyzer7(self.exchange)
        self.ob = OrderBookAnalyzer(self.exchange)
        self.sent = SentimentWhaleAnalyzer()
        self.active_trades = {}

    def _get_levels(self, data):
        price = data["price"]
        atr = data["atr_percent"]
        return {
            "entry": price,
            "tp": round(atr * 2, 2),
            "sl": round(atr * 1.2, 2),
            "ts": round(atr * 0.5, 2)
        }

    def send_to_wunder(self, action, symbol, direction=None, levels=None):
        payload = {
            "action": action,
            "pair": symbol.replace("/", ""),
            "order_type": "limit" if USE_LIMIT_ORDER and action != "exit" else "market"
        }
        if levels:
            payload.update({
                "direction": direction.lower(),
                "entry_price": levels["entry"],
                "take_profit": levels["tp"],
                "stop_loss": levels["sl"],
                "trailing_stop": levels["ts"]
            })
        try:
            requests.post(WUNDERTRADE_WEBHOOK_URL, json=payload, timeout=5)
        except Exception as e: logger.error(f"Wunder Error: {e}")

    # ===================== NOUVELLE SURVEILLANCE MTF =====================
    def monitor_mtf_reversal(self, symbol):
        trade = self.active_trades.get(symbol)
        if not trade: return

        # On interroge le Radar 1H et 4H
        data_1h = self.tech.analyze_indicators(symbol, timeframe="1h")
        data_4h = self.tech.analyze_indicators(symbol, timeframe="4h")
        
        if not data_1h or not data_4h: return

        # Logique de sortie : Si le 1H ET le 4H se retournent contre le trade 1D
        should_exit = False
        if trade["direction"] == "LONG":
            if data_1h["direction"] == "bearish" and data_4h["direction"] == "bearish":
                should_exit = True
        elif trade["direction"] == "SHORT":
            if data_1h["direction"] == "bullish" and data_4h["direction"] == "bullish":
                should_exit = True

        if should_exit:
            logger.warning(f"⚠️ URGENCE MTF {symbol} | Retournement 1H & 4H détecté.")
            self.send_to_wunder("exit", symbol)
            send_discord_alert({"symbol": symbol, "direction": trade["direction"]}, "mtf_reversal")
            del self.active_trades[symbol]

    def process(self, symbol):
        data = self.tech.analyze_indicators(symbol, timeframe="1d")
        if not data: return
        
        # Surveillance active si un trade est en cours
        if symbol in self.active_trades:
            self.monitor_mtf_reversal(symbol)
            return # On ne cherche pas d'entrée si on est déjà dedans

        pressure = self.ob.analyze(symbol)
        direction = "HOLD"
        if data["direction"] == "bullish" and pressure == "buy":
            direction = "LONG"
        elif data["direction"] == "bearish" and pressure == "sell":
            direction = "SHORT"
        
        now = datetime.now(timezone.utc)
        if (direction != "HOLD" and data["confluence_score"] >= MIN_CONFLUENCE_SCORE and
            TRADE_START_HOUR_GMT <= now.hour <= TRADE_END_HOUR_GMT):
            
            levels = self._get_levels(data)
            self.send_to_wunder(direction.lower(), symbol, direction, levels)
            send_discord_alert({"symbol": symbol, "direction": direction, "levels": levels}, "entry")
            self.active_trades[symbol] = {"direction": direction, **levels}

# ===================== NOTIFICATIONS =====================
def send_discord_alert(signal, alert_type):
    emoji = "🚀" if alert_type == "entry" else "⚠️"
    title = "ENTRÉE" if alert_type == "entry" else "URGENCE MTF"
    msg = (f"{emoji} **QUANTIS {title}**\n"
           f"Paire: {signal['symbol']}\n"
           f"Direction: {signal['direction']}\n")
    
    if alert_type == "entry":
        lv = signal.get("levels", {})
        msg += f"Entrée: {lv.get('entry')}\nTP: {lv.get('tp')}% | SL: {lv.get('sl')}%"
    else:
        msg += "Raison: Structure 1H et 4H inversée. Position fermée."

    if DISCORD_WEBHOOK_URL: 
        try: requests.post(DISCORD_WEBHOOK_URL, json={"content": msg})
        except: pass

app = Flask(__name__)
quantis = QuantisEngine()

@app.route("/health")
def health(): return jsonify({"status": "active"}), 200

def run_loop():
    while True:
        for s in SYMBOLS[:MAX_ACTIVE_PAIRS]:
            try: quantis.process(s)
            except Exception as e: logger.error(f"Loop Error: {e}")
        time.sleep(30)

if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=8080), daemon=True).start()
    run_loop()
