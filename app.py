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
        self.report_sent = False # Pour le bilan de 13h

    def validate_environment(self):
        """Vérifie la présence des clés essentielles avant de démarrer"""
        required = ["BYBIT_API_KEY", "BYBIT_API_SECRET", "WUNDERTRADE_WEBHOOK_URL"]
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
        - RSI (14) : Ajouté pour filtrer les sorties et éviter les respirations
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

            # --- CALCUL DU RSI (Option 1) ---
            delta = df['c'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            df['rsi'] = 100 - (100 / (1 + rs))

            return {
                "price": df['c'].iloc[-1],
                "vwap": df['vwap'].iloc[-1],
                "atr": df['atr'].iloc[-1],
                "rsi": df['rsi'].iloc[-1],
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

            # --- LOGIQUE BILAN 13H ---
            if now_civ.hour == 13 and not self.report_sent:
                data_1d = self.get_indicators(SYMBOLS[0], '1d')
                msg = f"📊 **BILAN 13H00**\nBougie du jour : {data_1d['direction'].upper()}\nPrix : {data_1d['price']}\nPrêt pour analyse de session."
                self.send_notif(msg)
                self.report_sent = True
            
            if now_civ.hour != 13:
                self.report_sent = False

            can_open_new = (START_HOUR <= now_civ.hour < END_HOUR)

            for symbol in SYMBOLS:
                if symbol in self.active_trades:
                    # Ajout de l'heure pour la sortie à la clôture
                    self.exit_trade_with_retracement(symbol, now_civ)
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
        entry = round(data['price'], 4)
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
            f"🎯 ORDRE {side} ({symbol})\n"
            f"Entrée : {entry}\n"
            f"TP : {round(abs(tp-entry)/entry*100,2)}% | SL : {round(abs(sl-entry)/entry*100,2)}%"
        )

        self.send_to_wunder(symbol, side, entry, tp, sl, ts)

    def exit_trade_with_retracement(self, symbol, now_civ):
        """
        LOGIQUE DE SORTIE :
        - Sortie filtrée par RSI + Clôture à la minute 59 (Option 1 & 3).
        - Fermeture à 21h59 si trade encore ouvert.
        - À +1.5% : Sortie 50% immédiate.
        - Sécurisation du reste à +0.5%.
        """
        trade = self.active_trades[symbol]
        side = trade['dir']
        entry = trade['entry']

        # 1️⃣ ANALYSE DU SIGNAL ACTUEL (MTF)
        data_now = self.get_indicators(symbol, '1h')
        if not data_now:
            return

        price = data_now['price']
        current_dir = data_now['direction']
        current_rsi = data_now['rsi']
        pnl = (price - entry) / entry * 100 if side == "LONG" else (entry - price) / entry * 100

        # --- 🚨 NOUVEAU : SORTIE FILTRÉE PAR RSI ET CLÔTURE (OPTION 1 & 3) ---
        # Le bot ne vérifie l'inversion de signal qu'à la 59ème minute pour éviter les mèches
        if now_civ.minute == 59:
            if side == "LONG" and current_dir == "bearish" and current_rsi < 45:
                self.send_to_wunder(symbol, "exit", entry, trade["tp"], trade["sl"], trade["ts"])
                self.send_notif(f"🔄 SIGNAL INVERSÉ + RSI ({symbol}) → Sortie au marché : {round(pnl,2)}%")
                del self.active_trades[symbol]
                return
            elif side == "SHORT" and current_dir == "bullish" and current_rsi > 55:
                self.send_to_wunder(symbol, "exit", entry, trade["tp"], trade["sl"], trade["ts"])
                self.send_notif(f"🔄 SIGNAL INVERSÉ + RSI ({symbol}) → Sortie au marché : {round(pnl,2)}%")
                del self.active_trades[symbol]
                return

        # --- FERMETURE À 21H59 ---
        if now_civ.hour == 21 and now_civ.minute == 59:
            self.send_to_wunder(symbol, "exit", entry, trade["tp"], trade["sl"], trade["ts"])
            self.send_notif(f"⏰ FIN DE SESSION ({symbol}) → Sortie finale : {round(pnl,2)}%")
            del self.active_trades[symbol]
            return

        # --- SORTIE PARTIELLE À +1.5% ---
        if pnl >= 1.5 and not trade["partial_done"]:
            self.send_to_wunder(symbol, "partial_exit", entry, trade["tp"], trade["sl"], trade["ts"])
            trade["sl"] = entry * 1.005 if side == "LONG" else entry * 0.995
            trade["partial_done"] = True
            trade["be_protected"] = True
            self.send_notif(f"💰 PROFIT PARTIEL ({symbol}) → +1.5% encaissé, reste sécurisé.")
            return

        # --- PROTECTION SL SÉCURISÉ ---
        if trade["be_protected"]:
            if (side == "LONG" and price <= trade["sl"]) or (side == "SHORT" and price >= trade["sl"]):
                self.send_to_wunder(symbol, "exit", entry, trade["tp"], trade["sl"], trade["ts"])
                self.send_notif(f"✅ SÉCURITÉ TOUCHÉE ({symbol}) → Sortie à +0.5%")
                del self.active_trades[symbol]
                return

        # --- TP FINAL 2xATR ---
        if (side == "LONG" and price >= trade["tp"]) or (side == "SHORT" and price <= trade["tp"]):
            self.send_to_wunder(symbol, "exit", entry, trade["tp"], trade["sl"], trade["ts"])
            self.send_notif(f"🚀 OBJECTIF ATTEINT ({symbol}) → TP Final Validé")
            del self.active_trades[symbol]

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

        try:
            # Pour WunderTrading, on utilise désormais le montant dynamique défini dans l'interface du bot
            if wunder_action == "partial_exit":
                amount = "50%" # Demande de fermer 50% de la position actuelle
                order_type = "market"
                wunder_action = "exit"
            elif wunder_action == "exit":
                amount = "100%" # Demande de fermer la totalité de la position
                order_type = "market"
            else:
                # Pour l'entrée, on laisse WunderTrading utiliser le montant défini dans sa config
                amount = "100%" 

        except Exception as e:
            amount = "100%"

        payload = {
            "action": wunder_action,
            "pair": symbol.replace("/", ""),
            "order_type": order_type,
            "entry_price": entry,
            "amount": amount,
            "leverage": 1, # Laissé à 1 par défaut, WunderTrading appliquera le levier configuré dans le bot
            "post_only": True if order_type == "limit" else False,
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
