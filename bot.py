"""
Liquidation Map Bot — 100% бесплатно через Binance Public API
==============================================================
Алгоритм:
  1. Берём текущую цену с Binance Futures
  2. Берём суммарный Open Interest
  3. Распределяем OI по плечам (2x, 3x, 5x, 10x, 20x, 50x, 100x)
  4. Считаем цены ликвидации лонгов и шортов на каждом уровне
  5. Строим barh график как у Coinglass
"""

import asyncio
import io
import logging
import os

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
BOT_TOKEN         = os.environ["BOT_TOKEN"]
ALERT_CHAT_ID     = -1003867089540
ALERT_TOPIC_ID    = 17135
ALERT_THRESHOLD   = 500_000

WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "UNIUSDT", "FILUSDT", "DOTUSDT",
    "LTCUSDT", "LINKUSDT", "XLMUSDT", "ATOMUSDT", "ZILUSDT"
]

# Распределение OI по плечам (сумма = 1.0)
LEVERAGE_DIST = {
    2:   0.05,
    3:   0.08,
    5:   0.15,
    10:  0.25,
    20:  0.22,
    50:  0.15,
    100: 0.10,
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

BINANCE_BASE = "https://fapi.binance.com"


# ============================================================
# 1. ПОЛУЧЕНИЕ ДАННЫХ С BINANCE (БЕСПЛАТНО)
# ============================================================
def get_price(symbol: str) -> float:
    r = requests.get(f"{BINANCE_BASE}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])


def get_open_interest(symbol: str, price: float) -> float:
    r = requests.get(f"{BINANCE_BASE}/fapi/v1/openInterest", params={"symbol": symbol}, timeout=10)
    r.raise_for_status()
    return float(r.json()["openInterest"]) * price


def build_liquidation_df(symbol: str) -> tuple:
    price  = get_price(symbol)
    oi_usd = get_open_interest(symbol, price)

    rows = []
    spread   = 0.40
    n_levels = 30

    for leverage, share in LEVERAGE_DIST.items():
        oi_slice = oi_usd * share

        prices_long  = [price * (1 - spread * i / n_levels) for i in range(1, n_levels + 1)]
        prices_short = [price * (1 + spread * i / n_levels) for i in range(1, n_levels + 1)]

        for entry in prices_long:
            liq_price = entry * (1 - 1 / leverage)
            if liq_price > 0:
                rows.append({"price": liq_price, "usd_value": oi_slice / n_levels, "type": "long"})

        for entry in prices_short:
            liq_price = entry * (1 + 1 / leverage)
            rows.append({"price": liq_price, "usd_value": oi_slice / n_levels, "type": "short"})

    df = pd.DataFrame(rows)
    decimals = _get_decimals(price)
    df["price"] = df["price"].round(decimals)
    df = df.groupby(["price", "type"], as_index=False)["usd_value"].sum()

    return df, price


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
def build_chart(df: pd.DataFrame, symbol: str, current_price: float) -> io.BytesIO:
    BG    = "#131722"
    GRID  = "#2a2e39"
    GREEN = "#089981"
    RED   = "#f23645"
    GOLD  = "#f5c518"
    TEXT  = "#d1d4dc"

    df     = df.sort_values("price", ascending=True).reset_index(drop=True)
    longs  = df[df["type"] == "long"]
    shorts = df[df["type"] == "short"]

    price_range = df["price"].max() - df["price"].min()
    n_levels    = len(df["price"].unique())
    bar_h       = (price_range / max(n_levels, 1)) * 0.75
    decimals    = _get_decimals(df["price"].max())

    fig, ax = plt.subplots(figsize=(12, max(8, n_levels * 0.18)))
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    ax.barh(longs["price"],  longs["usd_value"],  height=bar_h, color=RED,   alpha=0.92)
    ax.barh(shorts["price"], shorts["usd_value"], height=bar_h, color=GREEN, alpha=0.92)

    ax.axhline(y=current_price, color=GOLD, linewidth=1.2, linestyle="--", alpha=0.9,
               label=f"Price: {current_price:,.{decimals}f}")

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

    ax.set_xlabel("USD Value (estimated)", color=TEXT, fontsize=11, fontfamily="monospace")
    ax.set_ylabel("Price",                 color=TEXT, fontsize=11, fontfamily="monospace")
    ax.set_title(f"Predicted Liquidation Levels – {symbol}",
                 color=TEXT, fontsize=13, pad=14, fontfamily="monospace")
    ax.legend(facecolor=BG, edgecolor=GRID, labelcolor=TEXT, fontsize=9, loc="upper right")

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
    coins = "\n".join([f"  <code>/liq {s}</code>" for s in WATCHLIST])
    await message.answer(
        "📊 <b>Liquidation Map Bot</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 <b>Как пользоваться:</b>\n"
        "Отправь <code>/liq СИМВОЛ</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🗂 <b>Доступные монеты:</b>\n{coins}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎨 <b>Как читать график:</b>\n\n"
        "🟢 Зелёный — зона ликвидации шортов → цена растёт\n"
        "🔴 Красный — зона ликвидации лонгов → цена падает\n"
        "🟡 Линия — текущая цена\n"
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
        df, price = build_liquidation_df(symbol)
        buf       = build_chart(df, symbol, price)
        max_short = df[df["type"] == "short"]["usd_value"].max()
        max_long  = df[df["type"] == "long"]["usd_value"].max()
        decimals  = _get_decimals(price)

        caption = (
            f"📊 <b>Liquidation Map — {symbol}</b>\n\n"
            f"💰 Цена: <b>${price:,.{decimals}f}</b>\n"
            f"🟢 Макс. шорт-зона: <b>${max_short:,.0f}</b>\n"
            f"🔴 Макс. лонг-зона:  <b>${max_long:,.0f}</b>\n\n"
            f"<i>Расчёт на основе Binance Open Interest</i>"
        )

        await bot.send_photo(
            message.chat.id,
            photo=BufferedInputFile(buf.read(), filename=f"liq_{symbol}.png"),
            caption=caption,
            parse_mode="HTML",
            message_thread_id=message.message_thread_id,
        )

    except Exception as e:
        logger.exception(e)
        await message.reply(f"❌ Ошибка: {e}")
    finally:
        await wait_msg.delete()


# ============================================================
# 5. АВТОАЛЕРТЫ (каждые 30 минут)
# ============================================================
async def auto_alert_loop():
    await asyncio.sleep(15)
    while True:
        for symbol in WATCHLIST:
            try:
                df, price = build_liquidation_df(symbol)
                max_short = df[df["type"] == "short"]["usd_value"].max()
                max_long  = df[df["type"] == "long"]["usd_value"].max()
                max_val   = max(max_short, max_long)

                if max_val >= ALERT_THRESHOLD:
                    buf      = build_chart(df, symbol, price)
                    emoji    = "🟢" if max_short > max_long else "🔴"
                    decimals = _get_decimals(price)
                    caption  = (
                        f"🚨 <b>АЛЕРТ — {symbol}</b>\n\n"
                        f"{emoji} Мощная зона ликвидаций!\n"
                        f"💰 Цена: <b>${price:,.{decimals}f}</b>\n"
                        f"🟢 Шорты: <b>${max_short:,.0f}</b>\n"
                        f"🔴 Лонги:  <b>${max_long:,.0f}</b>"
                    )
                    await bot.send_photo(
                        chat_id=ALERT_CHAT_ID,
                        photo=BufferedInputFile(buf.read(), filename=f"alert_{symbol}.png"),
                        caption=caption,
                        parse_mode="HTML",
                        message_thread_id=ALERT_TOPIC_ID,
                    )
                await asyncio.sleep(2)
            except Exception as e:
                logger.warning(f"Alert error {symbol}: {e}")

        await asyncio.sleep(1800)


# ============================================================
# 6. ЗАПУСК
# ============================================================
async def main():
    asyncio.create_task(auto_alert_loop())
    logger.info("✅ Bot started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
