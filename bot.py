import asyncio
import io
import json
import logging
import os
import random
import statistics
import time
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent
os.environ.setdefault("MPLCONFIGDIR", str(BASE_DIR / ".matplotlib"))
Path(os.environ["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
import requests
import websockets
from aiogram import Bot, Dispatcher, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BufferedInputFile


def load_local_env(env_path: Path) -> None:
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))
    except FileNotFoundError:
        return


load_local_env(BASE_DIR / ".env")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALERT_CHAT_ID = int(os.getenv("ALERT_CHAT_ID", "-1003867089540"))
ALERT_TOPIC_ID = int(os.getenv("ALERT_TOPIC_ID", "17135"))
ALERT_THRESHOLD = int(os.getenv("ALERT_THRESHOLD", "500000"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "8") or "8")
BINANCE_BASE_URL = os.getenv("BINANCE_BASE_URL", "https://fapi.binance.com").rstrip("/")
BYBIT_BASE_URL = os.getenv("BYBIT_BASE_URL", "https://api.bybit.com").rstrip("/")
DROP_PENDING_UPDATES = os.getenv("DROP_PENDING_UPDATES", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
BINANCE_PROXY_URLS_RAW = os.getenv("BINANCE_PROXY_URLS", "")
BINANCE_PROXY_URL = (
    os.getenv("BINANCE_PROXY_URL")
    or os.getenv("HTTPS_PROXY")
    or os.getenv("HTTP_PROXY")
    or os.getenv("ALL_PROXY")
    or ""
).strip()
BINANCE_DIRECT_FALLBACK = os.getenv("BINANCE_DIRECT_FALLBACK", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
PUBLIC_PROXY_FALLBACK = os.getenv("PUBLIC_PROXY_FALLBACK", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

WATCHLIST = [
    "BTC",
    "ETH",
    "SOL",
    "BNB",
    "XRP",
    "DOGE",
    "ADA",
    "UNI",
    "FIL",
    "DOT",
    "LTC",
    "LINK",
    "XLM",
    "ATOM",
    "ZIL",
]

LEVERAGE_DIST = {
    2: 0.05,
    3: 0.08,
    5: 0.15,
    10: 0.25,
    20: 0.22,
    50: 0.15,
    100: 0.10,
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# WebSocket liquidation data storage
_liq_ws_data = {
    "by_symbol": {},  # {symbol: [{price, side, amount, time}, ...]}
    "total_1h": {},  # {symbol: {"long": sum, "short": sum}}
}

# Multi-exchange OI data storage
_multi_oi_cache = {}  # {symbol: {"binance": oi, "bybit": oi, "okx": oi, "bitget": oi, "total": oi}}

OKX_BASE_URL = "https://www.okx.com"
BITGET_BASE_URL = "https://api.bitget.com"
COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"

# Multi-exchange Funding Rate cache: {symbol: {"binance": rate, "bybit": rate, "okx": rate, "bitget": rate, "aggregated": rate, "sources": count}}
_multi_funding_cache = {}

# Historical liquidations cache
_hist_liq_cache = {}  # {symbol: [{time, long_liq, short_liq}, ...]}


async def liquidation_ws_listener():
    """WebSocket listener for Binance liquidation stream (free)"""
    ws_url = "wss://fstream.binance.com/ws/!forceOrder@arr"
    
    while True:
        try:
            logger.info("Connecting to Binance liquidation WebSocket...")
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                logger.info("Liquidation WebSocket connected")
                
                async for message in ws:
                    try:
                        data = json.loads(message)
                        if "o" in data:
                            liq = data["o"]
                            symbol = liq.get("s", "")
                            side = liq.get("S", "").lower()  # BUY=short liquidation, SELL=long liquidation
                            amount = float(liq.get("l", 0))  # liquidated volume in USD
                            price = float(liq.get("p", 0))
                            
                            # Normalize side
                            liq_side = "short" if side == "buy" else "long"  # Buy liquidation = shorts got rekt
                            
                            # Store data
                            if symbol not in _liq_ws_data["by_symbol"]:
                                _liq_ws_data["by_symbol"][symbol] = []
                            
                            _liq_ws_data["by_symbol"][symbol].append({
                                "price": price,
                                "side": liq_side,
                                "amount": amount,
                                "time": time.time()
                            })
                            
                            # Keep only last 100 per symbol
                            _liq_ws_data["by_symbol"][symbol] = _liq_ws_data["by_symbol"][symbol][-100:]
                            
                            # Update 1h totals
                            if symbol not in _liq_ws_data["total_1h"]:
                                _liq_ws_data["total_1h"][symbol] = {"long": 0, "short": 0}
                            _liq_ws_data["total_1h"][symbol][liq_side] += amount
                            
                            # Alert on large liquidation
                            if amount >= 500000:  # $500k+
                                logger.info(f"Large liquidation: {symbol} {liq_side} ${amount:,.0f} at ${price:,.2f}")
                    except Exception as e:
                        logger.debug(f"WS message parse error: {e}")
                        
        except Exception as e:
            logger.warning(f"Liquidation WebSocket error: {e}")
            await asyncio.sleep(5)  # Reconnect delay


def get_okx_oi(sym: str, price: float) -> float:
    """Get Open Interest from OKX (free API)"""
    try:
        # OKX uses different symbol format: BTC-USDT-SWAP
        okx_sym = f"{sym.replace('USDT', '')}-USDT-SWAP"
        url = f"{OKX_BASE_URL}/api/v5/public/open-interest"
        params = {"instType": "SWAP", "instId": okx_sym}
        
        r = http.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            if data.get("data") and len(data["data"]) > 0:
                oi_contracts = float(data["data"][0].get("oi", 0))
                # OKX returns OI in contracts, need to convert to USD
                # Contract size varies, approximate with price
                return oi_contracts * price * 0.01  # Approximate conversion
    except Exception as e:
        logger.debug(f"OKX OI error for {sym}: {e}")
    return 0


def get_bitget_oi(sym: str, price: float) -> float:
    """Get Open Interest from Bitget (free API)"""
    try:
        # Bitget symbol format: BTCUSDT_UMCBL
        bg_sym = f"{sym}_UMCBL"
        url = f"{BITGET_BASE_URL}/api/v2/mix/market/open-interest"
        params = {"symbol": bg_sym, "productType": "USDT-FUTURES"}
        
        r = http.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            if data.get("data") and data.get("code") == "00000":
                oi = float(data["data"].get("openInterest", 0))
                return oi * price
    except Exception as e:
        logger.debug(f"Bitget OI error for {sym}: {e}")
    return 0


def get_multi_exchange_oi(sym: str, price: float) -> float:
    """Aggregate OI from all available exchanges for better accuracy"""
    global _multi_oi_cache
    
    # Get OI from each exchange
    binance_oi = 0
    try:
        data = exchange_get("Binance", BINANCE_BASE_URL, "/fapi/v1/openInterest", {"symbol": sym})
        if data and "openInterest" in data:
            binance_oi = float(data["openInterest"]) * price
    except Exception:
        pass
    
    bybit_oi = 0
    try:
        d = exchange_get("Bybit", BYBIT_BASE_URL, "/v5/market/open-interest",
                        {"category": "linear", "symbol": sym, "intervalTime": "1h", "limit": 1})
        if d and d.get("result") and d["result"].get("list"):
            bybit_oi = float(d["result"]["list"][0]["openInterest"]) * price
    except Exception:
        pass
    
    okx_oi = get_okx_oi(sym, price)
    bitget_oi = get_bitget_oi(sym, price)
    
    # Store in cache
    _multi_oi_cache[sym] = {
        "binance": binance_oi,
        "bybit": bybit_oi,
        "okx": okx_oi,
        "bitget": bitget_oi,
        "total": binance_oi + bybit_oi + okx_oi + bitget_oi,
        "sources": sum([1 for x in [binance_oi, bybit_oi, okx_oi, bitget_oi] if x > 0])
    }
    
    total_oi = _multi_oi_cache[sym]["total"]
    
    # If we got data from multiple sources, use it
    if total_oi > 0:
        logger.info(f"Multi-exchange OI for {sym}: {total_oi:,.0f} USD from {_multi_oi_cache[sym]['sources']} sources")
        return total_oi
    
    # Fallback
    return price * 1_000_000


def get_binance_funding(sym: str) -> float:
    """Get funding rate from Binance (1h, 8h, or current)"""
    try:
        # Try premiumIndex for current funding rate
        data = exchange_get("Binance", BINANCE_BASE_URL, "/fapi/v1/premiumIndex", {"symbol": sym})
        if data and "lastFundingRate" in data:
            return float(data["lastFundingRate"]) * 100  # Convert to percentage
    except Exception as e:
        logger.debug(f"Binance funding error for {sym}: {e}")
    return None


def get_bybit_funding(sym: str) -> float:
    """Get funding rate from Bybit"""
    try:
        d = exchange_get("Bybit", BYBIT_BASE_URL, "/v5/market/tickers",
                        {"category": "linear", "symbol": sym})
        if d and d.get("result") and d["result"].get("list"):
            funding = d["result"]["list"][0].get("fundingRate")
            if funding:
                return float(funding) * 100
    except Exception as e:
        logger.debug(f"Bybit funding error for {sym}: {e}")
    return None


def get_okx_funding(sym: str) -> float:
    """Get funding rate from OKX"""
    try:
        okx_sym = f"{sym.replace('USDT', '')}-USDT-SWAP"
        url = f"{OKX_BASE_URL}/api/v5/public/funding-rate"
        params = {"instId": okx_sym}
        r = http.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            if data.get("data") and len(data["data"]) > 0:
                return float(data["data"][0].get("fundingRate", 0)) * 100
    except Exception as e:
        logger.debug(f"OKX funding error for {sym}: {e}")
    return None


def get_bitget_funding(sym: str) -> float:
    """Get funding rate from Bitget"""
    try:
        bg_sym = f"{sym}_UMCBL"
        url = f"{BITGET_BASE_URL}/api/v2/mix/market/funding-rate"
        params = {"symbol": bg_sym, "productType": "USDT-FUTURES"}
        r = http.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            if data.get("data") and data.get("code") == "00000":
                return float(data["data"].get("fundingRate", 0)) * 100
    except Exception as e:
        logger.debug(f"Bitget funding error for {sym}: {e}")
    return None


def aggregate_with_outlier_detection(values: list, threshold: float = 2.0) -> tuple:
    """
    Aggregate multiple values with outlier detection using standard deviation.
    Returns: (aggregated_value, num_sources, method_used)
    """
    if not values:
        return None, 0, "no_data"
    
    if len(values) == 1:
        return values[0], 1, "single"
    
    # Remove exact duplicates
    unique_values = list(set(values))
    if len(unique_values) == 1:
        return unique_values[0], len(values), "identical"
    
    # Calculate mean and std
    mean = statistics.mean(unique_values)
    if len(unique_values) >= 3:
        try:
            std = statistics.stdev(unique_values)
            # Filter outliers (values beyond threshold * std from mean)
            filtered = [v for v in unique_values if abs(v - mean) <= threshold * std]
            if filtered:
                # Use median for robustness
                median_val = statistics.median(filtered)
                return median_val, len(filtered), "median_filtered"
        except:
            pass
    
    # Fallback to median of all values
    median_val = statistics.median(unique_values)
    return median_val, len(unique_values), "median"


def get_multi_exchange_funding(sym: str) -> dict:
    """Get aggregated funding rate from all available exchanges with outlier detection"""
    global _multi_funding_cache
    
    # Get funding from each exchange
    sources = {}
    
    binance_funding = get_binance_funding(sym)
    if binance_funding is not None:
        sources["binance"] = binance_funding
    
    bybit_funding = get_bybit_funding(sym)
    if bybit_funding is not None:
        sources["bybit"] = bybit_funding
    
    okx_funding = get_okx_funding(sym)
    if okx_funding is not None:
        sources["okx"] = okx_funding
    
    bitget_funding = get_bitget_funding(sym)
    if bitget_funding is not None:
        sources["bitget"] = bitget_funding
    
    # Aggregate with outlier detection
    all_values = list(sources.values())
    aggregated, num_sources, method = aggregate_with_outlier_detection(all_values)
    
    # Store in cache
    _multi_funding_cache[sym] = {
        **sources,
        "aggregated": aggregated,
        "sources": num_sources,
        "method": method,
        "timestamp": time.time()
    }
    
    if aggregated is not None:
        logger.info(f"Multi-exchange funding for {sym}: {aggregated:.4f}% from {num_sources} sources ({method})")
    
    return _multi_funding_cache[sym]


def get_coinglass_liquidations(sym: str, hours: int = 24) -> dict:
    """Get historical liquidations from Coinglass (if API key available)"""
    api_key = os.getenv("COINGLASS_API_KEY")
    if not api_key:
        return None
    
    try:
        # Coinglass API v3
        url = f"https://open-api.coinglass.com/public/v2/liquidation_history"
        headers = {"coinglassSecret": api_key}
        params = {
            "symbol": sym,
            "time_type": "hourly",
            "range": hours
        }
        r = http.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            if data.get("data"):
                return {
                    "long_liq": sum(float(d.get("longLiquidationUsd", 0)) for d in data["data"]),
                    "short_liq": sum(float(d.get("shortLiquidationUsd", 0)) for d in data["data"]),
                    "entries": len(data["data"])
                }
    except Exception as e:
        logger.debug(f"Coinglass liquidation error for {sym}: {e}")
    return None


def get_okx_liquidation_history(sym: str, hours: int = 24) -> dict:
    """Get liquidation history from OKX"""
    try:
        okx_sym = f"{sym.replace('USDT', '')}-USDT-SWAP"
        url = f"{OKX_BASE_URL}/api/v5/public/liquidation-orders"
        # OKX gives data in pages, get last 100 records
        params = {
            "instType": "SWAP",
            "instId": okx_sym,
            "mgnMode": "",
            "type": "",
            "state": "filled",
            "limit": "100"
        }
        r = http.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            if data.get("data"):
                cutoff_time = (int(time.time()) - hours * 3600) * 1000  # OKX uses ms
                long_liq = 0
                short_liq = 0
                count = 0
                for entry in data["data"]:
                    if int(entry.get("fillTime", 0)) >= cutoff_time:
                        size = float(entry.get("sz", 0))
                        price = float(entry.get("fillPx", 0))
                        side = entry.get("side", "")
                        if side == "sell":  # long liquidation
                            long_liq += size * price
                        else:  # short liquidation
                            short_liq += size * price
                        count += 1
                return {"long_liq": long_liq, "short_liq": short_liq, "entries": count}
    except Exception as e:
        logger.debug(f"OKX liquidation history error for {sym}: {e}")
    return None


def get_btc_dominance() -> dict:
    """Get BTC dominance from CoinGecko (free tier)"""
    try:
        url = f"{COINGECKO_BASE_URL}/global"
        r = http.get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            if data.get("data"):
                market_cap = data["data"].get("total_market_cap", {})
                btc_cap = market_cap.get("btc", 0)
                total_cap = sum(market_cap.values()) if market_cap else 0
                if total_cap > 0:
                    dominance = (btc_cap / total_cap) * 100
                    return {
                        "btc_dominance": dominance,
                        "total_market_cap_usd": data["data"].get("total_market_cap", {}).get("usd", 0),
                        "market_cap_change_24h": data["data"].get("market_cap_change_percentage_24h_usd", 0)
                    }
    except Exception as e:
        logger.debug(f"CoinGecko dominance error: {e}")
    return None


def direct_get(base_url: str, path: str, params=None) -> dict:
    """Direct HTTP GET without any proxies - for emergency fallback"""
    url = f"{base_url}{path}"
    try:
        r = http.get(url, params=params, proxies=None, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        if not r.text:
            raise ValueError("empty response")
        return r.json()
    except Exception as e:
        logger.debug(f"Direct request failed for {url}: {e}")
    return None


def get_price_direct(sym: str) -> float:
    """Emergency price fetch without proxies - Bybit only"""
    try:
        data = direct_get(BYBIT_BASE_URL, "/v5/market/tickers", 
                         {"category": "linear", "symbol": sym})
        if data and data.get("result") and data["result"].get("list"):
            return float(data["result"]["list"][0]["lastPrice"])
    except Exception as e:
        logger.debug(f"Direct price error for {sym}: {e}")
    return None


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

http = requests.Session()
http.trust_env = False
http.headers.update({"User-Agent": "liqbot/1.0"})

_proxy_cache = []


def _normalize_proxy_url(proxy_url: str) -> str:
    proxy_url = proxy_url.strip().strip("'").strip('"')
    if proxy_url and "://" not in proxy_url:
        proxy_url = f"http://{proxy_url}"
    return proxy_url.rstrip("/")


def _proxy_config(proxy_url: str):
    normalized = _normalize_proxy_url(proxy_url)
    return {"http": normalized, "https": normalized}


def _proxy_label(proxy_url: str) -> str:
    parsed = urlsplit(_normalize_proxy_url(proxy_url))
    host = parsed.hostname or "proxy"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    auth = "***:***@" if (parsed.username or parsed.password) else ""
    return f"{parsed.scheme or 'http'}://{auth}{host}"


WEBSHARE_PROXIES = [
    "http://jbmfxgbs:b6qdned11779@31.59.20.176:6754",
    "http://jbmfxgbs:b6qdned11779@45.38.107.97:6014",
    "http://jbmfxgbs:b6qdned11779@198.105.121.200:6462",
    "http://jbmfxgbs:b6qdned11779@142.111.67.146:5611",
    "http://jbmfxgbs:b6qdned11779@31.58.9.4:6077",
]


def _load_configured_proxies():
    raw_values = [BINANCE_PROXY_URLS_RAW, BINANCE_PROXY_URL]
    proxies = []
    seen = set()

    for raw_value in raw_values:
        if not raw_value:
            continue
        prepared = raw_value.replace(",", "\n").replace(";", "\n")
        for part in prepared.splitlines():
            normalized = _normalize_proxy_url(part)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            proxies.append(normalized)

    # Use WebShare proxies as fallback if no proxies configured
    if not proxies:
        for proxy_url in WEBSHARE_PROXIES:
            normalized = _normalize_proxy_url(proxy_url)
            if normalized and normalized not in seen:
                seen.add(normalized)
                proxies.append(normalized)

    return proxies


CONFIGURED_PROXY_URLS = _load_configured_proxies()


def refresh_proxies():
    global _proxy_cache

    if not PUBLIC_PROXY_FALLBACK:
        return

    try:
        r = http.get(
            "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=5000&country=all&ssl=yes&anonymity=all",
            timeout=5,
        )
        r.raise_for_status()
        proxies = [f"http://{p}" for p in r.text.strip().split("\n")[:20] if p.strip()]
        _proxy_cache = proxies
        logger.info("Loaded %s public proxies", len(proxies))
    except Exception as e:
        logger.warning("Proxy refresh failed: %s", e)


def _request_attempts(include_public: bool = True):
    attempts = []

    configured_proxies = list(CONFIGURED_PROXY_URLS)
    if len(configured_proxies) > 1:
        configured_proxies = random.sample(configured_proxies, len(configured_proxies))

    for proxy_url in configured_proxies:
        attempts.append((f"configured proxy {_proxy_label(proxy_url)}", _proxy_config(proxy_url)))

    if not configured_proxies or BINANCE_DIRECT_FALLBACK:
        attempt_name = "direct fallback" if configured_proxies else "direct"
        attempts.append((attempt_name, None))

    if include_public and PUBLIC_PROXY_FALLBACK:
        if not _proxy_cache:
            refresh_proxies()
        for proxy_url in random.sample(_proxy_cache, min(3, len(_proxy_cache))):
            attempts.append((f"public proxy {_proxy_label(proxy_url)}", _proxy_config(proxy_url)))

    return attempts


def _format_error(error: Exception) -> str:
    text = str(error).strip() or error.__class__.__name__
    return text[:140]


def _probe_binance():
    url = f"{BINANCE_BASE_URL}/fapi/v1/ping"
    results = []

    for attempt_name, proxies in _request_attempts(include_public=False):
        started = time.monotonic()
        try:
            r = http.get(url, proxies=proxies, timeout=REQUEST_TIMEOUT)
            elapsed = time.monotonic() - started
            r.raise_for_status()
            results.append(
                {
                    "attempt": attempt_name,
                    "ok": True,
                    "status": r.status_code,
                    "elapsed": elapsed,
                }
            )
        except Exception as e:
            elapsed = time.monotonic() - started
            results.append(
                {
                    "attempt": attempt_name,
                    "ok": False,
                    "elapsed": elapsed,
                    "error": _format_error(e),
                }
            )

    return results


def _proxy_summary_lines():
    lines = [
        "🌐 <b>Exchange transport</b>",
        f"Base URL: <code>{BINANCE_BASE_URL}</code>",
        f"Bybit URL: <code>{BYBIT_BASE_URL}</code>",
        f"Timeout: <code>{REQUEST_TIMEOUT:.1f}s</code>",
        f"Direct fallback: <code>{'on' if BINANCE_DIRECT_FALLBACK else 'off'}</code>",
        f"Public proxy fallback: <code>{'on' if PUBLIC_PROXY_FALLBACK else 'off'}</code>",
    ]

    if CONFIGURED_PROXY_URLS:
        lines.append(f"Configured proxies: <code>{len(CONFIGURED_PROXY_URLS)}</code>")
        for idx, proxy_url in enumerate(CONFIGURED_PROXY_URLS, start=1):
            lines.append(f"{idx}. <code>{_proxy_label(proxy_url)}</code>")
    else:
        lines.append("Configured proxies: <code>0</code>")

    return lines


def _probe_summary_lines(results):
    lines = ["🩺 <b>Binance diagnostic</b>", f"Ping URL: <code>{BINANCE_BASE_URL}/fapi/v1/ping</code>"]

    for item in results:
        icon = "✅" if item["ok"] else "❌"
        if item["ok"]:
            detail = f"HTTP {item['status']} in {item['elapsed']:.2f}s"
        else:
            detail = f"{item['error']} after {item['elapsed']:.2f}s"
        lines.append(f"{icon} <code>{item['attempt']}</code> - {detail}")

    return lines


def _transport_label():
    if CONFIGURED_PROXY_URLS:
        label = f"{len(CONFIGURED_PROXY_URLS)} configured proxies"
    else:
        label = "direct"
    if BINANCE_DIRECT_FALLBACK and CONFIGURED_PROXY_URLS:
        label = f"{label} + direct fallback"
    if PUBLIC_PROXY_FALLBACK:
        label = f"{label} + public proxy fallback"
    return label


def exchange_get(source: str, base_url: str, path: str, params=None, include_public: bool = True):
    url = f"{base_url}{path}"
    last_error = None

    for attempt_name, proxies in _request_attempts(include_public=include_public):
        try:
            r = http.get(url, params=params, proxies=proxies, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            if not r.text:
                raise ValueError("empty response")
            return r.json()
        except Exception as e:
            last_error = e
            logger.warning("%s request failed via %s: %s", source, attempt_name, e)

    logger.error("%s request failed for %s: %s", source, url, last_error)
    return None


def get_price(sym: str) -> float:
    # Try with proxies first
    data = exchange_get("Binance", BINANCE_BASE_URL, "/fapi/v1/ticker/price", {"symbol": sym})
    if data and "price" in data:
        return float(data["price"])

    data = exchange_get(
        "Bybit",
        BYBIT_BASE_URL,
        "/v5/market/tickers",
        {"category": "linear", "symbol": sym},
    )
    if data and data.get("result") and data["result"].get("list"):
        return float(data["result"]["list"][0]["lastPrice"])
    
    # Emergency fallback - direct connection without proxies
    logger.warning(f"All proxy attempts failed for {sym}, trying direct connection...")
    price = get_price_direct(sym)
    if price:
        logger.info(f"Got price for {sym} via direct connection: ${price:,.2f}")
        return price

    raise RuntimeError(f"No market price source is reachable for {sym}")


def get_oi(sym: str, price: float) -> float:
    """Get aggregated Open Interest from multiple exchanges for better accuracy"""
    return get_multi_exchange_oi(sym, price)


def _dec(p: float) -> int:
    if p >= 1000:
        return 1
    if p >= 10:
        return 2
    if p >= 0.01:
        return 4
    return 6


def build_df(coin: str):
    sym = coin.upper().replace("USDT", "").replace("BUSD", "") + "USDT"
    price = get_price(sym)
    oi = get_oi(sym, price)
    rows = []

    for lev, share in LEVERAGE_DIST.items():
        side_liq = oi * share
        for i in range(1, 31):
            lp = price * (1 - 0.4 * i / 30) * (1 - 1 / lev)
            sp = price * (1 + 0.4 * i / 30) * (1 + 1 / lev)
            if lp > 0:
                rows.append({"price": lp, "usd_value": side_liq / 30, "type": "long"})
            rows.append({"price": sp, "usd_value": side_liq / 30, "type": "short"})

    df = pd.DataFrame(rows)
    df["price"] = df["price"].round(_dec(price))
    grouped = df.groupby(["price", "type"], as_index=False)["usd_value"].sum()
    return grouped, price, sym


def build_chart(df: pd.DataFrame, symbol: str, current_price: float) -> io.BytesIO:
    """Генерация графика с тепловой раскраской и подписями топ зон"""
    df = df.sort_values("price").reset_index(drop=True)
    lo = df[df["type"] == "long"]
    sh = df[df["type"] == "short"]
    max_val = df["usd_value"].max()

    # Топ зоны по объёму (топ-3 жёлтые, топ 4-7 оранжевые)
    top_gold_prices = set(df.nlargest(3, "usd_value")["price"].tolist())
    top_orange_prices = set(
        df.nlargest(7, "usd_value").iloc[3:]["price"].tolist()
    )

    def bar_color(price_val, side):
        if price_val in top_gold_prices:
            return "#f5c518"  # GOLD ★
        if price_val in top_orange_prices:
            return "#ef9f27"  # ORANGE ◆
        return "#f23645" if side == "long" else "#089981"  # RED / GREEN

    def bar_alpha(usd_val):
        """Градиент прозрачности: маленькие бары тусклее"""
        ratio = usd_val / max_val if max_val > 0 else 0
        return max(0.30, 0.30 + 0.65 * ratio)

    pr = df["price"].max() - df["price"].min()
    nl = len(df["price"].unique())
    bh = (pr / max(nl, 1)) * 0.75
    dec = _dec(df["price"].max())

    max_render_height = 35
    fig_h = min(max(6, nl * 0.12), max_render_height)
    dpi = max(80, min(120, int(6000 / max(12, fig_h))))

    fig, ax = plt.subplots(figsize=(12, fig_h))
    fig.patch.set_facecolor("#131722")
    ax.set_facecolor("#131722")

    # Рисуем бары с тепловой раскраской
    for _, row in lo.iterrows():
        ax.barh(row["price"], row["usd_value"], height=bh,
                color=bar_color(row["price"], "long"),
                alpha=bar_alpha(row["usd_value"]))
    for _, row in sh.iterrows():
        ax.barh(row["price"], row["usd_value"], height=bh,
                color=bar_color(row["price"], "short"),
                alpha=bar_alpha(row["usd_value"]))

    # ★ Подписи на топ зонах с % расстоянием от цены
    annotated = set()
    for rank, price_val in enumerate(
        df.nlargest(7, "usd_value")["price"].tolist(), start=1
    ):
        if price_val in annotated:
            continue
        annotated.add(price_val)
        zone_val = df[df["price"] == price_val]["usd_value"].sum()
        pct = (price_val - current_price) / current_price * 100
        sign = "+" if pct >= 0 else ""
        star = "★" if rank <= 3 else "◆"
        color = "#f5c518" if rank <= 3 else "#ef9f27"
        ax.annotate(
            f"{star} {sign}{pct:.1f}%  ${zone_val/1000:.0f}k",
            xy=(zone_val, price_val),
            xytext=(8, 0), textcoords="offset points",
            va="center", ha="left",
            fontsize=8, color=color, fontfamily="monospace",
            bbox={"boxstyle": "round,pad=0.15", "facecolor": "#131722", "edgecolor": color, "alpha": 0.7},
        )

    # Линия текущей цены
    ax.axhline(y=current_price, color="#f5c518", linewidth=1.2, linestyle="--", alpha=0.9,
               label=f"Price: {current_price:,.{dec}f}")
    ax.annotate(
        f"▶ {current_price:,.{dec}f}",
        xy=(1.0, current_price), xycoords=("axes fraction", "data"),
        xytext=(8, 0), textcoords="offset points",
        va="center", ha="left", color="#f5c518", fontsize=9, fontfamily="monospace",
        bbox={"boxstyle": "round,pad=0.2", "facecolor": "#131722", "edgecolor": "#f5c518", "alpha": 0.9},
    )

    # Сетка и оформление
    ax.grid(axis="x", color="#2a2e39", linestyle="--", alpha=0.5, linewidth=0.7)
    ax.grid(axis="y", which="major", color="#2a2e39", linestyle=":", alpha=0.3, linewidth=0.5)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_edgecolor("#2a2e39")

    # Оси
    y_ticks = min(60, max(25, int(fig_h // 0.5)))
    ax.yaxis.set_major_locator(mticker.MaxNLocator(nbins=y_ticks, min_n_ticks=25))
    ax.yaxis.set_minor_locator(mticker.AutoMinorLocator(2))
    ax.tick_params(axis="x", colors="#d1d4dc", labelsize=9, length=3)
    ax.tick_params(axis="y", colors="#d1d4dc", labelsize=8, length=3, pad=5)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda y, _: f"{y:.{dec}f}"))
    for lb in ax.get_xticklabels() + ax.get_yticklabels():
        lb.set_fontfamily("monospace")
        lb.set_color("#d1d4dc")

    # Легенда
    from matplotlib.patches import Patch
    legend_elements = [
        Patch(facecolor="#f5c518", label="★ Топ-3 магниты"),
        Patch(facecolor="#ef9f27", label="◆ Топ 4–7 зоны"),
        Patch(facecolor="#089981", label="Шорты (рост)"),
        Patch(facecolor="#f23645", label="Лонги (падение)"),
    ]
    ax.legend(handles=legend_elements, facecolor="#131722", edgecolor="#2a2e39",
              labelcolor="#d1d4dc", fontsize=8, loc="upper right")

    ax.set_xlabel("USD Value", color="#d1d4dc", fontsize=11, fontfamily="monospace")
    ax.set_ylabel("Price", color="#d1d4dc", fontsize=11, fontfamily="monospace")
    ax.set_title(f"Predicted Liquidation Levels — {symbol}",
                 color="#d1d4dc", fontsize=13, pad=14, fontfamily="monospace")

    plt.tight_layout(pad=1.5)
    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight", dpi=dpi, facecolor="#131722")
    buf.seek(0)
    plt.close(fig)
    return buf


def _imbalance_line(df: pd.DataFrame) -> str:
    """Строка с индикатором перекоса лонг/шорт"""
    total_short = df[df["type"] == "short"]["usd_value"].sum()
    total_long = df[df["type"] == "long"]["usd_value"].sum()
    total = total_short + total_long
    if total == 0:
        return ""
    short_pct = total_short / total * 100
    long_pct = total_long / total * 100
    if short_pct > long_pct:
        diff = short_pct - long_pct
        return f"📊 Перекос: 🟢 Шорты {short_pct:.0f}% vs 🔴 Лонги {long_pct:.0f}% (+{diff:.0f}% → вероятен рост)"
    else:
        diff = long_pct - short_pct
        return f"📊 Перекос: 🔴 Лонги {long_pct:.0f}% vs 🟢 Шорты {short_pct:.0f}% (+{diff:.0f}% → вероятно падение)"


def _top_zones_text(df: pd.DataFrame, price: float, sym: str) -> str:
    """Текстовое описание топ-3 зон"""
    dec = _dec(price)
    top3 = df.nlargest(3, "usd_value")
    lines = []
    for rank, (_, row) in enumerate(top3.iterrows(), 1):
        pct = (row["price"] - price) / price * 100
        sign = "+" if pct >= 0 else ""
        side = "🟢шорты" if row["type"] == "short" else "🔴лонги"
        lines.append(
            f"{'★'*rank} ${row['price']:,.{dec}f} ({sign}{pct:.1f}%) "
            f"— ${row['usd_value']/1000:.0f}k {side}"
        )
    return "\n".join(lines)


def _buffered_png(buf: io.BytesIO, filename: str) -> BufferedInputFile:
    buf.seek(0)
    return BufferedInputFile(buf.getvalue(), filename=filename)


async def _send_chart_media(
    chat_id: int,
    buf: io.BytesIO,
    filename: str,
    caption: str,
    message_thread_id: int | None = None,
) -> None:
    try:
        await bot.send_photo(
            chat_id,
            photo=_buffered_png(buf, filename),
            caption=caption,
            parse_mode="HTML",
            message_thread_id=message_thread_id,
        )
    except TelegramBadRequest as e:
        if "PHOTO_INVALID_DIMENSIONS" not in str(e):
            raise
        logger.warning("Photo rejected for %s, retrying as document: %s", filename, e)
        await bot.send_document(
            chat_id,
            document=_buffered_png(buf, filename),
            caption=caption,
            parse_mode="HTML",
            message_thread_id=message_thread_id,
        )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    coins = " ".join([f"<code>{s}</code>" for s in WATCHLIST])
    await message.answer(
        "📊 <b>Liquidation Map Bot v3</b>\n"
        "<i>Multi-Exchange • Aggregation • Fallback</i>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 <b>Основные:</b>\n"
        "  <code>/liq BTC</code> — карта ликвидаций\n"
        "  <code>/scan</code> — скан всех монет\n"
        "  <code>/top</code> — топ-5 зон\n"
        "  <code>/fullstats BTC</code> — полная сводка\n\n"
        "💰 <b>Funding & Data:</b>\n"
        "  <code>/funding BTC</code> — funding rate (4 биржи)\n"
        "  <code>/liqhist BTC</code> — история ликвидаций\n"
        "  <code>/liqstats</code> — ликвидации WebSocket\n"
        "  <code>/dominance</code> — BTC доминация\n\n"
        "🌐 <b>Система:</b>\n"
        "  <code>/net</code> — диагностика сети\n"
        "  <code>/proxy</code> — статус прокси\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ <b>Мониторинг:</b>\n{coins}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🟡 Жёлтый ★ — топ магнит\n"
        "🟠 Оранжевый ◆ — сильная зона\n"
        "🟢 Зелёный — шорты (рост)\n"
        "🔴 Красный — лонги (падение)\n"
        "📊 Данные агрегируются со всех бирж\n"
        "⚡ Автоалерт свыше <b>$500,000</b>",
        parse_mode="HTML")


def _is_allowed_chat(message: types.Message) -> bool:
    """Проверка: команды разрешены только в ЛС или в топике 17135"""
    # В личных сообщениях — разрешено
    if message.chat.type == "private":
        return True
    # В группе только в топике 17135
    if message.chat.id == ALERT_CHAT_ID and message.message_thread_id == ALERT_TOPIC_ID:
        return True
    return False


@dp.message(Command("liq"))
async def cmd_liq(message: types.Message):
    if not _is_allowed_chat(message):
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.reply("⚠️ Пример: <code>/liq BTC</code>", parse_mode="HTML")
        return

    wait = await message.reply(
        f"⏳ Загружаю данные для {parts[1].upper()}...",
        parse_mode="HTML",
    )

    try:
        df, price, sym = build_df(parts[1])
        buf = build_chart(df, sym, price)
        
        short_df = df[df["type"] == "short"]
        long_df = df[df["type"] == "long"]
        
        ms = short_df["usd_value"].max()
        ml = long_df["usd_value"].max()
        
        # Цены максимальных зон ликвидации
        short_max_price = short_df.loc[short_df["usd_value"].idxmax(), "price"]
        long_max_price = long_df.loc[long_df["usd_value"].idxmax(), "price"]
        
        dec = _dec(price)
        
        caption = (
            f"📊 <b>Liquidation Map — {sym}</b>\n\n"
            f"💰 Цена: <b>${price:,.{dec}f}</b>\n\n"
            f"🟡 <b>Топ магниты:</b>\n{_top_zones_text(df, price, sym)}\n\n"
            f"🟢 При росте к <b>${short_max_price:,.{dec}f}</b> — шорты: <b>${ms:,.0f}</b>\n"
            f"🔴 При падении к <b>${long_max_price:,.{dec}f}</b> — лонги: <b>${ml:,.0f}</b>\n\n"
            f"{_imbalance_line(df)}"
        )

        await _send_chart_media(
            message.chat.id,
            buf,
            filename=f"liq_{sym}.png",
            caption=caption,
            message_thread_id=message.message_thread_id,
        )
    except Exception as e:
        logger.exception(e)
        await message.reply(f"❌ Ошибка получения данных: {e}")
    finally:
        await wait.delete()


@dp.message(Command("proxy"))
async def cmd_proxy(message: types.Message):
    if not _is_allowed_chat(message):
        return
    await message.answer("\n".join(_proxy_summary_lines()), parse_mode="HTML")


@dp.message(Command("net"))
async def cmd_net(message: types.Message):
    if not _is_allowed_chat(message):
        return
    wait = await message.reply("⏳ Проверяю доступ к Binance...", parse_mode="HTML")

    try:
        results = await asyncio.to_thread(_probe_binance)
        await message.reply("\n".join(_probe_summary_lines(results)), parse_mode="HTML")
    except Exception as e:
        logger.exception(e)
        await message.reply(f"❌ Диагностика не удалась: {e}")
    finally:
        await wait.delete()


@dp.message(Command("scan"))
async def cmd_scan(message: types.Message):
    """Текстовый обзор топ-зон по всем монетам WATCHLIST"""
    if not _is_allowed_chat(message):
        return
    wait = await message.reply("⏳ Сканирую все монеты...", parse_mode="HTML")
    lines = ["🔍 <b>Scan — топ магниты по всем монетам</b>\n"]
    for coin in WATCHLIST:
        try:
            df, price, sym = build_df(coin)
            top1 = df.nlargest(1, "usd_value").iloc[0]
            pct = (top1["price"] - price) / price * 100
            sign = "+" if pct >= 0 else ""
            dec = _dec(price)
            side = "🟢" if top1["type"] == "short" else "🔴"
            lines.append(
                f"{side} <b>{sym}</b> ${price:,.{dec}f} "
                f"→ магнит ${top1['price']:,.{dec}f} ({sign}{pct:.1f}%) "
                f"${top1['usd_value']/1000:.0f}k"
            )
            await asyncio.sleep(0.5)
        except Exception as e:
            lines.append(f"⚠️ {coin}: ошибка")
            logger.warning(f"scan {coin}: {e}")
    await message.reply("\n".join(lines), parse_mode="HTML")
    await wait.delete()


@dp.message(Command("top"))
async def cmd_top(message: types.Message):
    """Топ-5 самых жирных зон прямо сейчас по всем монетам"""
    if not _is_allowed_chat(message):
        return
    wait = await message.reply("⏳ Собираю топ зоны...", parse_mode="HTML")
    all_zones = []
    for coin in WATCHLIST:
        try:
            df, price, sym = build_df(coin)
            top1 = df.nlargest(1, "usd_value").iloc[0]
            pct = (top1["price"] - price) / price * 100
            all_zones.append({
                "sym": sym, "price": price, "zone_price": top1["price"],
                "usd_val": top1["usd_value"], "pct": pct, "type": top1["type"]
            })
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.warning(f"top {coin}: {e}")

    all_zones.sort(key=lambda x: x["usd_val"], reverse=True)
    lines = ["🏆 <b>Топ-5 самых жирных зон прямо сейчас</b>\n"]
    for i, z in enumerate(all_zones[:5], 1):
        dec = _dec(z["price"])
        sign = "+" if z["pct"] >= 0 else ""
        side = "🟢шорты" if z["type"] == "short" else "🔴лонги"
        lines.append(
            f"{i}. <b>{z['sym']}</b> — ${z['usd_val']/1000:.0f}k {side}\n"
            f"   зона ${z['zone_price']:,.{dec}f} ({sign}{z['pct']:.1f}% от цены)"
        )
    await message.reply("\n".join(lines), parse_mode="HTML")
    await wait.delete()


@dp.message(Command("liqstats"))
async def cmd_liqstats(message: types.Message):
    """Показать статистику реальных ликвидаций из WebSocket"""
    if not _is_allowed_chat(message):
        return
    
    lines = ["📊 <b>Статистика ликвидаций (WebSocket)</b>\n"]
    
    # Show totals for watchlist coins
    for coin in WATCHLIST[:5]:  # Top 5
        sym = coin.upper() + "USDT"
        totals = _liq_ws_data["total_1h"].get(sym, {"long": 0, "short": 0})
        recent = _liq_ws_data["by_symbol"].get(sym, [])
        
        if totals["long"] > 0 or totals["short"] > 0:
            lines.append(
                f"<b>{sym}</b>: 🔴Лонги ${totals['long']:,.0f} | 🟢Шорты ${totals['short']:,.0f}"
            )
            if recent:
                last = recent[-1]
                lines.append(f"  Последняя: {last['side']} ${last['amount']:,.0f} @ ${last['price']:,.2f}")
    
    # Show WebSocket status
    ws_status = "🟢 Подключен" if _liq_ws_data["by_symbol"] else "🟡 Ожидание данных..."
    lines.append(f"\n<b>WebSocket статус:</b> {ws_status}")
    
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("funding"))
async def cmd_funding(message: types.Message):
    """Показать funding rate с агрегацией со всех бирж"""
    if not _is_allowed_chat(message):
        return
    
    parts = message.text.strip().split()
    coin = parts[1].upper() if len(parts) > 1 else "BTC"
    sym = coin.replace("USDT", "") + "USDT"
    
    wait = await message.reply(f"⏳ Загружаю funding rate для {coin}...", parse_mode="HTML")
    
    try:
        funding_data = await asyncio.to_thread(get_multi_exchange_funding, sym)
        
        lines = [f"💰 <b>Funding Rate — {sym}</b>\n"]
        
        # Show individual sources
        sources = ["binance", "bybit", "okx", "bitget"]
        for src in sources:
            if src in funding_data and funding_data[src] is not None:
                val = funding_data[src]
                emoji = "🟢" if val > 0 else "🔴"
                lines.append(f"  <code>{src:8}</code>: {emoji} {val:+.4f}%")
        
        # Show aggregated value
        if funding_data.get("aggregated") is not None:
            agg = funding_data["aggregated"]
            method = funding_data.get("method", "unknown")
            sources_count = funding_data.get("sources", 0)
            emoji = "🟢" if agg > 0 else "🔴"
            lines.append(f"\n<b>Агрегированное:</b> {emoji} {agg:+.4f}%")
            lines.append(f"<i>Метод: {method} из {sources_count} источников</i>")
            
            # Interpretation
            if abs(agg) > 0.1:
                lines.append(f"\n⚠️ Высокий funding! {'Лонги платят шортам' if agg > 0 else 'Шорты платят лонгам'}")
        else:
            lines.append("\n❌ Нет данных с бирж")
        
        await message.reply("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        logger.exception(e)
        await message.reply(f"❌ Ошибка: {e}")
    finally:
        await wait.delete()


@dp.message(Command("liqhist"))
async def cmd_liqhist(message: types.Message):
    """Показать исторические ликвидации за 24ч"""
    if not _is_allowed_chat(message):
        return
    
    parts = message.text.strip().split()
    coin = parts[1].upper() if len(parts) > 1 else "BTC"
    sym = coin.replace("USDT", "") + "USDT"
    
    wait = await message.reply(f"⏳ Загружаю историю ликвидаций для {coin}...", parse_mode="HTML")
    
    try:
        lines = [f"📊 <b>Исторические ликвидации 24ч — {sym}</b>\n"]
        
        # Try Coinglass first
        coinglass_data = await asyncio.to_thread(get_coinglass_liquidations, sym, 24)
        if coinglass_data:
            lines.append(f"<b>Coinglass:</b>")
            lines.append(f"  🔴 Лонги: ${coinglass_data['long_liq']:,.0f}")
            lines.append(f"  🟢 Шорты: ${coinglass_data['short_liq']:,.0f}")
            total = coinglass_data['long_liq'] + coinglass_data['short_liq']
            lines.append(f"  <b>Всего: ${total:,.0f}</b> ({coinglass_data['entries']} записей)")
        
        # Try OKX
        okx_data = await asyncio.to_thread(get_okx_liquidation_history, sym, 24)
        if okx_data:
            lines.append(f"\n<b>OKX:</b>")
            lines.append(f"  🔴 Лонги: ${okx_data['long_liq']:,.0f}")
            lines.append(f"  🟢 Шорты: ${okx_data['short_liq']:,.0f}")
            total = okx_data['long_liq'] + okx_data['short_liq']
            lines.append(f"  <b>Всего: ${total:,.0f}</b> ({okx_data['entries']} записей)")
        
        # WebSocket data
        ws_totals = _liq_ws_data["total_1h"].get(sym, {"long": 0, "short": 0})
        if ws_totals["long"] > 0 or ws_totals["short"] > 0:
            lines.append(f"\n<b>WebSocket (реальное время):</b>")
            lines.append(f"  🔴 Лонги: ${ws_totals['long']:,.0f}")
            lines.append(f"  🟢 Шорты: ${ws_totals['short']:,.0f}")
        
        if len(lines) == 1:
            lines.append("❌ Нет данных с источников")
        
        await message.reply("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        logger.exception(e)
        await message.reply(f"❌ Ошибка: {e}")
    finally:
        await wait.delete()


@dp.message(Command("dominance"))
async def cmd_dominance(message: types.Message):
    """Показать BTC доминацию и общую капитализацию"""
    if not _is_allowed_chat(message):
        return
    
    wait = await message.reply("⏳ Загружаю данные рынка...", parse_mode="HTML")
    
    try:
        data = await asyncio.to_thread(get_btc_dominance)
        
        if data:
            dom = data["btc_dominance"]
            cap = data["total_market_cap_usd"]
            change = data["market_cap_change_24h"]
            
            emoji = "🟢" if change >= 0 else "🔴"
            dom_emoji = "👑" if dom > 50 else "⚡"
            
            lines = [
                f"{dom_emoji} <b>BTC Доминация: {dom:.1f}%</b>",
                f"",
                f"💰 Общая капитализация: <b>${cap/1e12:.2f}T</b>",
                f"📊 Изменение 24ч: {emoji} {change:+.2f}%",
                f"",
                f"<i>Данные: CoinGecko</i>"
            ]
            
            # Add interpretation
            if dom > 60:
                lines.append(f"\n⚠️ Высокая доминация — альтсезон отложен")
            elif dom < 40:
                lines.append(f"\n🚀 Низкая доминация — альтсезон активен!")
            
            await message.reply("\n".join(lines), parse_mode="HTML")
        else:
            await message.reply("❌ Не удалось получить данные с CoinGecko")
    except Exception as e:
        logger.exception(e)
        await message.reply(f"❌ Ошибка: {e}")
    finally:
        await wait.delete()


@dp.message(Command("fullstats"))
async def cmd_fullstats(message: types.Message):
    """Полная сводка по монете: цена, OI, funding, ликвидации"""
    if not _is_allowed_chat(message):
        return
    
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.reply("⚠️ Пример: <code>/fullstats BTC</code>", parse_mode="HTML")
        return
    
    coin = parts[1].upper()
    sym = coin.replace("USDT", "") + "USDT"
    
    wait = await message.reply(f"⏳ Собираю полную сводку для {coin}...", parse_mode="HTML")
    
    try:
        # Get all data in parallel
        price_task = asyncio.to_thread(get_price, sym)
        oi_task = asyncio.to_thread(get_multi_exchange_oi, sym, 0)  # Price will be updated
        funding_task = asyncio.to_thread(get_multi_exchange_funding, sym)
        
        price = await price_task
        oi_data = await asyncio.to_thread(get_multi_exchange_oi, sym, price)
        funding_data = await funding_task
        
        lines = [f"📊 <b>Полная сводка — {sym}</b>\n"]
        
        # Price
        dec = _dec(price)
        lines.append(f"💰 <b>Цена:</b> ${price:,.{dec}f}")
        
        # OI
        oi_total = oi_data.get("total", 0)
        oi_sources = oi_data.get("sources", 0)
        lines.append(f"📈 <b>Open Interest:</b> ${oi_total:,.0f} ({oi_sources} источников)")
        
        # Funding
        if funding_data.get("aggreguated") is not None:
            fund = funding_data["aggregated"]
            fund_emoji = "🟢" if fund > 0 else "🔴"
            lines.append(f"💸 <b>Funding Rate:</b> {fund_emoji} {fund:+.4f}%")
        
        # Liquidation zones from build_df
        try:
            df, _, _ = build_df(coin)
            short_max = df[df["type"] == "short"]["usd_value"].max()
            long_max = df[df["type"] == "long"]["usd_value"].max()
            lines.append(f"\n🎯 <b>Макс. зона ликвидации:</b>")
            lines.append(f"   🟢 Шорты: ${short_max:,.0f}")
            lines.append(f"   🔴 Лонги: ${long_max:,.0f}")
        except:
            pass
        
        # WebSocket liquidations
        ws_totals = _liq_ws_data["total_1h"].get(sym, {"long": 0, "short": 0})
        if ws_totals["long"] > 0 or ws_totals["short"] > 0:
            lines.append(f"\n⚡ <b>Ликвидации 1ч (WS):</b>")
            lines.append(f"   🔴 Лонги: ${ws_totals['long']:,.0f}")
            lines.append(f"   🟢 Шорты: ${ws_totals['short']:,.0f}")
        
        await message.reply("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        logger.exception(e)
        await message.reply(f"❌ Ошибка: {e}")
    finally:
        await wait.delete()


@dp.message()
async def cmd_fallback(message: types.Message):
    if message.chat.type in ("group", "supergroup"):
        return
    await message.reply(
        "Используй:\n"
        "📌 <code>/liq BTC</code> — карта ликвидаций\n"
        "🔍 <code>/scan</code> — топ магниты по всем монетам\n"
        "🏆 <code>/top</code> — самые жирные зоны сейчас\n"
        "📈 <code>/liqstats</code> — реальные ликвидации\n"
        "💰 <code>/funding BTC</code> — funding rate (мульти-биржа)\n"
        "📊 <code>/liqhist BTC</code> — история ликвидаций\n"
        "👑 <code>/dominance</code> — BTC доминация\n"
        "📋 <code>/fullstats BTC</code> — полная сводка\n"
        "❓ <code>/help</code> — справка",
        parse_mode="HTML",
    )


async def auto_alert_loop():
    await asyncio.sleep(15)
    refresh_proxies()

    while True:
        for coin in WATCHLIST:
            try:
                df, price, sym = build_df(coin)
                ms = df[df["type"] == "short"]["usd_value"].max()
                ml = df[df["type"] == "long"]["usd_value"].max()

                if max(ms, ml) >= ALERT_THRESHOLD:
                    buf = build_chart(df, sym, price)
                    emoji = "🟢" if ms > ml else "🔴"
                    dec = _dec(price)
                    caption = (
                        f"🚨 <b>АЛЕРТ — {sym}</b>\n\n"
                        f"{emoji} Мощная зона ликвидации!\n"
                        f"💰 Цена: ${price:,.{dec}f}\n"
                        f"🟢 Шорты при росте: ${ms:,.0f}\n"
                        f"🔴 Лонги при падении: ${ml:,.0f}"
                    )

                    await _send_chart_media(
                        ALERT_CHAT_ID,
                        buf,
                        filename=f"alert_{sym}.png",
                        caption=caption,
                        message_thread_id=ALERT_TOPIC_ID,
                    )
                await asyncio.sleep(2)
            except Exception as e:
                logger.warning("%s: %s", coin, e)

        refresh_proxies()
        await asyncio.sleep(1800)


async def main():
    # Start WebSocket liquidation listener (free real-time data)
    asyncio.create_task(liquidation_ws_listener())
    asyncio.create_task(auto_alert_loop())
    await bot.delete_webhook(drop_pending_updates=DROP_PENDING_UPDATES)
    logger.info("Bot started. Binance transport: %s, WebSocket: enabled", _transport_label())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
