#!/usr/bin/env python3
"""
QUANTIS - Assistant de Trading Quantitatif Haute Fréquence
Version: 2.0.0
Auteur: Ingénieur Senior Trading Quantitatif
"""

import os
import json
import time
import asyncio
import threading
from datetime import datetime, time as dt_time, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
import requests
import pandas as pd
import numpy as np
import ccxt
import pandas_ta as ta
from flask import Flask, jsonify
from websockets import connect, WebSocketException
from concurrent.futures import ThreadPoolExecutor
import logging
from logging.handlers import RotatingFileHandler
from enum import Enum

# ===================== CONFIGURATION =====================

class TradingMode(Enum):
    DAY = "day"
    NIGHT = "night"
    SILENT = "silent"

@dataclass
class TradingConfig:
    # Paires à surveiller (max 2)
    PAIR_1: str = os.getenv("PAIR_1", "BTC/USDT")
    PAIR_2: str = os.getenv("PAIR_2", "ETH/USDT")
    
    # Heures de trading
    HEURE_DEBUT: str = os.getenv("HEURE_DEBUT", "09:00")
    HEURE_FIN: str = os.getenv("HEURE_FIN", "22:00")
    ALERTE_NUIT: bool = os.getenv("ALERTE_NUIT", "true").lower() == "true"
    
    # Seuils de trading
    RSI_OVERBOUGHT: int = 70
    RSI_OVERSOLD: int = 30
    FLASH_CRASH_THRESHOLD: float = float(os.getenv("FLASH_CRASH_THRESHOLD", "5.0"))
    WHALE_ALERT_THRESHOLD: float = float(os.getenv("WHALE_ALERT_THRESHOLD", "1000000"))
    
    # APIs
    BYBIT_API_KEY: str = os.getenv("BYBIT_API_KEY", "")
    BYBIT_API_SECRET: str = os.getenv("BYBIT_API_SECRET", "")
    CRYPTOPANIC_API_KEY: str = os.getenv("CRYPTOPANIC_API_KEY", "")
    WHALE_ALERT_API_KEY: str = os.getenv("WHALE_ALERT_API_KEY", "")
    DISCORD_WEBHOOK: str = os.getenv("DISCORD_WEBHOOK", "")
    
    # Paramètres techniques
    ORDERBOOK_DEPTH: int = 50
    VOLUME_SPIKE_MULTIPLIER: float = 3.0
    TRAILING_STOP_PERCENT: float = 2.0
    MIN_CONFLUENCE_SCORE: float = 80.0

# ===================== LOGGING =====================

def setup_logging():
    """Configuration du logging professionnel"""
    logger = logging.getLogger("quantis")
    logger.setLevel(logging.INFO)
    
    # Formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Handler fichier
    file_handler = RotatingFileHandler(
        'quantis.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    # Handler console
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    return logger

logger = setup_logging()

# ===================== APIS EXTERNES =====================

class APIManager:
    """Gestionnaire des APIs externes avec retry et cache"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'QUANTIS-Trading-Bot/2.0'
        })
        self.cache = {}
        self.cache_timeout = 300  # 5 minutes
        
    def get_cached(self, key):
        """Récupération avec cache"""
        if key in self.cache:
            data, timestamp = self.cache[key]
            if time.time() - timestamp < self.cache_timeout:
                return data
        return None
    
    def set_cache(self, key, data):
        """Mise en cache"""
        self.cache[key] = (data, time.time())
    
    def get_cryptopanic_sentiment(self, symbol: str) -> Dict:
        """Récupère le sentiment des news depuis CryptoPanic"""
        try:
            cache_key = f"cryptopanic_{symbol}"
            cached = self.get_cached(cache_key)
            if cached:
                return cached
            
            url = "https://cryptopanic.com/api/v1/posts/"
            params = {
                'auth_token': TradingConfig().CRYPTOPANIC_API_KEY,
                'currencies': symbol.replace('/USDT', ''),
                'public': 'true'
            }
            
            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            # Analyse sentiment
            sentiment = {
                'positive': 0,
                'negative': 0,
                'neutral': 0,
                'total': 0
            }
            
            for post in data.get('results', []):
                votes = post.get('votes', {})
                sentiment['positive'] += votes.get('positive', 0)
                sentiment['negative'] += votes.get('negative', 0)
                sentiment['neutral'] += votes.get('important', 0)
                sentiment['total'] += 1
            
            self.set_cache(cache_key, sentiment)
            return sentiment
            
        except Exception as e:
            logger.error(f"Erreur CryptoPanic pour {symbol}: {e}")
            return {'positive': 0, 'negative': 0, 'neutral': 0, 'total': 0}
    
    def get_whale_alert(self, symbol: str) -> List[Dict]:
        """Récupère les mouvements de baleines"""
        try:
            cache_key = f"whale_{symbol}_{int(time.time() / 60)}"  # Cache par minute
            cached = self.get_cached(cache_key)
            if cached:
                return cached
            
            url = "https://api.whale-alert.io/v1/transactions"
            params = {
                'api_key': TradingConfig().WHALE_ALERT_API_KEY,
                'min_value': TradingConfig().WHALE_ALERT_THRESHOLD,
                'start': int(time.time() - 3600),  # Dernière heure
                'limit': 10
            }
            
            response = self.session.get(url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            transactions = []
            asset = symbol.split('/')[0]
            
            for tx in data.get('transactions', []):
                if tx.get('symbol') == asset:
                    transactions.append({
                        'hash': tx.get('hash', ''),
                        'amount': tx.get('amount', 0),
                        'amount_usd': tx.get('amount_usd', 0),
                        'from': tx.get('from', {}).get('owner_type', ''),
                        'to': tx.get('to', {}).get('owner_type', ''),
                        'timestamp': tx.get('timestamp', 0)
                    })
            
            self.set_cache(cache_key, transactions)
            return transactions
            
        except Exception as e:
            logger.error(f"Erreur Whale Alert pour {symbol}: {e}")
            return []
    
    def send_discord_alert(self, embed_data: Dict, is_emergency: bool = False):
        """Envoie une alerte Discord formatée"""
        try:
            webhook_url = TradingConfig().DISCORD_WEBHOOK
            if not webhook_url:
                logger.warning("URL Discord webhook non configurée")
                return
            
            embed = {
                "title": embed_data.get("title", "QUANTIS Alert"),
                "color": 16711680 if is_emergency else 65280,  # Rouge pour urgence, vert sinon
                "timestamp": datetime.utcnow().isoformat(),
                "fields": embed_data.get("fields", []),
                "footer": {
                    "text": "QUANTIS Trading System v2.0"
                }
            }
            
            payload = {
                "embeds": [embed],
                "username": "QUANTIS AI",
                "avatar_url": "https://cdn-icons-png.flaticon.com/512/2103/2103655.png"
            }
            
            response = self.session.post(webhook_url, json=payload, timeout=10)
            response.raise_for_status()
            logger.info(f"Alerte Discord envoyée: {embed_data.get('title')}")
            
        except Exception as e:
            logger.error(f"Erreur envoi Discord: {e}")

# ===================== ANALYSE TECHNIQUE =====================

class TechnicalAnalyzer:
    """Analyse technique avec pandas_ta"""
    
    def __init__(self):
        self.indicators = {}
        
    def analyze_ohlcv(self, df: pd.DataFrame) -> Dict:
        """Analyse complète des données OHLCV"""
        try:
            close = df['close']
            high = df['high']
            low = df['low']
            volume = df['volume']
            
            # RSI
            rsi = ta.rsi(close, length=14).iloc[-1]
            
            # MACD
            macd = ta.macd(close, fast=12, slow=26, signal=9)
            macd_line = macd['MACD_12_26_9'].iloc[-1]
            signal_line = macd['MACDs_12_26_9'].iloc[-1]
            macd_hist = macd['MACDh_12_26_9'].iloc[-1]
            
            # Bollinger Bands
            bb = ta.bbands(close, length=20, std=2)
            bb_upper = bb['BBU_20_2.0'].iloc[-1]
            bb_lower = bb['BBL_20_2.0'].iloc[-1]
            bb_middle = bb['BBM_20_2.0'].iloc[-1]
            bb_width = (bb_upper - bb_lower) / bb_middle
            
            # Ichimoku Cloud
            ichimoku = ta.ichimoku(high, low, close)
            tenkan = ichimoku['ITS_9'].iloc[-1]
            kijun = ichimoku['IKS_26'].iloc[-1]
            senkou_a = ichimoku['ISA_9'].iloc[-1]
            senkou_b = ichimoku['ISB_26'].iloc[-1]
            chikou = ichimoku['ICS_26'].iloc[-26] if len(close) >= 26 else 0
            
            # Volume analysis
            volume_sma = volume.rolling(20).mean().iloc[-1]
            volume_ratio = volume.iloc[-1] / volume_sma if volume_sma > 0 else 1
            
            return {
                'rsi': float(rsi) if not pd.isna(rsi) else 50.0,
                'macd': {
                    'line': float(macd_line) if not pd.isna(macd_line) else 0.0,
                    'signal': float(signal_line) if not pd.isna(signal_line) else 0.0,
                    'histogram': float(macd_hist) if not pd.isna(macd_hist) else 0.0
                },
                'bollinger': {
                    'upper': float(bb_upper) if not pd.isna(bb_upper) else 0.0,
                    'middle': float(bb_middle) if not pd.isna(bb_middle) else 0.0,
                    'lower': float(bb_lower) if not pd.isna(bb_lower) else 0.0,
                    'width': float(bb_width) if not pd.isna(bb_width) else 0.0,
                    'percent_b': (close.iloc[-1] - bb_lower) / (bb_upper - bb_lower) if bb_upper != bb_lower else 0.5
                },
                'ichimoku': {
                    'tenkan': float(tenkan) if not pd.isna(tenkan) else 0.0,
                    'kijun': float(kijun) if not pd.isna(kijun) else 0.0,
                    'senkou_a': float(senkou_a) if not pd.isna(senkou_a) else 0.0,
                    'senkou_b': float(senkou_b) if not pd.isna(senkou_b) else 0.0,
                    'chikou': float(chikou) if not pd.isna(chikou) else 0.0
                },
                'volume': {
                    'current': float(volume.iloc[-1]),
                    'sma_20': float(volume_sma),
                    'ratio': float(volume_ratio)
                },
                'price': float(close.iloc[-1])
            }
            
        except Exception as e:
            logger.error(f"Erreur analyse technique: {e}")
            return {}

# ===================== SURVEILLANCE ORDERBOOK =====================

class OrderBookMonitor:
    """Surveillance du carnet d'ordres en temps réel"""
    
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.exchange = ccxt.bybit({
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'}
        })
        self.orderbook = {'bids': [], 'asks': []}
        self.last_update = 0
        self.volume_profile = {}
        
    def analyze_orderbook(self) -> Dict:
        """Analyse approfondie du carnet d'ordres"""
        try:
            orderbook = self.exchange.fetch_order_book(
                self.symbol, 
                limit=TradingConfig().ORDERBOOK_DEPTH
            )
            
            bids = pd.DataFrame(orderbook['bids'], columns=['price', 'volume'])
            asks = pd.DataFrame(orderbook['asks'], columns=['price', 'volume'])
            
            # Calcul des métriques
            bid_pressure = bids['volume'].sum()
            ask_pressure = asks['volume'].sum()
            total_pressure = bid_pressure + ask_pressure
            
            # Prix VWAP
            vwap_bid = (bids['price'] * bids['volume']).sum() / bid_pressure if bid_pressure > 0 else 0
            vwap_ask = (asks['price'] * asks['volume']).sum() / ask_pressure if ask_pressure > 0 else 0
            
            # Détection de murs
            bid_wall = bids[bids['volume'] > bids['volume'].quantile(0.9)]
            ask_wall = asks[asks['volume'] > asks['volume'].quantile(0.9)]
            
            # Imbalance
            imbalance = (bid_pressure - ask_pressure) / total_pressure if total_pressure > 0 else 0
            
            return {
                'bid_pressure': float(bid_pressure),
                'ask_pressure': float(ask_pressure),
                'vwap_bid': float(vwap_bid),
                'vwap_ask': float(vwap_ask),
                'spread': float(asks['price'].iloc[0] - bids['price'].iloc[0]),
                'imbalance': float(imbalance),
                'bid_wall': {
                    'count': len(bid_wall),
                    'total_volume': float(bid_wall['volume'].sum()),
                    'avg_price': float(bid_wall['price'].mean()) if len(bid_wall) > 0 else 0
                },
                'ask_wall': {
                    'count': len(ask_wall),
                    'total_volume': float(ask_wall['volume'].sum()),
                    'avg_price': float(ask_wall['price'].mean()) if len(ask_wall) > 0 else 0
                },
                'timestamp': time.time()
            }
            
        except Exception as e:
            logger.error(f"Erreur analyse orderbook {self.symbol}: {e}")
            return {}

# ===================== MOTEUR DE TRADING =====================

class TradingEngine:
    """Moteur de décision de trading"""
    
    def __init__(self):
        self.config = TradingConfig()
        self.api_manager = APIManager()
        self.tech_analyzer = TechnicalAnalyzer()
        self.active_positions = {}
        self.signal_history = []
        
    def calculate_confluence(self, symbol: str, tech_data: Dict, 
                            orderbook_data: Dict, sentiment_data: Dict) -> float:
        """Calcule le score de confluence multi-facteurs"""
        scores = []
        
        # Score technique (40%)
        tech_score = self._calculate_technical_score(tech_data)
        scores.append(tech_score * 0.4)
        
        # Score orderbook (30%)
        orderbook_score = self._calculate_orderbook_score(orderbook_data)
        scores.append(orderbook_score * 0.3)
        
        # Score sentiment (20%)
        sentiment_score = self._calculate_sentiment_score(sentiment_data)
        scores.append(sentiment_score * 0.2)
        
        # Score volume (10%)
        volume_score = self._calculate_volume_score(tech_data)
        scores.append(volume_score * 0.1)
        
        return sum(scores)
    
    def _calculate_technical_score(self, tech_data: Dict) -> float:
        """Calcule le score technique"""
        score = 50.0
        
        # RSI scoring
        rsi = tech_data.get('rsi', 50)
        if rsi < self.config.RSI_OVERSOLD:
            score += 25
        elif rsi > self.config.RSI_OVERBOUGHT:
            score -= 25
        
        # MACD scoring
        macd_hist = tech_data.get('macd', {}).get('histogram', 0)
        if macd_hist > 0:
            score += 15
        else:
            score -= 15
            
        return max(0, min(100, score))
    
    def _calculate_orderbook_score(self, orderbook_data: Dict) -> float:
        """Calcule le score du carnet d'ordres"""
        imbalance = orderbook_data.get('imbalance', 0)
        # Normalise de -1 à 1 vers 0 à 100
        return (imbalance + 1) * 50
    
    def _calculate_sentiment_score(self, sentiment_data: Dict) -> float:
        """Calcule le score de sentiment"""
        if sentiment_data['total'] == 0:
            return 50.0
        
        positive = sentiment_data['positive']
        negative = sentiment_data['negative']
        total = positive + negative
        
        if total == 0:
            return 50.0
        
        return (positive / total) * 100
    
    def _calculate_volume_score(self, tech_data: Dict) -> float:
        """Calcule le score de volume"""
        volume_ratio = tech_data.get('volume', {}).get('ratio', 1)
        if volume_ratio > self.config.VOLUME_SPIKE_MULTIPLIER:
            return 80.0
        elif volume_ratio > 1.5:
            return 60.0
        else:
            return 40.0
    
    def detect_flash_crash(self, symbol: str, current_price: float, 
                          previous_price: float) -> bool:
        """Détecte un flash crash"""
        if previous_price == 0:
            return False
        
        change_percent = abs((current_price - previous_price) / previous_price) * 100
        return change_percent >= self.config.FLASH_CRASH_THRESHOLD
    
    def check_whale_risk(self, symbol: str, whale_data: List[Dict]) -> Tuple[bool, str]:
        """Vérifie le risque lié aux baleines"""
        if not whale_data:
            return False, ""
        
        deposit_volume = sum(tx['amount_usd'] for tx in whale_data 
                           if tx['to'] == 'exchange')
        withdrawal_volume = sum(tx['amount_usd'] for tx in whale_data 
                              if tx['from'] == 'exchange')
        
        if deposit_volume > withdrawal_volume * 3:
            return True, f"Fort afflux de {symbol} sur les exchanges ({deposit_volume:,.0f}$)"
        
        return False, ""
    
    def generate_signal(self, symbol: str, confluence: float, 
                       tech_data: Dict, orderbook_data: Dict) -> Optional[Dict]:
        """Génère un signal de trading"""
        if confluence < self.config.MIN_CONFLUENCE_SCORE:
            return None
        
        direction = self._determine_direction(tech_data, orderbook_data)
        if not direction:
            return None
        
        entry_price = orderbook_data.get('vwap_ask' if direction == 'LONG' else 'vwap_bid', 0)
        stop_loss = self._calculate_stop_loss(direction, entry_price, tech_data)
        take_profit = self._calculate_take_profit(direction, entry_price, tech_data)
        trailing_stop = entry_price * (1 - self.config.TRAILING_STOP_PERCENT/100 
                                     if direction == 'LONG' else 
                                     1 + self.config.TRAILING_STOP_PERCENT/100)
        
        return {
            'symbol': symbol,
            'direction': direction,
            'entry_price': round(entry_price, 4),
            'stop_loss': round(stop_loss, 4),
            'take_profit': round(take_profit, 4),
            'trailing_stop': round(trailing_stop, 4),
            'confluence': round(confluence, 1),
            'timestamp': datetime.now().isoformat(),
            'signal_id': f"{symbol}_{int(time.time())}"
        }
    
    def _determine_direction(self, tech_data: Dict, orderbook_data: Dict) -> Optional[str]:
        """Détermine la direction du trade"""
        rsi = tech_data.get('rsi', 50)
        imbalance = orderbook_data.get('imbalance', 0)
        macd_hist = tech_data.get('macd', {}).get('histogram', 0)
        
        buy_signals = 0
        sell_signals = 0
        
        # RSI
        if rsi < self.config.RSI_OVERSOLD:
            buy_signals += 1
        elif rsi > self.config.RSI_OVERBOUGHT:
            sell_signals += 1
        
        # Orderbook imbalance
        if imbalance > 0.1:
            buy_signals += 1
        elif imbalance < -0.1:
            sell_signals += 1
        
        # MACD
        if macd_hist > 0:
            buy_signals += 1
        elif macd_hist < 0:
            sell_signals += 1
        
        if buy_signals >= 2 and sell_signals == 0:
            return 'LONG'
        elif sell_signals >= 2 and buy_signals == 0:
            return 'SHORT'
        
        return None
    
    def _calculate_stop_loss(self, direction: str, entry: float, 
                            tech_data: Dict) -> float:
        """Calcule le stop loss"""
        bb_lower = tech_data.get('bollinger', {}).get('lower', entry * 0.95)
        bb_upper = tech_data.get('bollinger', {}).get('upper', entry * 1.05)
        
        if direction == 'LONG':
            return bb_lower * 0.995  # 0.5% sous la bande inférieure
        else:
            return bb_upper * 1.005  # 0.5% au-dessus de la bande supérieure
    
    def _calculate_take_profit(self, direction: str, entry: float, 
                              tech_data: Dict) -> float:
        """Calcule le take profit"""
        atr_percent = tech_data.get('bollinger', {}).get('width', 0.02) * 100
        
        if direction == 'LONG':
            # TP basé sur ATR (1.5x à 3x)
            tp_multiplier = 1.5 + (atr_percent / 10)
            return entry * (1 + (tp_multiplier * atr_percent / 100))
        else:
            tp_multiplier = 1.5 + (atr_percent / 10)
            return entry * (1 - (tp_multiplier * atr_percent / 100))

# ===================== GESTIONNAIRE TEMPOREL =====================

class TimeManager:
    """Gestion des plages horaires et modes"""
    
    def __init__(self):
        self.config = TradingConfig()
        self.current_mode = TradingMode.DAY
        self.last_mode_check = time.time()
        
    def parse_time(self, time_str: str) -> dt_time:
        """Parse une chaîne de temps HH:MM"""
        return datetime.strptime(time_str, "%H:%M").time()
    
    def get_current_mode(self) -> TradingMode:
        """Détermine le mode actuel"""
        now = datetime.now()
        current_time = now.time()
        
        start_time = self.parse_time(self.config.HEURE_DEBUT)
        end_time = self.parse_time(self.config.HEURE_FIN)
        
        if start_time <= current_time <= end_time:
            return TradingMode.DAY
        elif self.config.ALERTE_NUIT:
            return TradingMode.NIGHT
        else:
            return TradingMode.SILENT
    
    def should_send_alert(self, movement_percent: float = 0.0) -> bool:
        """Détermine si une alerte doit être envoyée"""
        mode = self.get_current_mode()
        
        if mode == TradingMode.DAY:
            return True
        elif mode == TradingMode.NIGHT and abs(movement_percent) >= 10.0:
            return True
        elif mode == TradingMode.SILENT:
            return False
        
        return False
    
    def is_trading_hours(self) -> bool:
        """Vérifie si on est dans les heures de trading"""
        return self.get_current_mode() == TradingMode.DAY

# ===================== SURVEILLANCE EN TEMPS RÉEL =====================

class RealTimeMonitor:
    """Surveillance en temps réel des marchés"""
    
    def __init__(self):
        self.config = TradingConfig()
        self.trading_engine = TradingEngine()
        self.time_manager = TimeManager()
        self.api_manager = APIManager()
        self.websocket_connections = {}
        self.price_history = {}
        self.executor = ThreadPoolExecutor(max_workers=5)
        self.running = True
        
    def start_monitoring(self, symbol: str):
        """Démarre la surveillance d'une paire"""
        thread = threading.Thread(
            target=self._monitor_symbol,
            args=(symbol,),
            daemon=True
        )
        thread.start()
        logger.info(f"Surveillance démarrée pour {symbol}")
    
    def _monitor_symbol(self, symbol: str):
        """Thread de surveillance pour une paire"""
        exchange = ccxt.bybit({'enableRateLimit': True})
        orderbook_monitor = OrderBookMonitor(symbol)
        tech_analyzer = TechnicalAnalyzer()
        
        previous_price = 0
        last_analysis = 0
        
        while self.running:
            try:
                current_time = time.time()
                
                # Récupération des données
                ohlcv = exchange.fetch_ohlcv(symbol, '5m', limit=100)
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                
                current_price = df['close'].iloc[-1]
                
                # Analyse toutes les 30 secondes minimum
                if current_time - last_analysis > 30:
                    # Analyse technique
                    tech_data = tech_analyzer.analyze_ohlcv(df)
                    
                    # Analyse orderbook
                    orderbook_data = orderbook_monitor.analyze_orderbook()
                    
                    # Sentiment
                    sentiment_data = self.api_manager.get_cryptopanic_sentiment(symbol)
                    
                    # Whale data
                    whale_data = self.api_manager.get_whale_alert(symbol)
                    
                    # Calcul confluence
                    confluence = self.trading_engine.calculate_confluence(
                        symbol, tech_data, orderbook_data, sentiment_data
                    )
                    
                    # Détection flash crash
                    if previous_price > 0:
                        is_flash_crash = self.trading_engine.detect_flash_crash(
                            symbol, current_price, previous_price
                        )
                        
                        if is_flash_crash:
                            self._handle_flash_crash(symbol, current_price, previous_price)
                    
                    # Vérification risque baleine
                    whale_risk, whale_message = self.trading_engine.check_whale_risk(
                        symbol, whale_data
                    )
                    
                    if whale_risk:
                        self._handle_whale_risk(symbol, whale_message)
                    
                    # Génération de signal
                    if confluence >= self.trading_engine.config.MIN_CONFLUENCE_SCORE:
                        signal = self.trading_engine.generate_signal(
                            symbol, confluence, tech_data, orderbook_data
                        )
                        
                        if signal and self.time_manager.should_send_alert():
                            self._send_trading_signal(signal, tech_data, orderbook_data)
                    
                    last_analysis = current_time
                    previous_price = current_price
                
                time.sleep(5)  # Pause de 5 secondes entre les vérifications
                
            except Exception as e:
                logger.error(f"Erreur surveillance {symbol}: {e}")
                time.sleep(10)  # Pause plus longue en cas d'erreur
    
    def _handle_flash_crash(self, symbol: str, current: float, previous: float):
        """Gère un flash crash détecté"""
        change_percent = ((current - previous) / previous) * 100
        
        embed = {
            "title": "⚠️ FLASH CRASH DÉTECTÉ",
            "fields": [
                {"name": "Paire", "value": symbol, "inline": True},
                {"name": "Changement", "value": f"{change_percent:.2f}%", "inline": True},
                {"name": "Prix précédent", "value": f"{previous:.2f}$", "inline": True},
                {"name": "Prix actuel", "value": f"{current:.2f}$", "inline": True},
                {"name": "Action recommandée", "value": "Sortie immédiate conseillée", "inline": False},
                {"name": "Timestamp", "value": datetime.now().strftime("%H:%M:%S"), "inline": True}
            ]
        }
        
        self.api_manager.send_discord_alert(embed, is_emergency=True)
        logger.warning(f"Flash crash {symbol}: {change_percent:.2f}%")
    
    def _handle_whale_risk(self, symbol: str, message: str):
        """Gère un risque baleine"""
        embed = {
            "title": "🐳 ALERTE BALEINE",
            "fields": [
                {"name": "Paire", "value": symbol, "inline": True},
                {"name": "Risque", "value": "Afflux massif sur exchange", "inline": True},
                {"name": "Détails", "value": message, "inline": False},
                {"name": "Recommandation", "value": "Surveillance renforcée - Risque de chute", "inline": False}
            ]
        }
        
        self.api_manager.send_discord_alert(embed, is_emergency=True)
        logger.warning(f"Alerte baleine {symbol}: {message}")
    
    def _send_trading_signal(self, signal: Dict, tech_data: Dict, orderbook_data: Dict):
        """Envoie un signal de trading"""
        direction_emoji = "🟢" if signal['direction'] == 'LONG' else "🔴"
        
        embed = {
            "title": f"{direction_emoji} SIGNAL DE TRADING {direction_emoji}",
            "fields": [
                {"name": "Paire", "value": signal['symbol'], "inline": True},
                {"name": "Direction", "value": signal['direction'], "inline": True},
                {"name": "Confiance", "value": f"{signal['confluence']}%", "inline": True},
                {"name": "🎯 Entrée", "value": f"{signal['entry_price']}$", "inline": True},
                {"name": "🛑 Stop Loss", "value": f"{signal['stop_loss']}$", "inline": True},
                {"name": "💰 Take Profit", "value": f"{signal['take_profit']}$", "inline": True},
                {"name": "📈 Trailing Stop", "value": f"{signal['trailing_stop']}$", "inline": True},
                {"name": "RSI", "value": f"{tech_data.get('rsi', 0):.1f}", "inline": True},
                {"name": "Imbalance", "value": f"{orderbook_data.get('imbalance', 0):.3f}", "inline": True},
                {"name": "ID Signal", "value": signal['signal_id'], "inline": False}
            ]
        }
        
        self.api_manager.send_discord_alert(embed, is_emergency=False)
        logger.info(f"Signal envoyé {signal['symbol']}: {signal['direction']} à {signal['entry_price']}$")
    
    def stop(self):
        """Arrête la surveillance"""
        self.running = False
        self.executor.shutdown(wait=False)

# ===================== SERVEUR WEB FLASK =====================

app = Flask(__name__)
monitor = None

@app.route('/')
def home():
    """Page d'accueil"""
    return jsonify({
        "status": "online",
        "service": "QUANTIS Trading Assistant",
        "version": "2.0.0",
        "timestamp": datetime.now().isoformat(),
        "monitoring": [TradingConfig().PAIR_1, TradingConfig().PAIR_2] if monitor else []
    })

@app.route('/health')
def health():
    """Endpoint de santé pour Render/UptimeRobot"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat()
    })

@app.route('/status')
def status():
    """Statut détaillé du système"""
    if not monitor:
        return jsonify({"error": "Monitor not initialized"}), 500
    
    time_mgr = monitor.time_manager
    config = TradingConfig()
    
    return jsonify({
        "trading_mode": time_mgr.get_current_mode().value,
        "trading_hours": f"{config.HEURE_DEBUT} - {config.HEURE_FIN}",
        "night_alerts": config.ALERTE_NUIT,
        "pairs_monitored": [config.PAIR_1, config.PAIR_2],
        "confluence_threshold": config.MIN_CONFLUENCE_SCORE,
        "flash_crash_threshold": f"{config.FLASH_CRASH_THRESHOLD}%",
        "server_time": datetime.now().strftime("%H:%M:%S"),
        "uptime": time.time() - (monitor.start_time if hasattr(monitor, 'start_time') else time.time())
    })

@app.route('/signal/<symbol>')
def get_signal(symbol: str):
    """Force l'analyse d'une paire"""
    if not monitor:
        return jsonify({"error": "Monitor not initialized"}), 500
    
    try:
        # Simulation d'analyse pour l'API
        exchange = ccxt.bybit({'enableRateLimit': True})
        ohlcv = exchange.fetch_ohlcv(symbol, '5m', limit=100)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        tech_analyzer = TechnicalAnalyzer()
        tech_data = tech_analyzer.analyze_ohlcv(df)
        
        orderbook_monitor = OrderBookMonitor(symbol)
        orderbook_data = orderbook_monitor.analyze_orderbook()
        
        api_manager = APIManager()
        sentiment_data = api_manager.get_cryptopanic_sentiment(symbol)
        
        confluence = monitor.trading_engine.calculate_confluence(
            symbol, tech_data, orderbook_data, sentiment_data
        )
        
        signal = monitor.trading_engine.generate_signal(
            symbol, confluence, tech_data, orderbook_data
        )
        
        return jsonify({
            "symbol": symbol,
            "confluence": confluence,
            "signal": signal,
            "technical": tech_data,
            "timestamp": datetime.now().isoformat()
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ===================== POINT D'ENTRÉE =====================

def main():
    """Fonction principale"""
    global monitor
    
    logger.info("=" * 60)
    logger.info("🚀 DÉMARRAGE DE QUANTIS TRADING ASSISTANT")
    logger.info("=" * 60)
    
    # Vérification configuration
    config = TradingConfig()
    
    if not config.DISCORD_WEBHOOK:
        logger.warning("URL Discord webhook non configurée - pas d'alertes")
    
    if not config.CRYPTOPANIC_API_KEY:
        logger.warning("Clé CryptoPanic non configurée - pas d'analyse sentiment")
    
    if not config.WHALE_ALERT_API_KEY:
        logger.warning("Clé Whale Alert non configurée - pas de détection baleines")
    
    # Initialisation du moniteur
    monitor = RealTimeMonitor()
    monitor.start_time = time.time()
    
    # Démarrage surveillance des paires
    pairs = [config.PAIR_1]
    if config.PAIR_2:
        pairs.append(config.PAIR_2)
    
    for pair in pairs:
        if pair and pair != "NONE":
            monitor.start_monitoring(pair)
    
    # Lancement Flask dans un thread séparé
    flask_thread = threading.Thread(
        target=lambda: app.run(
            host='0.0.0.0',
            port=8080,
            debug=False,
            use_reloader=False
        ),
        daemon=True
    )
    flask_thread.start()
    
    logger.info(f"✅ Surveillance démarrée pour {len(pairs)} paires")
    logger.info(f"🌐 Serveur web démarré sur le port 8080")
    logger.info(f"⏰ Heures de trading: {config.HEURE_DEBUT} - {config.HEURE_FIN}")
    logger.info(f"🌙 Alertes nuit: {'Activées' if config.ALERTE_NUIT else 'Désactivées'}")
    
    # Boucle principale
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Arrêt demandé par l'utilisateur")
    except Exception as e:
        logger.error(f"Erreur inattendue: {e}")
    finally:
        if monitor:
            monitor.stop()
        logger.info("✅ QUANTIS arrêté proprement")

if __name__ == "__main__":
    main()
