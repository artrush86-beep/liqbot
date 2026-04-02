"""
Liquidation Map Telegram Bot — Railway.app ready
=================================================
Установка зависимостей:
  pip install aiogram matplotlib pandas requests

Запуск:
  python bot.py
"""

import asyncio
import os
import io
import logging

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
import requests
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import BufferedInputFile

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================
BOT_TOKEN = os.environ["BOT_TOKEN"]
COINGLASS_API_KEY = os.environ["COINGLASS_API_KEY"]

ALERT_CHAT_ID      = -1003867089540   # ID твоей группы
ALERT_TOPIC_ID     = 17135            # ID топика в группе
ALERT_THRESHOLD_USD = 500_000         # Порог для автоалертов ($500k)

WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()


# ============================================================
# 1. ПОЛУЧЕНИЕ ДАННЫХ — Coinglass
# ============================================================
def fetch_liquidation_data(symbol: str) -> pd.DataFrame:
    base = symbol.upper().replace("USDT", "").replace("1000", "")

    url = "https://open-api.coinglass.com/public/v2/liquidation_map"
    headers = {
        "coinglassSecret": COINGLASS_API_KEY,
        "Content-Type": "application/json",
    }
    params = {
        "symbol": base,
        "exName": "Binance",
        "type":   "1",
    }

    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if str(data.get("code")) != "0":
        raise ValueError(f"Coinglass API error: {data.get('msg', 'Unknown')}")

    rows = []
    payload = data.get("data", {})

    for item in payload.get("longLiquidationMap", []):
        rows.append({
            "price":     float(item["price"]),
            "usd_value": float(item["liquidationAmount"]),
            "type":      "long",
        })
    for item in payload.get("shortLiquidationMap", []):
        rows.append({
            "price":     float(item["price"]),
            "usd_value": float(item["liquidationAmount"]),
            "type":      "short",
        })

    if not rows:
        raise ValueError(f"Нет данных для символа {symbol}")

    return pd.DataFrame(rows)


# ============================================================
# 2. ФОРМАТИРОВАНИЕ ОСИ Y
# ============================================================
def _get_decimals(price: float) -> int:
    if price >= 1000: return 1
    if price >= 10:   return 2
    if price >= 0.01: return 4
    return 6


# ============================================================
# 3. ГЕНЕРАЦИЯ ГРАФИКА
# ============================================================
def build_chart(df: pd.DataFrame, symbol: str) -> io.BytesIO:
    BG    = "#131722"
    GRID  = "#2a2e39"
    GREEN = "#089981"
    RED   = "#f23645"
    TEXT  = "#d1d4dc"

    df     = df.sort_values("price", ascending=True).reset_index(drop=True)
    longs  = df[df["type"] == "long"]
    shorts = df[df["type"] == "short"]

    price_range = df["price"].max() - df["price"].min()
    n_levels    = len(df["price"].unique())
    bar_h       = (price_range / max(n_levels, 1)) * 0.75
    decimals    = _get_decimals(df["price"].max())

    fig, ax = plt.subplots(figsize=(12, max(8, n_levels * 0.28)))
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    ax.barh(longs["price"],  longs["usd_value"],  height=bar_h, color=RED,   alpha=0.92)
    ax.barh(shorts["price"], shorts["usd_value"], height=bar_h, color=GREEN, alpha=0.92)

    ax.grid(axis="x", color=GRID, linestyle="--", alpha=0.5, linewidth=0.7)
    ax.set_axisbelow(True)

    for spine in ax.spines.values():
        spine.set_edgecolor(GRID)

    ax.tick_params(colors=TEXT, labelsize=9, length=3)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda y, _: f"{y:.{decimals}f}"))

    for label in ax.get_xticklabels() + ax.get_yticklabels():
        label.set_fontfamily("monospace")
        label.set_color(TEXT)

    ax.set_xlabel("USD Value", color=TEXT, fontsize=11, fontfamily="monospace")
    ax.set_ylabel("Price",     color=TEXT, fontsize=11, fontfamily="monospace")
    ax.set_title(
        f"Predicted Liquidation Levels – {symbol}",
        color=TEXT, fontsize=13, pad=14, fontfamily="monospace"
    )

    plt.tight_layout(pad=1.5)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight", dpi=150, facecolor=BG)
    buf.seek(0)
    plt.close(fig)
    return buf


# ============================================================
# 4. КОМАНДЫ БОТА
# ============================================================
@dp.message(Command("start", "help"))
async def cmd_start(message: types.Message):
    await message.answer(
        "📊 <b>Liquidation Map Bot</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 <b>Как пользоваться:</b>\n"
        "Отправь <code>/liq СИМВОЛ</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🗂 <b>Доступные монеты:</b>\n\n"
        "🟡 <code>/liq BTCUSDT</code>  — Bitcoin\n"
        "🔵 <code>/liq ETHUSDT</code>  — Ethereum\n"
        "🟣 <code>/liq SOLUSDT</code>  — Solana\n"
        "🟡 <code>/liq BNBUSDT</code>  — BNB\n"
        "🔵 <code>/liq XRPUSDT</code>  — XRP\n"
        "🟡 <code>/liq DOGEUSDT</code> — Dogecoin\n"
        "🔵 <code>/liq ADAUSDT</code>  — Cardano\n"
        "🟣 <code>/liq UNIUSDT</code>  — Uniswap\n"
        "🔵 <code>/liq FILUSDT</code>  — Filecoin\n"
        "🔵 <code>/liq DOTUSDT</code>  — Polkadot\n"
        "⚪ <code>/liq LTCUSDT</code>  — Litecoin\n"
        "🔵 <code>/liq LINKUSDT</code> — Chainlink\n"
        "🟡 <code>/liq XLMUSDT</code>  — Stellar\n"
        "🔵 <code>/liq ATOMUSDT</code> — Cosmos\n"
        "🔵 <code>/liq ZILUSDT</code>  — Zilliqa\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎨 <b>Как читать график:</b>\n\n"
        "🟢 Зелёный — зона ликвидации шортов → цена растёт\n"
        "🔴 Красный — зона ликвидации лонгов → цена падает\n"
        "📏 Длинный бар = сильный магнит для цены\n"
        "⚡ Автоалерт при ликвидациях свыше <b>$500,000</b>",
        parse_mode="HTML"
    )


@dp.message(Command("liq"))
async def cmd_liq(message: types.Message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.reply("⚠️ Пример: <code>/liq BTCUSDT</code>", parse_mode="HTML")
        return

    symbol = parts[1].upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    wait_msg = await message.reply(
        f"⏳ Загружаю данные для <b>{symbol}</b>...", parse_mode="HTML"
    )

    try:
        df        = fetch_liquidation_data(symbol)
        buf       = build_chart(df, symbol)
        max_short = df[df["type"] == "short"]["usd_value"].max()
        max_long  = df[df["type"] == "long"]["usd_value"].max()

        caption = (
            f"📊 <b>Liquidation Map — {symbol}</b>\n\n"
            f"🟢 Макс. шорт-ликвидация: <b>${max_short:,.0f}</b>\n"
            f"🔴 Макс. лонг-ликвидация:  <b>${max_long:,.0f}</b>\n\n"
            f"<i>Длинный бар = зона притяжения цены</i>"
        )

        await bot.send_photo(
            message.chat.id,
            photo=BufferedInputFile(buf.read(), filename=f"liq_{symbol}.png"),
            caption=caption,
            parse_mode="HTML",
            # Если команда в топике — отвечаем в тот же топик
            message_thread_id=message.message_thread_id,
        )

    except ValueError as e:
        await message.reply(f"❌ {e}")
    except requests.HTTPError as e:
        await message.reply(f"❌ Ошибка API: {e}")
    except Exception as e:
        logger.exception(e)
        await message.reply(f"❌ Неожиданная ошибка: {e}")
    finally:
        await wait_msg.delete()


# ============================================================
# 5. АВТОАЛЕРТЫ В ТОПИК ГРУППЫ (каждые 30 минут)
# ============================================================
async def auto_alert_loop():
    """Шлёт алерт в группу/топик, если ликвидации > ALERT_THRESHOLD_USD"""
    await asyncio.sleep(10)  # небольшой старт-задержка

    while True:
        for symbol in WATCHLIST:
            try:
                df        = fetch_liquidation_data(symbol)
                max_short = df[df["type"] == "short"]["usd_value"].max()
                max_long  = df[df["type"] == "long"]["usd_value"].max()
                max_val   = max(max_short, max_long)

                if max_val >= ALERT_THRESHOLD_USD:
                    buf   = build_chart(df, symbol)
                    emoji = "🟢" if max_short > max_long else "🔴"
                    caption = (
                        f"🚨 <b>АЛЕРТ — {symbol}</b>\n\n"
                        f"{emoji} Мощная зона ликвидаций!\n"
                        f"🟢 Шорты: <b>${max_short:,.0f}</b>\n"
                        f"🔴 Лонги:  <b>${max_long:,.0f}</b>\n\n"
                        f"<i>Вероятный разворот или пробой структуры</i>"
                    )
                    await bot.send_photo(
                        chat_id=ALERT_CHAT_ID,
                        photo=BufferedInputFile(buf.read(), filename=f"alert_{symbol}.png"),
                        caption=caption,
                        parse_mode="HTML",
                        message_thread_id=ALERT_TOPIC_ID,  # ← шлём в твой топик
                    )
                    logger.info(f"Alert sent for {symbol}: ${max_val:,.0f}")

                await asyncio.sleep(3)  # пауза между символами

            except Exception as e:
                logger.warning(f"Auto-alert error for {symbol}: {e}")

        await asyncio.sleep(1800)  # ждём 30 минут до следующего цикла


# ============================================================
# 6. ЗАПУСК
# ============================================================
async def main():
    asyncio.create_task(auto_alert_loop())
    logger.info("✅ Bot started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
