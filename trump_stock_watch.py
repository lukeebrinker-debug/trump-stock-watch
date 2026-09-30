#!/usr/bin/env python3
"""
Trump stock-mention watcher -> ntfy (and optional Telegram).

  python trump_stock_watch.py            # check; alert only on NEW items
  python trump_stock_watch.py --daily    # check; ALWAYS send a status report

Env vars: NTFY_TOPIC (required), TELEGRAM_TOKEN / TELEGRAM_CHAT_ID (optional)
Not financial advice. Alerts are only as fast as GitHub's scheduler (~5-15 min).
"""
import json, os, re, sys, time
from pathlib import Path
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "change-me")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SEEN_FILE = Path(__file__).with_name("seen_trump.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

ALERT_ON_MARKET_WORDS = True   # also alert on Trump posts using words like "stock", "shares", "CEO"

# ---- Where Trump's posts come from (he posts on Truth Social). Mirrors can change/disappear;
# ---- the daily report tells you if one stops working.
TRUTH_FEEDS = [
    "https://trumpstruth.org/feed",
    "https://truthsocial.com/@realDonaldTrump.rss",
]

# ---- News searches (Google News RSS, free, no account)
NEWS_QUERIES = [
    "Trump stock shares surge",
    "Trump Truth Social company shares jump",
    "Trump praises CEO company stock",
    "Trump stock soars after Trump post",
    "Trump says buy stock",
    "Trump stake company shares",
]

# ---- Company name -> ticker. Add your own!
WATCHLIST = {
    "Apple": "AAPL", "Microsoft": "MSFT", "Amazon": "AMZN", "Alphabet": "GOOGL", "Google": "GOOGL",
    "Meta": "META", "Nvidia": "NVDA", "Tesla": "TSLA", "Intel": "INTC", "AMD": "AMD",
    "Dell": "DELL", "IBM": "IBM", "Oracle": "ORCL", "Cisco": "CSCO", "Micron": "MU",
    "Qualcomm": "QCOM", "Broadcom": "AVGO", "Texas Instruments": "TXN", "Palantir": "PLTR",
    "Coinbase": "COIN", "Trump Media": "DJT", "Boeing": "BA", "Lockheed Martin": "LMT",
    "Ford": "F", "General Motors": "GM", "Harley-Davidson": "HOG", "Caterpillar": "CAT",
    "Walmart": "WMT", "Costco": "COST", "Nike": "NKE", "Starbucks": "SBUX", "McDonald's": "MCD",
    "Coca-Cola": "KO", "Disney": "DIS", "Netflix": "NFLX", "Comcast": "CMCSA",
    "Paramount": "PARA", "Warner Bros": "WBD", "Pfizer": "PFE", "Moderna": "MRNA",
    "Exxon": "XOM", "Chevron": "CVX", "JPMorgan": "JPM", "Goldman Sachs": "GS",
    "Bank of America": "BAC", "Citigroup": "C", "Wells Fargo": "WFC", "Uber": "UBER",
    "MP Materials": "MP", "Lithium Americas": "LAC", "Intel Corporation": "INTC",
    "SoftBank": "SFTBY", "Robinhood": "HOOD", "GameStop": "GME",
}
NAME_RES = {n: re.compile(r"(?<![A-Za-z])" + re.escape(n) + r"(?![A-Za-z])") for n in WATCHLIST}
CASHTAG_RE = re.compile(r"\$([A-Z]{1,5})\b")
MARKET_WORDS = re.compile(r"\b(stocks?|shares?|invest(?:ing|ment|ors?)?|CEO|buy|stake|Wall Street|"
                          r"tariff exempt\w*|deal)\b", re.I)
MOVE_WORDS = re.compile(r"\b(surg\w+|soar\w+|jump\w+|rall\w+|spik\w+|plung\w+|tumbl\w+|"
                        r"shares|stock)\b", re.I)


def find_tickers(text):
    found = {WATCHLIST[n] for n, rx in NAME_RES.items() if rx.search(text)}
    found |= set(CASHTAG_RE.findall(text))
    return sorted(found)


def get(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def parse_rss(xml):
    soup = BeautifulSoup(xml, "xml")
    items = []
    for it in soup.find_all("item"):
        title = it.title.text if it.title else ""
        desc = BeautifulSoup(it.description.text, "html.parser").get_text(" ", strip=True) if it.description else ""
        link = it.link.text if it.link else (it.guid.text if it.guid else title)
        items.append((title, desc, link))
    return items


def quote(ticker):
    """Best-effort price move from Yahoo (unofficial; may fail silently)."""
    try:
        d = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                         "?interval=1d&range=5d&includePrePost=true",
                         headers=HEADERS, timeout=15).json()
        m = d["chart"]["result"][0]["meta"]
        px, prev = m["regularMarketPrice"], m["chartPreviousClose"]
        return f"{ticker} ${px:.2f} ({(px - prev) / prev * 100:+.1f}%)"
    except Exception:
        return ticker


def notify(title, body, url=None, priority="default"):
    try:
        h = {"Title": title.encode("utf-8"), "Priority": priority}
        if url and url.startswith("http"):
            h["Click"] = url
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"), headers=h, timeout=20)
    except Exception as e:
        print("ntfy failed:", e)
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          data={"chat_id": TELEGRAM_CHAT_ID,
                                "text": f"{title}\n{body}" + (f"\n{url}" if url else "")}, timeout=20)
        except Exception as e:
            print("telegram failed:", e)


def main():
    daily = "--daily" in sys.argv
    seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()
    first_run = not SEEN_FILE.exists()
    alerts, status, scanned = [], [], 0

    # 1) Trump's own posts
    for feed in TRUTH_FEEDS:
        try:
            items = parse_rss(get(feed))
            scanned += len(items)
            status.append(f"{feed.split('/')[2]}: {len(items)} posts" if items
                          else f"{feed.split('/')[2]}: 0 posts (check)")
            for title, desc, link in items:
                text = f"{title} {desc}"
                key = "post:" + link
                if key in seen:
                    continue
                seen.add(key)
                tickers = find_tickers(text)
                if tickers:
                    alerts.append(("TRUMP POST MENTIONS " + ", ".join(tickers), text[:280], link, "urgent", tickers))
                elif ALERT_ON_MARKET_WORDS and MARKET_WORDS.search(text):
                    alerts.append(("Trump post (market words)", text[:280], link, "default", []))
        except Exception as e:
            status.append(f"{feed.split('/')[2]}: FAILED ({type(e).__name__})")

    # 2) News about Trump + a stock move
    for q in NEWS_QUERIES:
        try:
            url = ("https://news.google.com/rss/search?q=" + quote_plus(q + " when:1d")
                   + "&hl=en-US&gl=US&ceid=US:en")
            items = parse_rss(get(url))
            scanned += len(items)
            for title, desc, link in items:
                key = "news:" + title.lower()[:90]
                if key in seen:
                    continue
                seen.add(key)
                tickers = find_tickers(title)
                if "trump" in title.lower() and tickers and MOVE_WORDS.search(title):
                    alerts.append(("NEWS: Trump + " + ", ".join(tickers), title, link, "high", tickers))
        except Exception as e:
            status.append(f"news '{q[:20]}': FAILED ({type(e).__name__})")
        time.sleep(1)

    SEEN_FILE.write_text(json.dumps(sorted(seen)[-5000:]))

    if first_run and not daily:
        alerts = []   # don't spam old items on the very first run

    for title, body, link, prio, tickers in alerts:
        prices = " | ".join(quote(t) for t in tickers[:4])
        notify(title, (prices + "\n" if prices else "") + body, link, prio)

    if daily:
        msg = (("Nothing notable today.\n" if not alerts else f"{len(alerts)} alerts today.\n")
               + f"Scanned {scanned} items.\nSources: " + "; ".join(status or ["none"]))
        notify("Trump stock watch - daily report", msg)


if __name__ == "__main__":
    main()
