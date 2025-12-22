#!/usr/bin/env python3
"""
QUANTIS - Trading Bot Professionnel (Version Live Finale)
Cibles : ZEC/USDT & SOL/USDT
Plateforme : Bybit API v5
"""

import os
import time
import threading
import pandas as pd
import numpy as np
import ccxt
import pandas_ta as ta
from flask import Flask, jsonify
import logging
from dataclasses import dataclass
from enum import Enum

# ===================== CONFIGURATION LIVE =====================

@dataclass
class TradingConfig:
    # PAIRES DE TRADING
    PAIR_1: str = "ZEC/USDT"
    PAIR_2: str = "SOL/USDT"
    
    # PARAMÈTRES FINANCIERS
    CAPITAL: float = float(os.getenv("CAPITAL", "300"))
    LEVERAGE: int = int(os.getenv("LEVERAGE", "20"))
    RISK_PERCENT: float = float(os.getenv("RISK_PERCENT", "2"))
    
    # PARAMÈTRES TECHNIQUES (CONFLUENCE)
    RSI_OVERBOUGHT: int = 70
    RSI_OVERSOLD: int = 30
    TP_TREND: float = 5.0  # Take Profit 5%
    SL_DEFAULT: float = 2.0 # Stop Loss 2%
    
    # IDENTIFIANTS API (À configurer sur Render)
    BYBIT_API_KEY: str = os.getenv("BYBIT_API_KEY", "")
    BYBIT_API_SECRET: str = os.getenv("BYBIT_API_SECRET", "")
    USE_TESTNET: bool = os.getenv("USE_TESTNET", "false").lower() == "true"

# ===================== LOGGING =====================

def setup_logging():
    logger = logging.getLogger("quantis")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - QUANTIS - %(levelname)s - %(message)s')
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    return logger

logger = setup_logging()

# ===================== BYBIT V5 MANAGER =====================

class BybitManagerV5:
    def __init__(self, config: TradingConfig):
        self.config = config
        self.exchange = self._init_exchange()
    
    def _init_exchange(self):
        try:
            exchange = ccxt.bybit({
                'apiKey': self.config.BYBIT_API_KEY,
                'secret': self.config.BYBIT_API_SECRET,
                'enableRateLimit': True,
                'options': {'defaultType': 'linear'}
            })
            if self.config.USE_TESTNET:
                exchange.set_sandbox_mode(True)
            
            # INDISPENSABLE : Charge les précisions (décimales) de ZEC et SOL
            exchange.load_markets()
            logger.info("✅ Connexion Bybit V5 établie.")
            return exchange
        except Exception as e:
            logger.critical(f"❌ Erreur Init Bybit: {e}")
            raise

    def calculate_qty(self, symbol: str, entry: float, sl: float) -> float:
        try:
            risk_usd = self.config.CAPITAL * (self.config.RISK_PERCENT / 100)
            diff = abs(entry - sl)
            if diff == 0: return 0
            raw_qty = (risk_usd * self.config.LEVERAGE) / entry
            # Ajuste selon les règles de Bybit (ex: 0.1 ZEC)
            return float(self.exchange.amount_to_precision(symbol, raw_qty))
        except:
            return 0

    def execute_trade(self, symbol: str, side: str, qty: float, entry: float, tp: float, sl: float):
        try:
            # Réglage du levier avant l'ordre
            try: self.exchange.set_leverage(self.config.LEVERAGE, symbol)
            except: pass

            params = {
                'takeProfit': str(tp),
                'stopLoss': str(sl),
                'tpslMode': 'Full',
                'tpOrderType': 'Market',
                'slOrderType': 'Market'
            }
            
            order = self.exchange.create_order(
                symbol=symbol,
                type='limit',
                side=side.lower(),
                amount=qty,
                price=entry,
                params=params
            )
            logger.info(f"🚀 ORDRE LIVE : {side} {qty} {symbol} à {entry} (SL: {sl}, TP: {tp})")
            return order
        except Exception as e:
            logger.error(f"❌ Erreur Execution {symbol}: {e}")
            return None

# ===================== MOTEUR D'ANALYSE =====================

class QuantisEngine:
    def __init__(self):
        self.config = TradingConfig()
        self.manager = BybitManagerV5(self.config)

    def fetch_data_and_signal(self, symbol: str):
        try:
            ohlcv = self.manager.exchange.fetch_ohlcv(symbol, '5m', limit=50)
            df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
            rsi = ta.rsi(df['c'], length=14).iloc[-1]
            price = df['c'].iloc[-1]
            
            direction = None
            if rsi < self.config.RSI_OVERSOLD: direction = 'BUY'
            elif rsi > self.config.RSI_OVERBOUGHT: direction = 'SELL'
            
            if direction:
                # Calcul des niveaux
                sl = price * 0.98 if direction == 'BUY' else price * 1.02
                tp = price * 1.05 if direction == 'BUY' else price * 0.95
                qty = self.manager.calculate_qty(symbol, price, sl)
                
                if qty > 0:
                    self.manager.execute_trade(symbol, direction, qty, price, tp, sl)
        except Exception as e:
            logger.error(f"Analyse {symbol} échouée: {e}")

# ===================== RUNTIME =====================

app = Flask(__name__)
engine = QuantisEngine()

@app.route('/')
def status(): return "QUANTIS LIVE ACTIVE", 200

def run_bot():
    logger.info("🤖 Bot Quantis démarré sur ZEC/USDT et SOL/USDT")
    while True:
        engine.fetch_data_and_signal(engine.config.PAIR_1)
        engine.fetch_data_and_signal(engine.config.PAIR_2)
        time.sleep(60) # Scan toutes les minutes

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
