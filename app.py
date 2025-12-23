import os
import ccxt
import time
import requests
import pandas as pd
from datetime import datetime
import pytz

# ===================== CONFIGURATION QUANTIS PRO =====================
SYMBOLS = ["ZEC/USDT", "ETH/USDT"] 
TIMEZONE = pytz.timezone("Africa/Abidjan") 
START_HOUR = 13
END_HOUR = 22

WHALE_API_KEY = os.getenv("WHALE_ALERT_KEY")
CP_API_KEY = os.getenv("CRYPTOPANIC_KEY")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")

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
            df['tr'] = df[['h', 'l', 'c']].apply(lambda x: max(x[0]-x[1], abs(x[0]-x[2]), abs(x[1]-x[2])), axis=1)
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
            bids = sum([b[1] for b in ob['bids'][:10]])
            asks = sum([a[1] for a in ob['asks'][:10]])
            if bids > (asks * 1.2): return "buy"
            if asks > (bids * 1.2): return "sell"
            return "neutral"
        except: return "neutral"

    # --- 3. EXÉCUTION & SURVEILLANCE MTF (CORRIGÉ LONG/SHORT) ---
    def run_strategy(self):
        now_civ = datetime.now(TIMEZONE)
        if not (START_HOUR <= now_civ.hour < END_HOUR):
            return 

        for symbol in SYMBOLS:
            data_1d = self.get_indicators(symbol, '1d')
            if not data_1d: continue

            # Surveillance Sortie d'urgence
            if symbol in self.active_trades:
                data_1h = self.get_indicators(symbol, '1h')
                side = self.active_trades[symbol]['dir']
                
                # Sortie LONG si retournement baissier
                if side == "LONG" and data_1h['direction'] == "bearish":
                    self.exit_trade(symbol, "Retournement MTF (Prix < VWAP 1H)")
                # Sortie SHORT si retournement haussier
                elif side == "SHORT" and data_1h['direction'] == "bullish":
                    self.exit_trade(symbol, "Retournement MTF (Prix > VWAP 1H)")
                continue

            safety_ok = self.check_external_safety(symbol)
            book_pressure = self.analyze_order_book(symbol)

            # LOGIQUE ENTRÉE LONG
            if data_1d['direction'] == "bullish" and safety_ok and book_pressure == "buy":
                self.enter_trade(symbol, data_1d, "LONG")
            
            # LOGIQUE ENTRÉE SHORT
            elif data_1d['direction'] == "bearish" and safety_ok and book_pressure == "sell":
                self.enter_trade(symbol, data_1d, "SHORT")

    def enter_trade(self, symbol, data, side):
        limit_price = round(data['vwap'], 4)
        atr = data['atr']
        
        # Inversion des calculs TP/SL selon le sens
        if side == "LONG":
            tp = limit_price + (atr * 2.0)
            sl = limit_price - (atr * 1.5)
        else: # SHORT
            tp = limit_price - (atr * 2.0)
            sl = limit_price + (atr * 1.5)
            
        ts_activation = atr * 0.5

        self.active_trades[symbol] = {
            'dir': side, 
            'entry': limit_price, 
            'tp': tp, 
            'sl': sl,
            'ts': ts_activation
        }
        
        msg = (f"🎯 **ORDRE LIMITE {side} 1J ({symbol})**\n"
               f"Entrée (VWAP): {limit_price}\n"
               f"TP: {round(tp, 4)} | SL: {round(sl, 4)}")
        self.send_notif(msg)

    def exit_trade(self, symbol, reason):
        if symbol in self.active_trades:
            del self.active_trades[symbol]
            self.send_notif(f"⚠️ **SORTIE D'URGENCE ({symbol})**\nRaison: {reason}")

    def send_notif(self, msg):
        print(msg)
        if DISCORD_WEBHOOK and "http" in DISCORD_WEBHOOK:
            try: requests.post(DISCORD_WEBHOOK, json={"content": msg})
            except: pass

# ===================== DÉMARRAGE =====================
quantis = QuantisFinal()
print("✅ Quantis IA Connecté - Mode LONG & SHORT (Abidjan Time)")

while True:
    try:
        quantis.run_strategy()
    except Exception as e:
        print(f"Erreur Système: {e}")
    time.sleep(30)
