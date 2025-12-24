import os
import ccxt
import time
import requests
import pandas as pd
from datetime import datetime
import pytz

# ===================== CONFIGURATION QUANTIS PRO =====================
# Choisis ici UNE SEULE paire à la fois (FUTURES)

# 1️⃣ ETHUSDT FUTURES
SYMBOLS = ["ETH/USDT"]

# 2️⃣ SOLUSDT FUTURES
# SYMBOLS = ["SOL/USDT"]

# 3️⃣ ZECUSDT FUTURES
# SYMBOLS = ["ZEC/USDT"]

TIMEZONE = pytz.timezone("Africa/Abidjan")
START_HOUR = 13
END_HOUR = 22
# =====================================================

WHALE_API_KEY = os.getenv("WHALE_ALERT_KEY")
CP_API_KEY = os.getenv("CRYPTOPANIC_KEY")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
WUNDERTRADE_WEBHOOK = os.getenv("WUNDERTRADE_WEBHOOK_URL")

class QuantisFinal:
    def __init__(self):
        self.exchange = ccxt.bybit({
            'apiKey': os.getenv("BYBIT_API_KEY"),
            'secret': os.getenv("BYBIT_API_SECRET"),
            'enableRateLimit': True
        })
        self.active_trades = {}

    def get_indicators(self, symbol, timeframe='1d'):
        try:
            bars = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
            df = pd.DataFrame(bars, columns=['t', 'o', 'h', 'l', 'c', 'v'])

            typical_price = (df['h'] + df['l'] + df['c']) / 3
            df['vwap'] = (typical_price * df['v']).cumsum() / df['v'].cumsum()

            df['tr'] = df[['h', 'l', 'c']].apply(
                lambda x: max(
                    x[0] - x[1],
                    abs(x[0] - x[2]),
                    abs(x[1] - x[2])
                ), axis=1
            )
            df['atr'] = df['tr'].rolling(14).mean()

            return {
                "price": df['c'].iloc[-1],
                "vwap": df['vwap'].iloc[-1],
                "atr": df['atr'].iloc[-1],
                "direction": "bullish" if df['c'].iloc[-1] > df['vwap'].iloc[-1] else "bearish"
            }
        except Exception as e:
            print(f"Erreur indicateurs {symbol} [{timeframe}]: {e}")
            return None

    def check_external_safety(self, symbol):
        whale_risk = "safe"
        sentiment = "positive"
        return whale_risk == "safe" and sentiment == "positive"

    def analyze_order_book(self, symbol):
        try:
            ob = self.exchange.fetch_order_book(symbol)
            bids = sum(b[1] for b in ob['bids'][:10])
            asks = sum(a[1] for a in ob['asks'][:10])

            if bids > asks * 1.2:
                return "buy"
            if asks > bids * 1.2:
                return "sell"
            return "neutral"
        except:
            return "neutral"

    def run_strategy(self):
        now_civ = datetime.now(TIMEZONE)
        if not (START_HOUR <= now_civ.hour < END_HOUR):
            return

        for symbol in SYMBOLS:
            data_1d = self.get_indicators(symbol, '1d')
            if not data_1d:
                continue

            if symbol in self.active_trades:
                self.exit_trade_with_retracement(symbol)
                continue

            safety_ok = self.check_external_safety(symbol)
            book_pressure = self.analyze_order_book(symbol)

            if data_1d['direction'] == "bullish" and safety_ok and book_pressure == "buy":
                self.enter_trade(symbol, data_1d, "LONG")

            elif data_1d['direction'] == "bearish" and safety_ok and book_pressure == "sell":
                self.enter_trade(symbol, data_1d, "SHORT")

    def enter_trade(self, symbol, data, side):
        entry = round(data['vwap'], 4)
        atr = data['atr']

        if side == "LONG":
            tp = entry + atr * 2.0
            sl = entry - atr * 1.5
        else:
            tp = entry - atr * 2.0
            sl = entry + atr * 1.5

        ts = atr * 0.5

        self.active_trades[symbol] = {
            "dir": side,
            "entry": entry,
            "tp": tp,
            "sl": sl,
            "ts": ts
        }

        self.send_notif(
            f"🎯 ORDRE {side} 1J ({symbol})\n"
            f"Entrée VWAP: {entry}\n"
            f"TP: {round(abs(tp-entry)/entry*100,2)}% | "
            f"SL: {round(abs(sl-entry)/entry*100,2)}% | "
            f"TS: {round(ts/entry*100,2)}%"
        )

        self.send_to_wunder(symbol, side, entry, tp, sl, ts)

    def exit_trade_with_retracement(self, symbol):
        trade = self.active_trades[symbol]
        side = trade['dir']
        entry = trade['entry']

        data_1h = self.get_indicators(symbol, '1h')
        price = data_1h['price']
        vwap = data_1h['vwap']

        buffer = 0.002

        if side == "LONG" and price > vwap * (1 - buffer):
            return
        if side == "SHORT" and price < vwap * (1 + buffer):
            return

        pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
        pnl = round(pnl, 2)

        # Signal de sortie partielle envoyé ici
        self.send_to_wunder(symbol, "partial_exit", entry, trade['tp'], trade['sl'], trade['ts'])

        trade['sl'] = entry

        self.send_notif(
            f"💰 SORTIE PARTIELLE ({symbol})\n"
            f"Profit sécurisé: {round(pnl/2,2)}%\n"
            f"SL placé à break-even"
        )

    def send_notif(self, msg):
        print(msg)
        if DISCORD_WEBHOOK and "http" in DISCORD_WEBHOOK:
            try:
                requests.post(DISCORD_WEBHOOK, json={"content": msg})
            except:
                pass

    def send_to_wunder(self, symbol, action, entry, tp, sl, ts):
        if not WUNDERTRADE_WEBHOOK:
            return

        # --- MODIFICATION POUR LES 50% ---
        wunder_action = action.lower()
        amount_pct = 100  # Par défaut, on ferme tout
        
        # Si c'est un retracement, on dit explicitement à WunderTrading de ne vendre que 50%
        if wunder_action == "partial_exit":
            wunder_action = "exit" 
            amount_pct = 50       
        # --------------------------------

        payload = {
            "action": wunder_action,
            "pair": symbol.replace("/", ""),
            "order_type": "limit" if action.lower() not in ["exit", "partial_exit"] else "market",
            "entry_price": entry,
            "amount_pct": amount_pct,  # Indication explicite de la quantité à vendre
            "take_profit": round(abs(tp-entry)/entry*100,2),
            "stop_loss": round(abs(sl-entry)/entry*100,2),
            "trailing_stop": round(ts/entry*100,2)
        }

        try:
            requests.post(WUNDERTRADE_WEBHOOK, json=payload, timeout=5)
        except Exception as e:
            print(f"Erreur WunderTrading: {e}")

# ===================== DÉMARRAGE =====================
quantis = QuantisFinal()
print("✅ Quantis IA Connecté – Futures – Abidjan Time")

while True:
    try:
        quantis.run_strategy()
    except Exception as e:
        print(f"Erreur Système: {e}")
    time.sleep(30)
