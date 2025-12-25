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
            'enableRateLimit': True,
            'timeout': 30000  # Timeout de 30s pour éviter les blocages API
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
            if DISCORD_WEBHOOK:
                try: requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=5)
                except: pass
            raise EnvironmentError(msg)

    def get_indicators(self, symbol, timeframe='1d'):
        """
        Calcul des indicateurs :
        - VWAP : Prix moyen pondéré par le volume (Pivot institutionnel)
        - ATR (14) : Utilisé pour le calcul dynamique du TP (ATR*2) et SL (ATR*1.5)
        """
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
        """Vérifie le spread du carnet d'ordres pour éviter les flash-crashs (limite 0.15%)"""
        try:
            ob = self.exchange.fetch_order_book(symbol)
            spread = (ob['asks'][0][0] - ob['bids'][0][0]) / ob['bids'][0][0]
            if spread > 0.0015:
                print(f"⚠️ Sécurité: Spread trop large ({round(spread*100,3)}%)")
                return False
            return True
        except:
            return False

    def analyze_order_book(self, symbol):
        """Analyse la pression acheteuse/vendeuse (seuil 20% d'écart)"""
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
            
            self.error_count = 0 

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
            "partial_done": False,
            "be_protected": False 
        }

        self.send_notif(
            f"🎯 ORDRE LIMITE {side} 1J ({symbol})\n"
            f"Entrée VWAP : {entry}\n"
            f"TP : {round(abs(tp-entry)/entry*100,2)}% | SL : {round(abs(sl-entry)/entry*100,2)}%"
        )

        self.send_to_wunder(symbol, side, entry, tp, sl, ts)

    def exit_trade_with_retracement(self, symbol):
        """
        Gestion des sorties IDENTIQUE avec ajout du Step-Trailing +1.5%/+1%
        """
        trade = self.active_trades[symbol]
        side = trade['dir']
        entry = trade['entry']

        data_1h = self.get_indicators(symbol, '1h')
        if not data_1h: return 
        
        price = data_1h['price']
        vwap = data_1h['vwap']
        buffer = 0.005 

        current_pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
        
        # --- LOGIQUE STEP-TRAILING (MODIFIÉE ICI) ---
        if not trade.get("be_protected") and current_pnl >= 1.5:
            # On verrouille 1% de gain au lieu de 0%
            trade["sl"] = entry * 1.01 if side == "LONG" else entry * 0.99
            trade["be_protected"] = True
            self.send_notif(f"🛡️ STEP-TRAILING ACTIVÉ ({symbol}) : Profit +1% verrouillé (Prix à +1.5%).")

        # --- SORTIE SI LE PRIX TOUCHE LE SL (ENCAISSEMENT DES 1% OU STOP INITIAL) ---
        if (side == "LONG" and price <= trade['sl']) or (side == "SHORT" and price >= trade['sl']):
            self.send_to_wunder(symbol, "exit", entry, trade['tp'], trade['sl'], trade['ts'])
            msg = "💰 ENCAISSEMENT +1%" if trade.get("be_protected") else "🛑 STOP LOSS"
            self.send_notif(f"{msg} ({symbol}) : Position fermée à {price}.")
            del self.active_trades[symbol]
            return

        # --- LOGIQUE DE SORTIE PARTIELLE AU VWAP 1H ---
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
                    f"Profit sécurisé : {round(pnl/2,2)}%\n"
                    f"Reste au break-even (SL au prix d'entrée)."
                )

    def send_notif(self, msg):
        print(msg)
        if DISCORD_WEBHOOK and "http" in DISCORD_WEBHOOK:
            try: requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=5)
            except: pass

    def send_to_wunder(self, symbol, action, entry, tp, sl, ts):
        if not WUNDERTRADE_WEBHOOK:
            return

        wunder_action = action.lower()
        order_type = "limit"

        # --- AMOUNT TOTALEMENT DYNAMIQUE (CONSERVÉ) ---
        try:
            balance_info = self.exchange.fetch_balance()
            usdt_balance = balance_info['total'].get('USDT', 0)

            if wunder_action == "partial_exit":
                amount_usdt = usdt_balance * 0.5
                order_type = "market"
                wunder_action = "exit"
            elif wunder_action == "exit":
                amount_usdt = usdt_balance
                order_type = "market"
            else:
                amount_usdt = usdt_balance * 0.05

            qty = round(amount_usdt / entry, 6)
            amount = qty

        except Exception as e:
            print(f"⚠️ Impossible de calculer amount dynamique: {e}")
            amount = "100%"

        payload = {
            "action": wunder_action,
            "pair": symbol.replace("/", ""),
            "order_type": order_type,
            "entry_price": entry,
            "amount": amount,
            "take_profit": round(abs(tp-entry)/entry*100,2),
            "stop_loss": round(abs(sl-entry)/entry*100,2),
            "trailing_stop": round(ts/entry*100,2)
        }

        for attempt in range(2):
            try:
                r = requests.post(WUNDERTRADE_WEBHOOK, json=payload, timeout=10)
                if r.status_code == 200:
                    return
            except Exception as e:
                print(f"Tentative {attempt+1} échouée pour WunderTrading : {e}")
                time.sleep(2)

# ===================== DÉMARRAGE =====================
quantis = QuantisFinal()
print("✅ Quantis IA Connecté – Futures – Abidjan Time")

while True:
    try:
        quantis.run_strategy()
        time.sleep(30)
    except KeyboardInterrupt:
        print("Arrêt du bot...")
        break
    except Exception as e:
        print(f"Erreur boucle principale : {e}")
        time.sleep(10)
