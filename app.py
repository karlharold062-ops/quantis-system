import os
import ccxt
import time
import requests
import pandas as pd
import threading
from datetime import datetime
import pytz

# ===================== CONFIGURATION QUANTIS PRO =====================
SYMBOLS = ["ETH/USDT"]  # Paire Futures sur MEXC
TIMEZONE = pytz.timezone("Africa/Abidjan")
START_HOUR = 13
END_HOUR = 22
# =====================================================

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
WUNDERTRADE_WEBHOOK = os.getenv("WUNDERTRADE_WEBHOOK_URL")
WHALE_ALERT_API = os.getenv("WHALE_ALERT_API")
CRYPTOPANIC_API = os.getenv("CRYPTOPANIC_API")

class QuantisFinal:
    def __init__(self):
        self.validate_environment()

        # --- CONNEXION MEXC (adaptée de Code 1) ---
        self.exchange = ccxt.mexc({
            'apiKey': os.getenv("MEXC_API_KEY"),
            'secret': os.getenv("MEXC_API_SECRET"),
            'enableRateLimit': True,
            'options': {'defaultType': 'swap', 'adjustForTimeDifference': True}
        })
        
        self.active_trades = {}
        self.cooldowns = {}
        self.error_count = 0
        self.max_errors = 5
        self.circuit_open = False
        self.report_sent = False

    def validate_environment(self):
        required = ["MEXC_API_KEY", "MEXC_API_SECRET", "WUNDERTRADE_WEBHOOK_URL", "WHALE_ALERT_API", "CRYPTOPANIC_API"]
        missing = [var for var in required if not os.getenv(var)]
        if missing:
            msg = f"❌ ERREUR CRITIQUE: Variables manquantes : {missing}"
            print(msg)
            threading.Thread(target=self._send_discord_thread, args=(msg,)).start()
            raise EnvironmentError(msg)

    # --- INDICATEURS TECHNIQUES ---
    def get_indicators(self, symbol, timeframe='1d'):
        try:
            bars = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
            df = pd.DataFrame(bars, columns=['t','o','h','l','c','v'])
            typical_price = (df['h'] + df['l'] + df['c']) / 3
            df['vwap'] = (typical_price * df['v']).cumsum() / df['v'].cumsum()
            df['tr'] = df[['h','l','c']].apply(lambda x: max(x.iloc[0]-x.iloc[1], abs(x.iloc[0]-x.iloc[2]), abs(x.iloc[1]-x.iloc[2])), axis=1)
            df['atr'] = df['tr'].rolling(14).mean()
            delta = df['c'].diff()
            gain = (delta.where(delta>0,0)).rolling(14).mean()
            loss = (-delta.where(delta<0,0)).rolling(14).mean()
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

    # --- DONNÉES EXTERNES ---
    def get_whale_signal(self):
        try:
            url = f"https://api.whale-alert.io/v1/transactions?api_key={WHALE_ALERT_API}&min_value=1000000"
            resp = requests.get(url, timeout=5).json()
            if "transactions" in resp and len(resp["transactions"]) > 0:
                total_inflow = sum(tx['amount_usd'] for tx in resp['transactions'] if tx['to']['owner_type']=="exchange")
                total_outflow = sum(tx['amount_usd'] for tx in resp['transactions'] if tx['from']['owner_type']=="exchange")
                return "bullish" if total_inflow < total_outflow else "bearish"
            return "neutral"
        except: return "neutral"

    def get_cryptopanic_signal(self):
        try:
            url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_API}&public=true"
            resp = requests.get(url, timeout=5).json()
            bullish = sum(1 for post in resp.get("results",[]) if post['title'].lower().count("bull")>0)
            bearish = sum(1 for post in resp.get("results",[]) if post['title'].lower().count("bear")>0)
            if bullish > bearish: return "bullish"
            if bearish > bullish: return "bearish"
            return "neutral"
        except: return "neutral"

    # --- SÉCURITÉS ---
    def check_flash_crash(self, symbol):
        try:
            bars = self.exchange.fetch_ohlcv(symbol, timeframe='15m', limit=1)
            if not bars: return False
            open_p, current_p = bars[0][1], bars[0][4]
            change = (current_p - open_p) / open_p * 100
            if symbol not in self.active_trades: return False
            direction = self.active_trades[symbol]['dir']
            if (direction == "LONG" and change <= -1.5) or (direction == "SHORT" and change >= 1.5):
                return True
        except: return False
        return False

    def check_trend_guard(self, symbol):
        try:
            data_1h = self.get_indicators(symbol, timeframe='1h')
            if not data_1h: return False
            rsi_1h = data_1h['rsi']
            if symbol not in self.active_trades: return False
            direction = self.active_trades[symbol]['dir']
            if (direction == "LONG" and rsi_1h < 35) or (direction == "SHORT" and rsi_1h > 65):
                return True
        except: return False
        return False

    # --- STRATÉGIE ---
    def run_strategy(self):
        if self.circuit_open:
            time.sleep(300)
            self.circuit_open = False
            return
        try:
            now_civ = datetime.now(TIMEZONE)
            
            if now_civ.hour == 13 and not self.report_sent:
                data_1d = self.get_indicators(SYMBOLS[0], '1d')
                if data_1d:
                    self.send_notif(f"📊 **BILAN 13H00**\nBougie : {data_1d['direction'].upper()}\nPrix : {data_1d['price']}")
                    self.report_sent = True
            if now_civ.hour != 13: self.report_sent = False

            for symbol in list(self.active_trades.keys()):
                self.manage_active_trade(symbol)

            if not (START_HOUR <= now_civ.hour < END_HOUR): return

            whale_sig = self.get_whale_signal()
            news_sig = self.get_cryptopanic_signal()

            for symbol in SYMBOLS:
                if symbol in self.active_trades: continue
                if symbol in self.cooldowns and (time.time() - self.cooldowns[symbol] < 300): continue

                data_1d = self.get_indicators(symbol, '1d')
                if not data_1d: continue

                final_signal = data_1d['direction']
                if whale_sig != "neutral": final_signal = whale_sig
                if news_sig != "neutral": final_signal = news_sig

                ob_analysis = self.analyze_order_book(symbol)
                
                if final_signal == "bullish" and ob_analysis == "buy":
                    self.enter_trade(symbol, data_1d, "LONG")
                elif final_signal == "bearish" and ob_analysis == "sell":
                    self.enter_trade(symbol, data_1d, "SHORT")
                    
        except Exception as e:
            print(f"Erreur Loop: {e}")
            self.error_count += 1
            if self.error_count > self.max_errors:
                self.circuit_open = True
                self.send_notif("⚠️ Trop d'erreurs, pause de 5 minutes.")
                self.error_count = 0

    # --- ENVOI DE SIGNALS (PAS D'ORDRES DIRECTS) ---
    def enter_trade(self, symbol, data, side):
        try:
            entry = round(data['price'], 4)
            tp = entry + data['atr'] * 2.0 if side == "LONG" else entry - data['atr'] * 2.0
            sl = entry - data['atr'] * 1.5 if side == "LONG" else entry + data['atr'] * 1.5
            
            self.active_trades[symbol] = {
                "dir": side, "entry": entry, "tp": tp, "sl": sl, 
                "ts": data['atr'] * 1.0, "partial_done": False, 
                "be_protected": False, "status": "ACTIVE"
            }

            self.send_to_wunder(symbol, side, entry, tp, sl, self.active_trades[symbol]['ts'])
            self.send_notif(f"🎯 SIGNAL {side} ({symbol}) - Entrée : {entry}")

        except Exception as e:
            print(f"Erreur signal {symbol}: {e}")
            if symbol in self.active_trades: del self.active_trades[symbol]

    def manage_active_trade(self, symbol):
        trade = self.active_trades[symbol]

        data_now = self.get_indicators(symbol, '1d')
        if not data_now: return
        price, current_rsi = data_now['price'], data_now['rsi']

        pnl = (price - trade['entry']) / trade['entry'] * 100 if trade['dir']=="LONG" else (trade['entry'] - price) / trade['entry'] * 100

        # Trailing ATR
        atr_trail = data_now['atr'] * 1.0
        if trade['dir']=="LONG": trade['sl'] = max(trade['sl'], price - atr_trail)
        else: trade['sl'] = min(trade['sl'], price + atr_trail)

        if self.check_flash_crash(symbol) or self.check_trend_guard(symbol):
            self.do_exit(symbol, price, "exit", "🚨 SÉCURITÉ (Flash/Trend)")
            return

        # Partial exit
        if pnl >= 1.5 and not trade["partial_done"]:
            self.send_to_wunder(symbol, "partial_exit", price, trade["tp"], trade["sl"], trade["ts"])
            trade["sl"] = trade['entry'] * 1.005 if trade['dir']=="LONG" else trade['entry'] * 0.995
            trade["partial_done"] = True
            trade["be_protected"] = True
            self.send_notif(f"💰 PROFIT PARTIEL ({symbol}) → +1.5% encaissé. SL déplacé à BE.")
            return

        # RSI exit
        if trade["partial_done"] and ((trade['dir']=="LONG" and current_rsi >= 80) or (trade['dir']=="SHORT" and current_rsi <= 20)):
            self.do_exit(symbol, price, "exit", f"📈 Sortie RSI ({round(current_rsi,2)})")
            return

        # TP/SL exit
        sl_hit = (trade['dir']=="LONG" and price <= trade["sl"]) or (trade['dir']=="SHORT" and price >= trade["sl"])
        tp_hit = (trade['dir']=="LONG" and price >= trade["tp"]) or (trade['dir']=="SHORT" and price <= trade["tp"])
        if sl_hit or tp_hit:
            reason = "🏁 TP TOUCHÉ" if tp_hit else "🛑 SL TOUCHÉ"
            self.do_exit(symbol, price, "exit", reason)

    def do_exit(self, symbol, price, action, reason):
        trade = self.active_trades[symbol]
        self.send_to_wunder(symbol, action, price, trade["tp"], trade["sl"], trade["ts"])
        self.send_notif(f"{reason} ({symbol})")
        del self.active_trades[symbol]
        self.cooldowns[symbol] = time.time()

    def analyze_order_book(self, symbol):
        try:
            ob = self.exchange.fetch_order_book(symbol)
            bids, asks = sum(b[1] for b in ob['bids'][:10]), sum(a[1] for a in ob['asks'][:10])
            return "buy" if bids > asks * 1.2 else "sell" if asks > bids * 1.2 else "neutral"
        except: return "neutral"

    # --- NOTIFICATIONS ---
    def send_notif(self, msg):
        print(msg)
        if DISCORD_WEBHOOK: threading.Thread(target=self._send_discord_thread, args=(msg,)).start()

    def _send_discord_thread(self, msg):
        try: requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=5)
        except: pass

    def send_to_wunder(self, symbol, action, entry, tp, sl, ts):
        if not WUNDERTRADE_WEBHOOK: return
        threading.Thread(target=self._send_wunder_thread, args=(symbol, action, entry, tp, sl, ts)).start()

    def _send_wunder_thread(self, symbol, action, entry, tp, sl, ts):
        try:
            payload = {
                "action": action.replace("partial_exit","exit"),
                "pair": symbol.replace("/",""),
                "order_type": "market",
                "entry_price": entry,
                "amount": "100%",
                "take_profit": round(abs(tp-entry)/entry*100,2),
                "stop_loss": round(abs(sl-entry)/entry*100,2)
            }
            requests.post(WUNDERTRADE_WEBHOOK, json=payload, timeout=10)
        except Exception as e: print(f"Erreur Wunder: {e}")

# --- DÉMARRAGE BOT ---
quantis = QuantisFinal()
print("🤖 QUANTIS PRO DÉMARRÉ - Mode Signaux uniquement sur MEXC")
while True:
    quantis.run_strategy()
    time.sleep(30)
