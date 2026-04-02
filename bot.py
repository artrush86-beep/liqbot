import asyncio, io, logging, os, random
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

WATCHLIST = ["BTC","ETH","SOL","BNB","XRP",
             "DOGE","ADA","UNI","FIL","DOT",
             "LTC","LINK","XLM","ATOM","ZIL"]

LEVERAGE_DIST = {2:0.05,3:0.08,5:0.15,10:0.25,20:0.22,50:0.15,100:0.10}
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

_proxy_cache = []

def refresh_proxies():
    global _proxy_cache
    try:
        r = requests.get(
            "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=5000&country=all&ssl=yes&anonymity=all",
            timeout=5)
        proxies = [f"http://{p}" for p in r.text.strip().split("\n")[:20] if p.strip()]
        _proxy_cache = proxies
        logger.info(f"Loaded {len(proxies)} proxies")
    except Exception as e:
        logger.warning(f"Proxy refresh failed: {e}")

def binance_get(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=8)
        if r.status_code == 200 and r.text:
            return r.json()
    except Exception:
        pass
    if not _proxy_cache:
        refresh_proxies()
    for _ in range(3):
        if not _proxy_cache:
            break
        proxy = {"https": random.choice(_proxy_cache)}
        try:
            r = requests.get(url, params=params, proxies=proxy, timeout=8)
            if r.status_code == 200 and r.text:
                return r.json()
        except Exception:
            continue
    return None

def get_price(sym):
    data = binance_get("https://fapi.binance.com/fapi/v1/ticker/price", {"symbol": sym})
    if data and "price" in data:
        return float(data["price"])
    r = requests.get("https://api.bybit.com/v5/market/tickers",
                     params={"category":"linear","symbol":sym}, timeout=10)
    r.raise_for_status()
    return float(r.json()["result"]["list"][0]["lastPrice"])

def get_oi(sym, price):
    data = binance_get("https://fapi.binance.com/fapi/v1/openInterest", {"symbol": sym})
    if data and "openInterest" in data:
        return float(data["openInterest"]) * price
    try:
        r = requests.get("https://api.bybit.com/v5/market/open-interest",
                         params={"category":"linear","symbol":sym,"intervalTime":"1h","limit":1}, timeout=10)
        d = r.json()
        if d.get("result") and d["result"].get("list"):
            return float(d["result"]["list"][0]["openInterest"]) * price
    except Exception:
        pass
    return price * 1_000_000

def _dec(p):
    if p>=1000: return 1
    if p>=10:   return 2
    if p>=0.01: return 4
    return 6

def build_df(coin):
    sym = coin.upper().replace("USDT","").replace("BUSD","") + "USDT"
    price = get_price(sym)
    oi    = get_oi(sym, price)
    rows  = []
    for lev,share in LEVERAGE_DIST.items():
        sl = oi*share
        for i in range(1,31):
            lp = price*(1-0.4*i/30)*(1-1/lev)
            sp = price*(1+0.4*i/30)*(1+1/lev)
            if lp>0: rows.append({"price":lp,"usd_value":sl/30,"type":"long"})
            rows.append({"price":sp,"usd_value":sl/30,"type":"short"})
    df = pd.DataFrame(rows)
    df["price"] = df["price"].round(_dec(price))
    return df.groupby(["price","type"],as_index=False)["usd_value"].sum(), price, sym

def build_chart(df, symbol, price):
    BG="#131722";GRID="#2a2e39";GREEN="#089981";RED="#f23645";GOLD="#f5c518";TEXT="#d1d4dc"
    df=df.sort_values("price").reset_index(drop=True)
    lo=df[df["type"]=="long"]; sh=df[df["type"]=="short"]
    pr=df["price"].max()-df["price"].min(); nl=len(df["price"].unique())
    bh=(pr/max(nl,1))*0.75; dec=_dec(df["price"].max())
    fig,ax=plt.subplots(figsize=(12,max(8,nl*0.18)))
    fig.patch.set_facecolor(BG); ax.set_facecolor(BG)
    ax.barh(lo["price"],lo["usd_value"],height=bh,color=RED,alpha=0.92)
    ax.barh(sh["price"],sh["usd_value"],height=bh,color=GREEN,alpha=0.92)
    ax.axhline(y=price,color=GOLD,linewidth=1.2,linestyle="--",alpha=0.9,label=f"Price: {price:,.{dec}f}")
    ax.grid(axis="x",color=GRID,linestyle="--",alpha=0.5,linewidth=0.7); ax.set_axisbelow(True)
    for s in ax.spines.values(): s.set_edgecolor(GRID)
    ax.tick_params(colors=TEXT,labelsize=9,length=3)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x,_:f"{x:,.0f}"))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda y,_:f"{y:.{dec}f}"))
    for lb in ax.get_xticklabels()+ax.get_yticklabels(): lb.set_fontfamily("monospace"); lb.set_color(TEXT)
    ax.set_xlabel("USD Value",color=TEXT,fontsize=11,fontfamily="monospace")
    ax.set_ylabel("Price",color=TEXT,fontsize=11,fontfamily="monospace")
    ax.set_title(f"Predicted Liquidation Levels – {symbol}",color=TEXT,fontsize=13,pad=14,fontfamily="monospace")
    ax.legend(facecolor=BG,edgecolor=GRID,labelcolor=TEXT,fontsize=9,loc="upper right")
    plt.tight_layout(pad=1.5)
    buf=io.BytesIO(); plt.savefig(buf,format="png",bbox_inches="tight",dpi=150,facecolor=BG)
    buf.seek(0); plt.close(fig); return buf

@dp.message(Command("start","help"))
async def cmd_start(message: types.Message):
    coins="\n".join([f"  <code>/liq {s}</code>" for s in WATCHLIST])
    await message.answer(
        "📊 <b>Liquidation Map Bot</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 Отправь <code>/liq BTC</code> или <code>/liq BTCUSDT</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🗂 <b>Доступные монеты:</b>\n{coins}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🟢 Зелёный — шорты ликвидируются → цена растёт\n"
        "🔴 Красный — лонги ликвидируются → цена падает\n"
        "🟡 Линия — текущая цена\n"
        "⚡ Автоалерт свыше <b>$500,000</b>",
        parse_mode="HTML")

@dp.message(Command("liq"))
async def cmd_liq(message: types.Message):
    parts=message.text.strip().split()
    if len(parts)<2:
        await message.reply("⚠️ Пример: <code>/liq BTC</code>",parse_mode="HTML"); return
    wait=await message.reply(f"⏳ Загружаю...",parse_mode="HTML")
    try:
        df,price,sym=build_df(parts[1])
        buf=build_chart(df,sym,price)
        ms=df[df["type"]=="short"]["usd_value"].max()
        ml=df[df["type"]=="long"]["usd_value"].max()
        dec=_dec(price)
        await bot.send_photo(message.chat.id,
            photo=BufferedInputFile(buf.read(),filename=f"liq_{sym}.png"),
            caption=(f"📊 <b>Liquidation Map — {sym}</b>\n\n"
                     f"💰 Цена: <b>${price:,.{dec}f}</b>\n"
                     f"🟢 Шорт-зона: <b>${ms:,.0f}</b>\n"
                     f"🔴 Лонг-зона:  <b>${ml:,.0f}</b>"),
            parse_mode="HTML",message_thread_id=message.message_thread_id)
    except Exception as e:
        logger.exception(e); await message.reply(f"❌ {e}")
    finally:
        await wait.delete()

async def auto_alert_loop():
    await asyncio.sleep(15)
    refresh_proxies()
    while True:
        for coin in WATCHLIST:
            try:
                df,price,sym=build_df(coin)
                ms=df[df["type"]=="short"]["usd_value"].max()
                ml=df[df["type"]=="long"]["usd_value"].max()
                if max(ms,ml)>=ALERT_THRESHOLD:
                    buf=build_chart(df,sym,price); emoji="🟢" if ms>ml else "🔴"; dec=_dec(price)
                    await bot.send_photo(ALERT_CHAT_ID,
                        photo=BufferedInputFile(buf.read(),filename=f"alert_{sym}.png"),
                        caption=(f"🚨 <b>АЛЕРТ — {sym}</b>\n\n"
                                 f"{emoji} Мощная зона!\n💰 ${price:,.{dec}f}\n"
                                 f"🟢 ${ms:,.0f}  🔴 ${ml:,.0f}"),
                        parse_mode="HTML",message_thread_id=ALERT_TOPIC_ID)
                await asyncio.sleep(2)
            except Exception as e:
                logger.warning(f"{coin}: {e}")
        await asyncio.sleep(1800)

async def main():
    asyncio.create_task(auto_alert_loop())
    logger.info("✅ Bot started!")
    await dp.start_polling(bot)

if __name__=="__main__":
    asyncio.run(main())