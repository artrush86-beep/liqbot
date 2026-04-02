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

BOT_TOKEN       = os.environ["BOT_TOKEN"]
ALERT_CHAT_ID   = -1003867089540
ALERT_TOPIC_ID  = 17135
ALERT_THRESHOLD = 500_000

WATCHLIST = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "DOGEUSDT","ADAUSDT","UNIUSDT","FILUSDT","DOTUSDT",
    "LTCUSDT","LINKUSDT","XLMUSDT","ATOMUSDT","ZILUSDT"
]

LEVERAGE_DIST = {2:0.05,3:0.08,5:0.15,10:0.25,20:0.22,50:0.15,100:0.10}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()
BINANCE = "https://fapi.binance.com"

def get_price(symbol):
    r = requests.get(f"{BINANCE}/fapi/v1/ticker/price", params={"symbol":symbol}, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])

def get_oi(symbol, price):
    r = requests.get(f"{BINANCE}/fapi/v1/openInterest", params={"symbol":symbol}, timeout=10)
    r.raise_for_status()
    return float(r.json()["openInterest"]) * price

def _decimals(price):
    if price >= 1000: return 1
    if price >= 10:   return 2
    if price >= 0.01: return 4
    return 6

def build_df(symbol):
    price = get_price(symbol)
    oi    = get_oi(symbol, price)
    rows  = []
    for lev, share in LEVERAGE_DIST.items():
        sl = oi * share
        for i in range(1, 31):
            lp = price*(1 - 0.4*i/30)*(1 - 1/lev)
            sp = price*(1 + 0.4*i/30)*(1 + 1/lev)
            if lp > 0:
                rows.append({"price":lp,"usd_value":sl/30,"type":"long"})
            rows.append({"price":sp,"usd_value":sl/30,"type":"short"})
    df = pd.DataFrame(rows)
    df["price"] = df["price"].round(_decimals(price))
    df = df.groupby(["price","type"],as_index=False)["usd_value"].sum()
    return df, price

def build_chart(df, symbol, price):
    BG="#131722"; GRID="#2a2e39"; GREEN="#089981"; RED="#f23645"; GOLD="#f5c518"; TEXT="#d1d4dc"
    df = df.sort_values("price").reset_index(drop=True)
    longs  = df[df["type"]=="long"]
    shorts = df[df["type"]=="short"]
    pr = df["price"].max()-df["price"].min()
    nl = len(df["price"].unique())
    bh = (pr/max(nl,1))*0.75
    dec = _decimals(df["price"].max())
    fig,ax = plt.subplots(figsize=(12,max(8,nl*0.18)))
    fig.patch.set_facecolor(BG); ax.set_facecolor(BG)
    ax.barh(longs["price"],  longs["usd_value"],  height=bh, color=RED,   alpha=0.92)
    ax.barh(shorts["price"], shorts["usd_value"], height=bh, color=GREEN, alpha=0.92)
    ax.axhline(y=price, color=GOLD, linewidth=1.2, linestyle="--", alpha=0.9, label=f"Price: {price:,.{dec}f}")
    ax.grid(axis="x",color=GRID,linestyle="--",alpha=0.5,linewidth=0.7); ax.set_axisbelow(True)
    for s in ax.spines.values(): s.set_edgecolor(GRID)
    ax.tick_params(colors=TEXT,labelsize=9,length=3)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x,_: f"{x:,.0f}"))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda y,_: f"{y:.{dec}f}"))
    for lb in ax.get_xticklabels()+ax.get_yticklabels(): lb.set_fontfamily("monospace"); lb.set_color(TEXT)
    ax.set_xlabel("USD Value",color=TEXT,fontsize=11,fontfamily="monospace")
    ax.set_ylabel("Price",color=TEXT,fontsize=11,fontfamily="monospace")
    ax.set_title(f"Predicted Liquidation Levels – {symbol}",color=TEXT,fontsize=13,pad=14,fontfamily="monospace")
    ax.legend(facecolor=BG,edgecolor=GRID,labelcolor=TEXT,fontsize=9,loc="upper right")
    plt.tight_layout(pad=1.5)
    buf=io.BytesIO(); plt.savefig(buf,format="png",bbox_inches="tight",dpi=150,facecolor=BG); buf.seek(0); plt.close(fig)
    return buf

@dp.message(Command("start","help"))
async def cmd_start(message: types.Message):
    coins="\n".join([f"  <code>/liq {s}</code>" for s in WATCHLIST])
    await message.answer(
[200~cd ~/liqbot        "📊 <b>Liquidation Map Bot</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 Отправь <code>/liq СИМВОЛ</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🗂 <b>Монеты:</b>\n{coins}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🟢 Зелёный — шорты ликвидируются → цена растёт\n"
        "🔴 Красный — лонги ликвидируются → цена падает\n"
        "🟡 Линия — текущая цена\n"
        "⚡ Автоалерт свыше <b>$500,000</b>",
        parse_mode="HTML")

@dp.message(Command("liq"))
async def cmd_liq(message: types.Message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.reply("⚠️ Пример: <code>/liq BTCUSDT</code>",parse_mode="HTML"); return
    symbol = parts[1].upper()
    if not symbol.endswith("USDT"): symbol+="USDT"
    wait = await message.reply(f"⏳ Загружаю <b>{symbol}</b>...",parse_mode="HTML")
    try:
        df,price = build_df(symbol)
        buf      = build_chart(df,symbol,price)
        ms = df[df["type"]=="short"]["usd_value"].max()
        ml = df[df["type"]=="long"]["usd_value"].max()
        dec = _decimals(price)
        await bot.send_photo(message.chat.id,
            photo=BufferedInputFile(buf.read(),filename=f"liq_{symbol}.png"),
            caption=f"📊 <b>{symbol}</b>\n💰 ${price:,.{dec}f}\n🟢 Шорты: ${ms:,.0f}\n🔴 Лонги: ${ml:,.0f}\n<i>Данные: Binance OI</i>",
            parse_mode="HTML", message_thread_id=message.message_thread_id)
    except Exception as e:
        logger.exception(e); await message.reply(f"❌ {e}")
    finally:
        await wait.delete()

async def auto_alert_loop():
    await asyncio.sleep(15)
    while True:
        for symbol in WATCHLIST:
            try:
                df,price = build_df(symbol)
                ms=df[df["type"]=="short"]["usd_value"].max()
                ml=df[df["type"]=="long"]["usd_value"].max()
                if max(ms,ml)>=ALERT_THRESHOLD:
                    buf=build_chart(df,symbol,price)
                    emoji="🟢" if ms>ml else "🔴"
                    dec=_decimals(price)
                    await bot.send_photo(ALERT_CHAT_ID,
                        photo=BufferedInputFile(buf.read(),filename=f"alert_{symbol}.png"),
                        caption=f"🚨 <b>АЛЕРТ {symbol}</b>\n{emoji} Мощная зона!\n💰 ${price:,.{dec}f}\n🟢 ${ms:,.0f}\n🔴 ${ml:,.0f}",
                        parse_mode="HTML", message_thread_id=ALERT_TOPIC_ID)
                await asyncio.sleep(2)
            except Exception as e:
                logger.warning(f"{symbol}: {e}")
        await asyncio.sleep(1800)

async def main():
    asyncio.create_task(auto_alert_loop())
    logger.info("✅ Bot started!")
    await dp.start_polling(bot)

if __name__=="__main__":
    asyncio.run(main())
