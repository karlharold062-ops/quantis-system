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
        # --- CORRECTION 1: Validation Sécurité ---
        self.validate_environment()
        
        self.exchange = ccxt.bybit({
            'apiKey': os.getenv("BYBIT_API_KEY"),
            'secret': os.getenv("BYBIT_API_SECRET"),
            'enableRateLimit': True
        })
        self.active_trades = {}
        
        # --- CORRECTION 2: Paramètres Circuit Breaker ---
        self.error_count = 0
        self.max_errors = 5
        self.circuit_open = False

    def validate_environment(self):
        """Vérifie la présence des clés essentielles avant de démarrer"""
        required = ["BYBIT_API_KEY", "BYBIT_API_SECRET", "WUNDERTRADE_WEBHOOK"]
        missing = [var for var in required if not os.getenv(var)]
        if missing:
            msg = f"❌ ERREUR CRITIQUE: Variables manquantes : {missing}"
            print(msg)
            # On envoie une notif si possible avant de crash
            if DISCORD_WEBHOOK:
                try: requests.post(DISCORD_WEBHOOK, json={"content": msg})
                except: pass
            raise EnvironmentError(msg)

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
        """Vérifie le spread du carnet d'ordres pour éviter les flash-crashs"""
        try:
            ob = self.exchange.fetch_order_book(symbol)
            spread = (ob['asks'][0][0] - ob['bids'][0][0]) / ob['bids'][0][0]
            # Si le spread est > 0.15%, le marché est trop instable (illiquide)
            if spread > 0.0015:
                print(f"⚠️ Sécurité: Spread trop large ({round(spread*100,3)}%)")
                return False
            return True
        except:
            return False

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
        # --- CORRECTION 3: Logique Circuit Breaker ---
        if self.circuit_open:
            print("⛔ CIRCUIT BREAKER ACTIVÉ - Pause de 5 min suite à erreurs répétées.")
            time.sleep(300)
            self.circuit_open = False
            self.error_count = 0
            return

        try:
            now_civ = datetime.now(TIMEZONE)
            can_open_new = (START_HOUR <= now_civ.hour < END_HOUR)

            for symbol in SYMBOLS:
                if symbol in self.active_trades:
                    self.exit_trade_with_retracement(symbol)
                    continue

                if not can_open_new:
                    continue

                data_1d = self.get_indicators(symbol, '1d')
                if not data_1d:
                    continue

                safety_ok = self.check_external_safety(symbol)
                book_pressure = self.analyze_order_book(symbol)

                if data_1d['direction'] == "bullish" and safety_ok and book_pressure == "buy":
                    self.enter_trade(symbol, data_1d, "LONG")

                elif data_1d['direction'] == "bearish" and safety_ok and book_pressure == "sell":
                    self.enter_trade(symbol, data_1d, "SHORT")
            
            self.error_count = 0 # Reset si tout se passe bien

        except Exception as e:
            self.error_count += 1
            print(f"⚠️ Erreur #{self.error_count}: {e}")
            if self.error_count >= self.max_errors:
                self.circuit_open = True
                self.send_notif("🚨 CIRCUIT BREAKER ACTIVÉ - Erreurs critiques détectées.")

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
            "ts": ts,
            "partial_done": False
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
        if not data_1h: return 
        
        price = data_1h['price']
        vwap = data_1h['vwap']
        buffer = 0.005 

        # On ne traite la sortie partielle que si elle n'est pas déjà faite
        if not trade.get("partial_done"):
            should_exit = False
            
            if side == "LONG" and price < vwap * (1 - buffer):
                should_exit = True
            elif side == "SHORT" and price > vwap * (1 + buffer):
                should_exit = True

            if should_exit:
                pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
                
                self.send_to_wunder(symbol, "partial_exit", entry, trade['tp'], trade['sl'], trade['ts'])

                trade["partial_done"] = True
                trade["sl"] = entry 

                self.send_notif(
                    f"💰 SORTIE PARTIELLE ({symbol})\n"
                    f"Profit sécurisé: {round(pnl/2,2)}%\n"
                    f"Le reste est protégé (SL au break-even)."
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

        wunder_action = action.lower()
        
        payload = {
            "pair": symbol.replace("/", ""),
            "order_type": "limit",
            "entry_price": entry,
            "take_profit": round(abs(tp-entry)/entry*100,2),
            "stop_loss": round(abs(sl-entry)/entry*100,2),
            "trailing_stop": round(ts/entry*100,2)
        }

        if wunder_action == "partial_exit":
            payload["action"] = "exit"
            payload["order_type"] = "market"
            payload["amount"] = "50%" 
        elif wunder_action == "exit":
            payload["action"] = "exit"
            payload["order_type"] = "market"
            payload["amount"] = "100%"
        else:
            payload["action"] = wunder_action
            payload["amount"] = "100%"

        try:
            requests.post(WUNDERTRADE_WEBHOOK, json=payload, timeout=5)
        except Exception as e:
            print(f"Erreur WunderTrading: {e}")

# ===================== DÉMARRAGE =====================
quantis = QuantisFinal()
print("✅ Quantis IA Connecté – Futures – Abidjan Time")

while True:
    quantis.run_strategy()
    time.sleep(30)
