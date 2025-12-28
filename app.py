import os  
import ccxt  
import time  
import requests  
import pandas as pd  
from datetime import datetime  
import pytz  
  
# ===================== CONFIGURATION QUANTIS PRO =====================  
SYMBOLS = ["ETH/USDT"]  
TIMEZONE = pytz.timezone("Africa/Abidjan")  
START_HOUR = 13  
END_HOUR = 22  
# =====================================================  
  
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")  
WUNDERTRADE_WEBHOOK = os.getenv("WUNDERTRADE_WEBHOOK_URL")  
  
class QuantisFinal:  
    def __init__(self):  
        self.validate_environment()  
        self.exchange = ccxt.binance({  
            'apiKey': os.getenv("BINANCE_API_KEY"),  
            'secret': os.getenv("BINANCE_API_SECRET"),  
            'enableRateLimit': True,  
            'options': {'defaultType': 'future'},  
            'timeout': 30000    
        })  
        self.active_trades = {}  
        self.error_count = 0  
        self.max_errors = 5  
        self.circuit_open = False  
        self.report_sent = False  
  
    def validate_environment(self):  
        required = ["BINANCE_API_KEY", "BINANCE_API_SECRET", "WUNDERTRADE_WEBHOOK_URL"]  
        missing = [var for var in required if not os.getenv(var)]  
        if missing:  
            msg = f"❌ ERREUR CRITIQUE: Variables manquantes : {missing}"  
            print(msg)  
            if DISCORD_WEBHOOK:  
                try: requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=5)  
                except: pass  
            raise EnvironmentError(msg)  
  
    def get_indicators(self, symbol, timeframe='1d'):  
        try:  
            bars = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)  
            df = pd.DataFrame(bars, columns=['t','o','h','l','c','v'])  
            typical_price = (df['h'] + df['l'] + df['c']) / 3  
            df['vwap'] = (typical_price * df['v']).cumsum() / df['v'].cumsum()  
            df['tr'] = df[['h','l','c']].apply(lambda x: max(x[0]-x[1], abs(x[0]-x[2]), abs(x[1]-x[2])), axis=1)  
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

    # --- SÉCURITÉ 1 : FLASH CRASH (15m) ---
    def check_flash_crash(self, symbol):
        try:
            bars = self.exchange.fetch_ohlcv(symbol, timeframe='15m', limit=1)
            if not bars: return False
            open_p, current_p = bars[0][1], bars[0][4]
            change = (current_p - open_p) / open_p * 100
            direction = self.active_trades[symbol]['dir']
            if (direction == "LONG" and change <= -1.5) or (direction == "SHORT" and change >= 1.5):
                return True
        except: return False
        return False

    # --- SÉCURITÉ 2 : GARDIEN DE TENDANCE (1h) ---
    def check_trend_guard(self, symbol):
        try:
            data_1h = self.get_indicators(symbol, timeframe='1h')
            if not data_1h: return False
            rsi_1h = data_1h['rsi']
            direction = self.active_trades[symbol]['dir']
            if (direction == "LONG" and rsi_1h < 35) or (direction == "SHORT" and rsi_1h > 65):
                return True
        except: return False
        return False

    def run_strategy(self):  
        if self.circuit_open:  
            time.sleep(300)  
            self.circuit_open = False  
            return  
        try:  
            now_civ = datetime.now(TIMEZONE)  
            if now_civ.hour == 13 and not self.report_sent:  
                data_1d = self.get_indicators(SYMBOLS[0], '1d')  
                self.send_notif(f"📊 **BILAN 13H00**\nBougie : {data_1d['direction'].upper()}\nPrix : {data_1d['price']}")  
                self.report_sent = True  
            if now_civ.hour != 13: self.report_sent = False  

            for symbol in SYMBOLS:  
                if symbol in self.active_trades:  
                    self.exit_trade(symbol)  
                    continue  

                if not (START_HOUR <= now_civ.hour < END_HOUR): continue  

                data_1d = self.get_indicators(symbol, '1d')  
                if not data_1d: continue  

                # Correction ici : Suppression du data_1d en trop
                if data_1d['direction'] == "bullish" and self.analyze_order_book(symbol) == "buy":  
                    self.enter_trade(symbol, data_1d, "LONG")  
                elif data_1d['direction'] == "bearish" and self.analyze_order_book(symbol) == "sell":  
                    self.enter_trade(symbol, data_1d, "SHORT")  
        except Exception as e:  
            print(f"Erreur : {e}")

    def enter_trade(self, symbol, data, side):  
        entry = round(data['price'],4)  
        tp = entry + data['atr']*2.0 if side=="LONG" else entry - data['atr']*2.0  
        sl = entry - data['atr']*1.5 if side=="LONG" else entry + data['atr']*1.5  
        self.active_trades[symbol] = {"dir": side, "entry": entry, "tp": tp, "sl": sl, "ts": data['atr']*0.5, "partial_done": False, "be_protected": False}  
        self.send_notif(f"🎯 ORDRE {side} ({symbol})\nEntrée : {entry}\nTP : {round(abs(tp-entry)/entry*100,2)}% | SL : {round(abs(sl-entry)/entry*100,2)}%")  
        self.send_to_wunder(symbol, side, entry, tp, sl, self.active_trades[symbol]['ts'])  

    def exit_trade(self, symbol):  
        trade = self.active_trades[symbol]  
        data_now = self.get_indicators(symbol,'1d')  
        if not data_now: return  
        price, current_rsi = data_now['price'], data_now['rsi']  
        pnl = (price-trade['entry'])/trade['entry']*100 if trade['dir']=="LONG" else (trade['entry']-price)/trade['entry']*100  

        # --- SÉCURITÉS ACTIVÉES ---
        if self.check_flash_crash(symbol) or self.check_trend_guard(symbol):
            self.send_to_wunder(symbol,"exit",price,trade["tp"],trade["sl"],trade["ts"])
            self.send_notif(f"🚨 SÉCURITÉ (Flash/Trend) ({symbol}) → Sortie d'urgence.")
            del self.active_trades[symbol]; return  

        # --- LOGIQUE NORMALE ---
        if pnl >= 1.5 and not trade["partial_done"]:  
            self.send_to_wunder(symbol,"partial_exit",price,trade["tp"],trade["sl"],trade["ts"])  
            trade["sl"] = trade['entry']*1.005 if trade['dir']=="LONG" else trade['entry']*0.995  
            trade["partial_done"] = trade["be_protected"] = True  
            self.send_notif(f"💰 PROFIT PARTIEL ({symbol}) → +1.5% encaissé.")  
            return  

        if trade["partial_done"] and ((trade['dir']=="LONG" and current_rsi >= 80) or (trade['dir']=="SHORT" and current_rsi <= 20)):  
            self.send_to_wunder(symbol,"exit",price,trade["tp"],trade["sl"],trade["ts"])  
            self.send_notif(f"📈 Sortie RSI ({symbol}) → RSI={round(current_rsi,2)}") # Correction guillemet faite
            del self.active_trades[symbol]; return  

        if (trade["be_protected"] and ((trade['dir']=="LONG" and price<=trade["sl"]) or (trade['dir']=="SHORT" and price>=trade["sl"]))) or \
           ((trade['dir']=="LONG" and price>=trade["tp"]) or (trade['dir']=="SHORT" and price<=trade["tp"])):
            self.send_to_wunder(symbol,"exit",price,trade["tp"],trade["sl"],trade["ts"])
            self.send_notif(f"🏁 TRADE FINI ({symbol})"); del self.active_trades[symbol]

    def analyze_order_book(self, symbol):  
        try:  
            ob = self.exchange.fetch_order_book(symbol)  
            bids, asks = sum(b[1] for b in ob['bids'][:10]), sum(a[1] for a in ob['asks'][:10])  
            return "buy" if bids > asks*1.2 else "sell" if asks > bids*1.2 else "neutral"  
        except: return "neutral"  

    def send_notif(self,msg):  
        print(msg)  
        if DISCORD_WEBHOOK: requests.post(DISCORD_WEBHOOK,json={"content":msg},timeout=5)  

    def send_to_wunder(self,symbol,action,entry,tp,sl,ts):  
        if not WUNDERTRADE_WEBHOOK: return  
        payload = {"action": action.replace("partial_exit","exit"), "pair": symbol.replace("/",""), "order_type": "market" if "exit" in action else "limit", "entry_price": entry, "amount": "50%" if "partial" in action else "100%", "leverage":1, "take_profit": round(abs(tp-entry)/entry*100,2), "stop_loss": round(abs(sl-entry)/entry*100,2)}  
        requests.post(WUNDERTRADE_WEBHOOK,json=payload,timeout=10)  

quantis = QuantisFinal()  
while True:  
    quantis.run_strategy()  
    time.sleep(30)
