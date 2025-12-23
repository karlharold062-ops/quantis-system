import os
import ccxt
import time
import requests
import pandas as pd
from datetime import datetime
import pytz

# ===================== CONFIGURATION QUANTIS PRO =====================
# Choisis ici les paires à surveiller
# 1 paire seule : ZEC
# SYMBOLS = ["ZEC/USDT"]
# 1 paire seule : ETH
# SYMBOLS = ["ETH/USDT"]
# 2 paires : ZEC + ETH
SYMBOLS = ["ZEC/USDT", "ETH/USDT"]

TIMEZONE = pytz.timezone("Africa/Abidjan")
START_HOUR = 13
END_HOUR = 22

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
                lambda x: max(x[0]-x[1], abs(x[0]-x[2]), abs(x[1]-x[2])), axis=1
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
            bids = sum([b[1] for b in ob['bids'][:10]])
            asks = sum([a[1] for a in ob['asks'][:10]])
            if bids > (asks * 1.2): return "buy"
            if asks > (bids * 1.2): return "sell"
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
                data_1h = self.get_indicators(symbol, '1h')
                side = self.active_trades[symbol]['dir']

                if side == "LONG" and data_1h['direction'] == "bearish":
                    self.exit_trade(symbol, "Retournement MTF (Prix < VWAP 1H)")
                elif side == "SHORT" and data_1h['direction'] == "bullish":
                    self.exit_trade(symbol, "Retournement MTF (Prix > VWAP 1H)")
                continue

            safety_ok = self.check_external_safety(symbol)
            book_pressure = self.analyze_order_book(symbol)

            if data_1d['direction'] == "bullish" and safety_ok and book_pressure == "buy":
                self.enter_trade(symbol, data_1d, "LONG")
            elif data_1d['direction'] == "bearish" and safety_ok and book_pressure == "sell":
                self.enter_trade(symbol, data_1d, "SHORT")

    def enter_trade(self, symbol, data, side):
        limit_price = round(data['vwap'], 4)
        atr = data['atr']

        if side == "LONG":
            tp = limit_price + (atr * 2.0)
            sl = limit_price - (atr * 1.5)
        else:
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

        tp_pct = round(abs(tp - limit_price) / limit_price * 100, 2)
        sl_pct = round(abs(sl - limit_price) / limit_price * 100, 2)
        ts_pct = round(ts_activation / limit_price * 100, 2)

        msg = (
            f"🎯 **ORDRE LIMITE {side} 1J ({symbol})**\n"
            f"Entrée (VWAP): {limit_price}\n"
            f"TP: {tp_pct}% | SL: {sl_pct}% | TS: {ts_pct}%"
        )
        self.send_notif(msg)
        self.send_to_wunder(symbol, side, limit_price, tp, sl, ts_activation)

    def exit_trade(self, symbol, reason):
        if symbol in self.active_trades:
            trade = self.active_trades[symbol]
            self.send_to_wunder(symbol, "exit", trade['entry'], trade['tp'], trade['sl'], trade['ts'])
            del self.active_trades[symbol]
            self.send_notif(f"⚠️ **SORTIE D'URGENCE ({symbol})**\nRaison: {reason}")

    # ================== AJOUT : SORTIE RETRACEMENT ==================
    def exit_trade_market_capture(self, symbol, reason):
        trade = self.active_trades[symbol]
        current_price = self.get_indicators(symbol, '1h')['price']
        entry = trade['entry']
        side = trade['dir']

        if side == "LONG":
            pnl_pct = round((current_price - entry) / entry * 100, 2)
        else:
            pnl_pct = round((entry - current_price) / entry * 100, 2)

        self.send_to_wunder(
            symbol,
            "exit",
            entry,
            trade['tp'],
            trade['sl'],
            trade['ts']
        )

        del self.active_trades[symbol]

        self.send_notif(
            f"💰 **SORTIE RETRACEMENT ({symbol})**\n"
            f"Raison: {reason}\n"
            f"Profit marché: {pnl_pct}%"
        )

    def exit_trade_with_retracement(self, symbol):
        """
        Sortie intelligente avec :
        1. Filtre de clôture sur la bougie 1H
        2. Buffer de sécurité autour du VWAP
        3. Sortie partielle pour sécuriser profit
        """
        if symbol not in self.active_trades:
            return

        trade = self.active_trades[symbol]
        side = trade['dir']
        entry = trade['entry']

        # --- 1. Filtre de clôture (1H) ---
        data_1h = self.get_indicators(symbol, '1h')
        price_close = data_1h['price']
        vwap_1h = data_1h['vwap']

        # --- 2. Buffer de sécurité ---
        buffer = 0.002  # 0.2%
        if side == "LONG" and price_close > vwap_1h * (1 - buffer):
            return
        if side == "SHORT" and price_close < vwap_1h * (1 + buffer):
            return

        # --- 3. Sortie partielle ---
        pnl_pct_full = round((price_close - entry) / entry * 100, 2) if side == "LONG" else round((entry - price_close) / entry * 100, 2)
        pnl_pct_partial = pnl_pct_full / 2  # 50% sécurisés

        self.send_to_wunder(
            symbol,
            "partial_exit",
            entry,
            trade['tp'],
            trade['sl'],
            trade['ts']
        )

        # Stop loss de la position restante à break-even
        trade['sl'] = entry

        self.send_notif(
            f"💰 **SORTIE PARTIELLE ({symbol})**\n"
            f"Prix actuel: {price_close}\n"
            f"Profit sécurisé: {pnl_pct_partial}%\n"
            f"Stop de la position restante placé à break-even"
        )
    # ==========================================================

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

        tp_pct = round(abs(tp - entry) / entry * 100, 2)
        sl_pct = round(abs(sl - entry) / entry * 100, 2)
        ts_pct = round(ts / entry * 100, 2)

        payload = {
            "action": action.lower(),
            "pair": symbol.replace("/", ""),
            "order_type": "limit" if action.lower() != "exit" else "market",
            "entry_price": entry,
            "take_profit": tp_pct,
            "stop_loss": sl_pct,
            "trailing_stop": ts_pct
        }

        try:
            requests.post(WUNDERTRADE_WEBHOOK, json=payload, timeout=5)
        except Exception as e:
            print(f"Erreur WunderTrading: {e}")

# ===================== DÉMARRAGE =====================
quantis = QuantisFinal()
print("✅ Quantis IA Connecté - Mode LONG & SHORT (Abidjan Time)")

while True:
    try:
        quantis.run_strategy()
    except Exception as e:
        print(f"Erreur Système: {e}")
    time.sleep(30)
