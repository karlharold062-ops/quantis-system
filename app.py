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
MAX_ACTIVE_PAIRS = 2 # Quantis se concentre sur les 2 premières
TRADE_START_HOUR_GMT = 9
TRADE_END_HOUR_GMT = 22
TRADE_MINUTE_WINDOW = 2
MIN_CONFLUENCE_SCORE = 85
USE_LIMIT_ORDER = True  # True pour 'limit', False pour 'market'

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
WUNDERTRADE_WEBHOOK_URL = os.getenv("WUNDERTRADE_WEBHOOK_URL")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# ===================== TECHNICAL ANALYZER (7+ INDICATORS) =====================
class TechnicalAnalyzer7:
    def __init__(self, exchange):
        self.exchange = exchange

    def analyze_indicators(self, symbol: str, timeframe="1d"):
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
            closes = [c[4] for c in ohlcv]
            highs = [c[2] for c in ohlcv]
            lows = [c[3] for c in ohlcv]

            # RSI & MACD
            rsi = self._rsi(closes)
            ema12 = sum(closes[-12:])/12
            ema26 = sum(closes[-26:])/26
            macd = ema12 - ema26
            
            # MOYENNES
            ema20 = sum(closes[-20:])/20
            sma50 = sum(closes[-50:])/50
            
            # BOLLINGER BANDS
            sma20 = sum(closes[-20:])/20
            std20 = (sum([(x-sma20)**2 for x in closes[-20:]])/20)**0.5
            upper_band = sma20 + (2 * std20)
            lower_band = sma20 - (2 * std20)
            
            # ATR & ICHIMOKU
            atr = max(highs[-14:]) - min(lows[-14:])
            atr_percent = (atr / closes[-1]) * 100
            tenkan = (max(highs[-9:]) + min(lows[-9:]))/2
            kijun = (max(highs[-26:]) + min(lows[-26:]))/2

            # SCORE DE CONFLUENCE (Calcul sur 100)
            votes = 0
            votes += 15 if rsi > 50 else -15
            votes += 15 if macd > 0 else -15
            votes += 10 if closes[-1] > ema20 else -10
            votes += 10 if closes[-1] > sma50 else -10
            votes += 15 if tenkan > kijun else -15
            votes += 15 if closes[-1] > sma20 else -10 # Position / milieu Bollinger
            
            confluence_score = abs(votes) + 20 # Base + technique

            return {
                "price": closes[-1],
                "direction": "bullish" if votes > 0 else "bearish",
                "atr_percent": atr_percent,
                "confluence_score": confluence_score,
                "bands": {"upper": upper_band, "lower": lower_band}
            }
        except Exception as e:
            logger.error(f"Erreur Analyse: {e}")
            return None

    def _rsi(self, closes, period=14):
        if len(closes) < period: return 50
        deltas = [closes[i+1]-closes[i] for i in range(len(closes)-1)]
        up = sum([x for x in deltas[:period] if x > 0]) / period
        down = -sum([x for x in deltas[:period] if x < 0]) / period
        return 100 - 100/(1 + up/down if down != 0 else 1)

# ===================== SENTIMENT & ORDERBOOK =====================
class SentimentWhaleAnalyzer:
    def analyze(self, symbol):
        # Simulation d'analyse Whale/Sentiment (Peut être lié à Whale Alert API)
        return {"bias": "neutral", "whale_movement": False}

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
        self.exchange = ccxt.bybit({"apiKey": BYBIT_API_KEY, "secret": BYBIT_API_SECRET, "enableRateLimit": True})
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
            "sl": round(atr * 1, 2),
            "ts": round(atr * 0.5, 2)
        }

    def send_to_wunder(self, symbol, action, direction=None, levels=None):
        payload = {
            "action": action, # 'buy', 'sell' ou 'exit'
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

    def monitor_retracement(self, symbol, current_price):
        trade = self.active_trades.get(symbol)
        if not trade: return
        
        entry = trade["entry"]
        diff = ((current_price - entry) / entry) * 100
        profit = round(diff if trade["direction"] == "LONG" else -diff, 2)

        # Seuil de retracement : 0.5% contre nous
        if (trade["direction"] == "LONG" and current_price < entry * 0.995) or \
           (trade["direction"] == "SHORT" and current_price > entry * 1.005):
            
            logger.warning(f"Sortie d'urgence {symbol} | PnL: {profit}%")
            self.send_to_wunder(symbol, action="exit")
            send_discord_alert({"symbol": symbol, "direction": trade["direction"], "levels": trade}, "retracement", profit)
            del self.active_trades[symbol]

    def process(self, symbol):
        data = self.tech.analyze_indicators(symbol)
        if not data: return
        
        pressure = self.ob.analyze(symbol)
        sentiment = self.sent.analyze(symbol)
        
        # LOGIQUE DE DÉCISION
        direction = "HOLD"
        if data["direction"] == "bullish" and pressure == "buy" and sentiment["bias"] != "sell":
            direction = "LONG"
        elif data["direction"] == "bearish" and pressure == "sell" and sentiment["bias"] != "buy":
            direction = "SHORT"

        self.monitor_retracement(symbol, data["price"])
        
        now = datetime.now(timezone.utc)
        if (direction != "HOLD" and symbol not in self.active_trades and 
            data["confluence_score"] >= MIN_CONFLUENCE_SCORE and
            TRADE_START_HOUR_GMT <= now.hour <= TRADE_END_HOUR_GMT and
            now.minute <= TRADE_MINUTE_WINDOW):
            
            levels = self._get_levels(data)
            self.send_to_wunder(symbol, action=direction.lower(), direction=direction, levels=levels)
            send_discord_alert({"symbol": symbol, "direction": direction, "levels": levels}, "entry")
            self.active_trades[symbol] = {"direction": direction, **levels}

# ===================== DISCORD & FLASK =====================
def send_discord_alert(signal, alert_type, profit=0):
    lv = signal.get("levels", {})
    order_mode = "LIMIT" if USE_LIMIT_ORDER else "MARKET"
    
    emoji = "🚀" if alert_type == "entry" else "⚠️"
    msg = (f"{emoji} **QUANTIS {alert_type.upper()}**\n"
           f"Paire: {signal['symbol']} ({order_mode})\n"
           f"Direction: {signal['direction']}\n"
           f"Entrée: {lv.get('entry')}\n"
           f"TP: {lv.get('tp')}% | SL: {lv.get('sl')}% | TS: {lv.get('ts')}%")
    
    if alert_type == "retracement":
        msg += f"\n**PnL à la sortie: {profit}%**"
        
    if DISCORD_WEBHOOK_URL:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg})



app = Flask(__name__)
quantis = QuantisEngine()

@app.route("/health")
def health(): return jsonify({"status": "active"}), 200

def run_loop():
    while True:
        # On ne traite que les paires configurées dans MAX_ACTIVE_PAIRS
        for s in SYMBOLS[:MAX_ACTIVE_PAIRS]:
            try: quantis.process(s)
            except Exception as e: logger.error(f"Error: {e}")
        time.sleep(30)

if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080))), daemon=True).start()
    run_loop()
