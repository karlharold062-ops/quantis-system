    import os
import requests
import logging
import ccxt
import threading
import time
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | QUANTIS | %(levelname)s | %(message)s"
)
logger = logging.getLogger("quantis")

# ===================== CONFIG =====================
SYMBOLS = ["ZEC/USDT", "SOL/USDT"]
TRADE_START_HOUR_GMT = 9   # Heure début trading (GMT = Abidjan)
TRADE_END_HOUR_GMT = 22
TRADE_MINUTE_WINDOW = 2
MIN_CONFLUENCE_SCORE = 85

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
THREE_COMMAS_WEBHOOK_URL = os.getenv("THREE_COMMAS_WEBHOOK_URL")
BOT_ID_ZEC = os.getenv("BOT_ID_ZEC")
BOT_ID_SOL = os.getenv("BOT_ID_SOL")
SECRET_TOKEN_3C = os.getenv("SECRET_TOKEN_3C")

# ===================== TECHNICAL ANALYZER =====================
class TechnicalAnalyzer7:
    """
    Analyse technique avec 7 indicateurs : RSI, MACD, EMA, SMA, Bollinger, ATR, Ichimoku (simplifié)
    """

    def __init__(self, exchange):
        self.exchange = exchange

    def analyze_7_indicators(self, symbol: str, timeframe: str):
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
            closes = [c[4] for c in ohlcv]
            highs = [c[2] for c in ohlcv]
            lows = [c[3] for c in ohlcv]

            # --- RSI ---
            rsi = self._rsi(closes)

            # --- EMA ---
            ema20 = sum(closes[-20:])/20

            # --- SMA ---
            sma50 = sum(closes[-50:])/50

            # --- MACD (simplifié) ---
            ema12 = sum(closes[-12:])/12
            ema26 = sum(closes[-26:])/26
            macd = ema12 - ema26

            # --- Bollinger (simplifié) ---
            sma20 = sum(closes[-20:])/20
            std20 = (sum([(x-sma20)**2 for x in closes[-20:]])/20)**0.5
            upper_band = sma20 + 2*std20
            lower_band = sma20 - 2*std20

            # --- ATR ---
            atr = max(highs[-14:]) - min(lows[-14:])
            atr_percent = atr / closes[-1] * 100

            # --- Ichimoku simplifié ---
            tenkan = (max(highs[-9:]) + min(lows[-9:]))/2
            kijun = (max(highs[-26:]) + min(lows[-26:]))/2
            ichimoku_trend = "bullish" if tenkan > kijun else "bearish"

            # Confluence : score indicateur (placeholder)
            confluence_score = 90

            # Détermination de la tendance globale
            trend_votes = 0
            trend_votes += 1 if rsi>50 else -1
            trend_votes += 1 if macd>0 else -1
            trend_votes += 1 if closes[-1]>ema20 else -1
            trend_votes += 1 if closes[-1]>sma50 else -1
            trend_votes += 1 if closes[-1]>sma20 else -1
            trend_votes += 1 if closes[-1]>upper_band else -1
            trend_votes += 1 if ichimoku_trend=="bullish" else -1
            trend = "bullish" if trend_votes>0 else "bearish"

            return {
                "price": closes[-1],
                "trend": {"direction": trend},
                "volatility": {"atr_percent": atr_percent},
                "confluence_score": confluence_score,
                "indicators": {
                    "RSI": rsi,
                    "MACD": macd,
                    "EMA20": ema20,
                    "SMA50": sma50,
                    "Bollinger_upper": upper_band,
                    "Bollinger_lower": lower_band,
                    "Ichimoku": ichimoku_trend
                }
            }

        except Exception as e:
            logger.error(f"Erreur TechnicalAnalyzer7: {e}")
            return {"price": 0, "trend":{"direction":"neutral"}, "volatility":{"atr_percent":1}, "confluence_score":0}

    def _rsi(self, closes, period=14):
        if len(closes) < period:
            return 50
        deltas = [closes[i+1]-closes[i] for i in range(len(closes)-1)]
        seed = deltas[:period]
        up = sum([x for x in seed if x>0])/period
        down = -sum([x for x in seed if x<0])/period
        return 100 - 100/(1 + up/down if down != 0 else 1)

# ===================== ORDERBOOK =====================
class OrderBookAnalyzer:
    def __init__(self, exchange):
        self.exchange = exchange

    def analyze_orderbook(self, symbol: str):
        try:
            ob = self.exchange.fetch_order_book(symbol)
            imbalance = sum([b[1] for b in ob['bids'][:5]]) - sum([a[1] for a in ob['asks'][:5]])
            pressure = "buy" if imbalance > 0 else "sell"
            return {"imbalance": imbalance, "pressure": pressure}
        except:
            return {"imbalance":0, "pressure":"neutral"}

# ===================== SENTIMENT =====================
class SentimentWhaleAnalyzer:
    def analyze_sentiment_whales(self, symbol: str):
        return {"score":0, "bias":"neutral"}

# ===================== SIGNAL GENERATOR =====================
class CompleteSignalGenerator:
    def __init__(self):
        self.exchange = ccxt.bybit({
            "apiKey": BYBIT_API_KEY,
            "secret": BYBIT_API_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "linear"}
        })
        self.tech = TechnicalAnalyzer7(self.exchange)
        self.orderbook = OrderBookAnalyzer(self.exchange)
        self.sentiment = SentimentWhaleAnalyzer()
        self.active_trades = {}

    def _calculate_price_levels(self, tech):
        atr = tech["volatility"]["atr_percent"]
        entry = tech["price"]
        return {
            "entry_price": entry,
            "tp_percent": round(atr*2,2),
            "sl_percent": round(atr*1,2),
            "trailing_stop": {"enabled": True, "callback_percent": round(atr*0.5,2)}
        }

    def _determine_direction(self, tech, orderbook, sentiment):
        if tech["trend"]["direction"]=="bullish" and orderbook["pressure"]=="buy":
            return "LONG"
        if tech["trend"]["direction"]=="bearish" and orderbook["pressure"]=="sell":
            return "SHORT"
        return "HOLD"

    def send_to_3commas(self, signal):
        symbol_clean = signal["symbol"].replace("/","")
        bot_id = BOT_ID_ZEC if "ZEC" in symbol_clean else BOT_ID_SOL
        action = "buy" if signal["direction"]=="LONG" else "sell"
        payload = {
            "message_type":"bot",
            "bot_id":bot_id,
            "email_token":SECRET_TOKEN_3C,
            "delay_seconds":0,
            "pair": f"USDT_{symbol_clean.replace('USDT','')}",
            "action": action,
            "take_profit": signal["price_levels"]["tp_percent"],
            "stop_loss": signal["price_levels"]["sl_percent"],
            "trailing_enabled": True,
            "trailing_deviation": signal["price_levels"]["trailing_stop"]["callback_percent"]
        }
        try:
            r = requests.post(THREE_COMMAS_WEBHOOK_URL, json=payload, timeout=5)
            logger.info(f"3Commas webhook: {r.status_code} {r.text}")
        except Exception as e:
            logger.error(f"Erreur 3Commas: {e}")

    def _check_retracement_alert(self, symbol, current_price):
        trade = self.active_trades.get(symbol)
        if trade:
            entry = trade["entry_price"]
            direction = trade["direction"]
            profit = round((current_price-entry)/entry*100,2)
            if direction=="LONG" and current_price < entry*0.995:
                logger.warning(f"⚠️ Retracement détecté LONG {symbol}. Profit actuel : {profit}%")
            if direction=="SHORT" and current_price > entry*1.005:
                logger.warning(f"⚠️ Retracement détecté SHORT {symbol}. Profit actuel : {profit}%")

    def generate_complete_signal(self, symbol):
        tech = self.tech.analyze_7_indicators(symbol, "1h")
        ob = self.orderbook.analyze_orderbook(symbol)
        sent = self.sentiment.analyze_sentiment_whales(symbol)
        direction = self._determine_direction(tech, ob, sent)
        price_levels = self._calculate_price_levels(tech)
        current_price = tech["price"]

        self._check_retracement_alert(symbol, current_price)

        now = datetime.now(timezone.utc)
        signal = {
            "symbol": symbol,
            "direction": direction,
            "price_levels": price_levels,
            "confluence_score": tech["confluence_score"],
            "indicators": tech["indicators"]
        }

        if (
            TRADE_START_HOUR_GMT <= now.hour <= TRADE_END_HOUR_GMT
            and direction != "HOLD"
            and tech["confluence_score"] >= MIN_CONFLUENCE_SCORE
            and now.minute <= TRADE_MINUTE_WINDOW
        ):
            logger.info(f"🚀 Signal {symbol} validé {direction}")
            self.send_to_3commas(signal)
            self.active_trades[symbol] = {"direction":direction, "entry_price":current_price}

        return signal

# ===================== FLASK + LOOP =====================
app = Flask(__name__)
engine = CompleteSignalGenerator()

@app.route("/health")
def health():
    return jsonify({"status":"running"}), 200

def trading_loop():
    while True:
        for s in SYMBOLS:
            try:
                engine.generate_complete_signal(s)
            except Exception as e:
                logger.error(f"Erreur loop: {e}")
        time.sleep(30)

if __name__=="__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT",8080))), daemon=True).start()
    trading_loop()        
