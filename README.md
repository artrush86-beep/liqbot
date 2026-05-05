# liqbot

## Environment

Use `.env.example` as a template for your environment variables.

Example:

```bash
cd /Users/artemt/Downloads/liqbot_repo
export BOT_TOKEN='replace_me'
export REQUEST_TIMEOUT='12'
export BINANCE_BASE_URL='https://fapi1.binance.com'
export BINANCE_DIRECT_FALLBACK='1'
export PUBLIC_PROXY_FALLBACK='0'
export BINANCE_PROXY_URLS='http://proxy_user:proxy_password@host1:port1/,http://proxy_user:proxy_password@host2:port2/'
python3 bot.py
```

Store real proxy credentials only in your local shell or a local `.env` file that is not committed.

## Telegram commands

- `/liq BTC` builds the liquidation chart for the symbol.
- `/proxy` shows the current Binance base URL, timeout, and configured proxy list.
- `/net` runs a Binance ping diagnostic against the configured routes.

## Curl checks

```bash
curl --proxy "http://proxy_user:proxy_password@host1:port1/" https://ipv4.webshare.io/
curl --proxy "http://proxy_user:proxy_password@host2:port2/" https://ipv4.webshare.io/
```

## Optional Binance host switch

If the default Binance host is blocked or slow in your region, use:

```bash
export BINANCE_BASE_URL='https://fapi1.binance.com'
```
