import os
import ccxt
import time
import requests
import pandas as pd
import threading
from datetime import datetime
import pytz

# ===================== CONFIGURATION QUANTIS PRO =====================
SYMBOLS = ["ZEC/USDT"] 
TIMEZONE = pytz.timezone("Africa/Abidjan")
START_HOUR = 13
# =====================================================

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
WUNDERTRADE_WEBHOOK = os.getenv("WUNDERTRADE_WEBHOOK_URL")
WHALE_ALERT_API = os.getenv("WHALE_ALERT_API")
CRYPTOPANIC_API = os.getenv("CRYPTOPANIC_API")

# --- DÉCORATEUR DE RECONNEXION AUTO (ANTI-CRASH) ---
def retry_api(func):
    def wrapper(*args, **kwargs):
        for i in range(3):
            try:
                return func(*args, **kwargs)
            except (ccxt.NetworkError, ccxt.ExchangeError, ccxt.RateLimitExceeded) as e:
                print(f"⚠️ Erreur API (Tentative {i+1}/3): {e}")
                time.sleep(5)
        return None
    return wrapper

class QuantisFinal:
    def __init__(self):
        self.validate_environment()
        self.connect_exchange()
        
        self.active_trades = {}
        self.cooldowns = {}
        self.error_count = 0
        self.max_errors = 5
        self.circuit_open = False

    def connect_exchange(self):
        self.exchange = ccxt.binance({
            'apiKey': os.getenv("BINANCE_API_KEY"),
            'secret': os.getenv("BINANCE_API_SECRET"),
            'enableRateLimit': True,
            'options': {'defaultType': 'future', 'adjustForTimeDifference': True}
        })

    def validate_environment(self):
        required = ["BINANCE_API_KEY", "BINANCE_API_SECRET", "WUNDERTRADE_WEBHOOK_URL", "WHALE_ALERT_API", "CRYPTOPANIC_API"]
        missing = [var for var in required if not os.getenv(var)]
        if missing:
            raise EnvironmentError(f"❌ Variables manquantes : {missing}")

    @retry_api
    def get_indicators(self, symbol, timeframe='1d'):
        bars = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
        df = pd.DataFrame(bars, columns=['t','o','h','l','c','v'])
        typical_price = (df['h'] + df['l'] + df['c']) / 3
        df['vwap'] = (typical_price * df['v']).cumsum() / df['v'].cumsum()
        df['tr'] = df[['h','l','c']].apply(lambda x: max(x.iloc[0]-x.iloc[1], abs(x.iloc[0]-x.iloc[2]), abs(x.iloc[1]-x.iloc[2])), axis=1)
        df['atr'] = df['tr'].rolling(14).mean()
        
        impulse = df['c'].iloc[-1] > df['c'].iloc[-2] and df['v'].iloc[-1] > df['v'].iloc[-2]
        direction = "bullish" if df['c'].iloc[-1] > df['vwap'].iloc[-1] else "bearish"
        
        return {
            "price": df['c'].iloc[-1],
            "vwap": df['vwap'].iloc[-1],
            "atr": df['atr'].iloc[-1],
            "impulse": impulse,
            "direction": direction
        }

    # --- SÉCURITÉ FLASH CRASH (RÉGLÉ À 3% POUR ZEC) ---
    @retry_api
    def check_flash_crash(self, symbol):
        bars = self.exchange.fetch_ohlcv(symbol, timeframe='15m', limit=2)
        if len(bars) < 2: return False
        last_open = bars[-1][1]
        current_price = bars[-1][4]
        change = (current_price - last_open) / last_open * 100
        
        direction = self.active_trades[symbol]['dir']
        # ✅ Mis à 3.0 pour encaisser la volatilité du ZEC
        if (direction == "LONG" and change <= -3.0) or (direction == "SHORT" and change >= 3.0):
            return True
        return False

    def run_strategy(self):
        if self.circuit_open:
            time.sleep(300)
            self.circuit_open = False
            return
            
        try:
            now_civ = datetime.now(TIMEZONE)
            
            # --- FERMETURE FIN DE JOURNÉE (23h59) ---
            if now_civ.hour == 23 and now_civ.minute == 59:
                for symbol in list(self.active_trades.keys()):
                    self.do_exit(symbol, self.active_trades[symbol]['entry'], "exit", "⏰ FERMETURE 23H59")
                return

            for symbol in list(self.active_trades.keys()):
                self.manage_active_trade(symbol)

            if now_civ.hour < START_HOUR: return

            for symbol in SYMBOLS:
                if symbol in self.active_trades: continue
                if symbol in self.cooldowns and (time.time() - self.cooldowns[symbol] < 300): continue

                data_1d = self.get_indicators(symbol, '1d')
                if data_1d and data_1d["impulse"]:
                    ob_analysis = self.analyze_order_book(symbol)
                    if data_1d['direction'] == "bullish" and ob_analysis == "buy":
                        self.enter_trade(symbol, data_1d, "LONG")
                    elif data_1d['direction'] == "bearish" and ob_analysis == "sell":
                        self.enter_trade(symbol, data_1d, "SHORT")
                    
        except Exception as e:
            print(f"Erreur Loop: {e}")
            self.error_count += 1
            if self.error_count > self.max_errors:
                self.circuit_open = True
                self.error_count = 0

    def enter_trade(self, symbol, data, side):
        try:
            entry = round(data['price'], 4)
            atr = data['atr']
            # TP Dynamique (ATR * 2)
            tp = entry + (atr * 2.0) if side == "LONG" else entry - (atr * 2.0)
            # SL Dynamique (ATR * 1.5)
            sl = entry - (atr * 1.5) if side == "LONG" else entry + (atr * 1.5)
            
            self.active_trades[symbol] = {
                "dir": side, "entry": entry, "tp": tp, "sl": sl, 
                "ts_mult": 1.5, "partial_done": False
            }
            self.send_to_wunder(symbol, side, entry, tp, sl, atr * 1.5)
            self.send_notif(f"🎯 SIGNAL {side} {symbol} | SL/TP ATR actifs")
        except: pass

    def manage_active_trade(self, symbol):
        trade = self.active_trades[symbol]
        data_now = self.get_indicators(symbol, '1d')
        if not data_now: return
        
        price = data_now['price']
        atr_trail_dist = data_now['atr'] * trade["ts_mult"]

        if self.check_flash_crash(symbol):
            self.do_exit(symbol, price, "exit", "🚨 FLASH CRASH (3%)")
            return

        # --- TRAILING ATR PERMANENT (S'ACTUALISE TOUT LE TEMPS) ---
        if trade['dir'] == "LONG":
            if price - atr_trail_dist > trade["sl"]: trade["sl"] = price - atr_trail_dist
        else:
            if price + atr_trail_dist < trade["sl"]: trade["sl"] = price + atr_trail_dist

        # --- SÉCURISATION +1% ---
        pnl = (price - trade['entry']) / trade['entry'] * 100 if trade['dir']=="LONG" else (trade['entry'] - price) / trade['entry'] * 100
        if pnl >= 1.0 and not trade["partial_done"]:
            self.send_to_wunder(symbol, "partial_exit", price, trade["tp"], trade["sl"], atr_trail_dist, amount="10%")
            if trade['dir'] == "LONG": trade["sl"] = max(trade["sl"], trade['entry'])
            else: trade["sl"] = min(trade["sl"], trade['entry'])
            trade["partial_done"] = True
            self.send_notif(f"💰 +1% sécurisé sur {symbol}")

        # --- SORTIE SL / TP / TRAILING ---
        sl_hit = (trade['dir']=="LONG" and price <= trade["sl"]) or (trade['dir']=="SHORT" and price >= trade["sl"])
        tp_hit = (trade['dir']=="LONG" and price >= trade["tp"]) or (trade['dir']=="SHORT" and price <= trade["tp"])
        
        if sl_hit or tp_hit:
            reason = "🏁 TP ATTEINT" if tp_hit else "🛡️ TRAILING SL TOUCHÉ"
            self.do_exit(symbol, price, "exit", reason)

    def do_exit(self, symbol, price, action, reason):
        trade = self.active_trades[symbol]
        self.send_to_wunder(symbol, action, price, trade["tp"], trade["sl"], 0)
        self.send_notif(f"{reason} ({symbol})")
        if symbol in self.active_trades: del self.active_trades[symbol]
        self.cooldowns[symbol] = time.time()

    @retry_api
    def analyze_order_book(self, symbol):
        ob = self.exchange.fetch_order_book(symbol)
        bids, asks = sum(b[1] for b in ob['bids'][:10]), sum(a[1] for a in ob['asks'][:10])
        return "buy" if bids > asks * 1.2 else "sell" if asks > bids * 1.2 else "neutral"

    def send_notif(self, msg):
        print(msg)
        if DISCORD_WEBHOOK: threading.Thread(target=self._send_discord_thread, args=(msg,)).start()

    def _send_discord_thread(self, msg):
        try: requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=5)
        except: pass

    def send_to_wunder(self, symbol, action, entry, tp, sl, ts, amount="100%"):
        if not WUNDERTRADE_WEBHOOK: return
        threading.Thread(target=self._send_wunder_thread, args=(symbol, action, entry, tp, sl, ts, amount)).start()

    def _send_wunder_thread(self, symbol, action, entry, tp, sl, ts, amount):
        try:
            payload = {
                "action": action.replace("partial_exit","exit"),
                "pair": symbol.replace("/",""),
                "order_type": "market",
                "entry_price": entry,
                "amount": amount,
                "take_profit": round(abs(tp-entry)/entry*100,2) if entry != 0 else 0,
                "stop_loss": round(abs(sl-entry)/entry*100,2) if entry != 0 else 0
            }
            requests.post(WUNDERTRADE_WEBHOOK, json=payload, timeout=10)
        except: pass

# --- DÉMARRAGE ---
quantis = QuantisFinal()
print("🤖 QUANTIS PRO DÉMARRÉ - Mode Ultra-Résilient ZEC 3%")
while True:
    quantis.run_strategy()
    time.sleep(30)
