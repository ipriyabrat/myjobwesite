"""
current_affairs.py
──────────────────
1. Opens SanskritiIAS daily page to get all article links for today
2. Scrapes each article individually
3. Summarizes each article into 1 exam-ready line via EURI API

URL pattern: https://www.sanskritiias.com/current-affairs/date/DD-Month-YYYY
"""

import re
import sys
import io
import os
import requests
from dotenv import load_dotenv

# Fix Windows console encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from bs4 import BeautifulSoup
from datetime import date
from openai import OpenAI

load_dotenv()

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
# Reads from .env locally, or from GitHub Actions secrets in CI
EURI_API_KEY       = os.getenv("EURI_API_KEY", "euri-4dd26b0381b5ff303d9fae23f5a47696b1f2dbae1aabfc0bbb5082706cc7d6b4")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

client = OpenAI(
    api_key=EURI_API_KEY,
    base_url="https://api.euron.one/api/v1/euri"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}
REQUEST_TIMEOUT = 20

SYSTEM_PROMPT = (
    "You are a current affairs expert for Indian competitive exams (SSC, Railway, Banking). "
    "Read the article and write exactly 1 original sentence in your own words — do NOT copy any sentence from the article. "
    "Rewrite the key fact uniquely, as if explaining it to a student for the first time. "
    "The sentence must capture the most important fact useful for exam MCQs. "
    "Keep it simple, clear, and under 30 words. "
    "Do NOT number it. Do NOT add extra lines."
)

_BASE_DATE_URL = "https://www.sanskritiias.com/current-affairs/date/{date}"

_STRIP_TAGS = [
    "script", "style", "nav", "header", "footer",
    "noscript", "iframe", "form", "button", "aside",
]

_SKIP_LINES = {
    "prev", "next", "tags:", "share", "print", "home", "back",
    "read more", "also read", "* * *",
}

_SKIP_PREFIXES = re.compile(
    r"^(source\s*:|tags\s*:|gs paper|quick facts|note\s*:|"
    r"upsc civil services|previous year question|pyq|practice question|faq|"
    r"gs1|gs2|gs3|gs4|prelims|mains|q\.)\b",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────
#  URL BUILDER
# ─────────────────────────────────────────────
def _daily_url(for_date: date) -> str:
    day   = str(for_date.day)
    month = for_date.strftime("%B")
    year  = for_date.strftime("%Y")
    return _BASE_DATE_URL.format(date=f"{day}-{month}-{year}")


# ─────────────────────────────────────────────
#  STEP 1: Get all article links for the day
# ─────────────────────────────────────────────
def get_article_links(for_date: date) -> list:
    """
    Returns list of (title, url) for each article on today's daily page.
    """
    url = _daily_url(for_date)
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        raise SystemExit(f"[ERROR] Cannot fetch daily page: {e}")

    soup = BeautifulSoup(r.text, "html.parser")

    articles = []
    seen = set()

    # Article links are /current-affairs/<slug> (no 'date' in path)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if (
            "sanskritiias.com/current-affairs/" in href
            and "/date/" not in href
            and "/category/" not in href
            and href not in seen
        ):
            title = a.get_text(strip=True)
            if len(title) > 20:          # skip nav/icon links
                articles.append((title, href))
                seen.add(href)

    return articles


# ─────────────────────────────────────────────
#  STEP 2: Scrape a single article page
# ─────────────────────────────────────────────
def scrape_article(url: str) -> str:
    """Return cleaned text of a single article (capped at 6000 chars)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        return ""

    soup = BeautifulSoup(r.text, "html.parser")

    for tag in soup(_STRIP_TAGS):
        tag.decompose()

    content = None
    for sel in [".content", ".entry-content", ".post-content", "article", "main"]:
        content = soup.select_one(sel)
        if content:
            break
    if not content:
        content = soup.find("body") or soup

    lines = []
    for el in content.find_all(["h2", "h3", "h4", "p", "li", "td"]):
        line = el.get_text(strip=True)
        if not line or len(line) < 5:
            continue
        if line.lower().rstrip(":") in _SKIP_LINES:
            continue
        if _SKIP_PREFIXES.match(line):
            continue
        lines.append(line)

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:6000]


# ─────────────────────────────────────────────
#  STEP 3: Summarize one article → 1 line
# ─────────────────────────────────────────────
def summarize_one(title: str, text: str) -> str:
    prompt = f"Article Title: {title}\n\n{text}"
    try:
        response = client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=120,
            temperature=0.4,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"[AI error: {e}]"


# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────
EMOJI_NUMBERS = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]

def build_telegram_message(results: list, today: date) -> str:
    date_str = today.strftime("%d %B %Y")
    lines = [
        "📰 <b>DAILY CURRENT AFFAIRS</b>",
        f"📅 <b>{date_str}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "🎯 <b>Important for SSC | Railway | Banking</b>",
        "",
    ]

    for i, (title, summary) in enumerate(results):
        emoji = EMOJI_NUMBERS[i] if i < len(EMOJI_NUMBERS) else f"{i+1}."
        lines.append(f"{emoji} <b>{title}</b>")
        lines.append(f"➤ {summary}")
        lines.append("")

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🔔 <b>Subscribe:</b> @jobnewsinodisha",
        "📲 <b>More updates daily — Stay ahead!</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)


def send_to_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [SKIP] Telegram credentials not set in .env")
        return False

    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "HTML",
    })
    if resp.status_code == 200:
        print("  Sent to Telegram successfully!")
        return True
    else:
        print(f"  [ERROR] Telegram send failed: {resp.text}")
        return False


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    import sys as _sys
    from datetime import timedelta
    # Optional: pass a date arg like  python current_affairs.py 2026-04-08
    if len(_sys.argv) > 1:
        today = date.fromisoformat(_sys.argv[1])
    else:
        today = date.today()

    print("=" * 65)
    print(f"  DAILY CURRENT AFFAIRS  —  {today.strftime('%d %B %Y')}")
    print("=" * 65)

    print("  Fetching article list...")
    articles = get_article_links(today)

    if not articles:
        raise SystemExit("[ERROR] No articles found for today.")

    print(f"  Found {len(articles)} articles\n")

    results = []
    for i, (title, url) in enumerate(articles, 1):
        print(f"  [{i}/{len(articles)}] {title[:60]}...")
        text = scrape_article(url)
        if not text:
            print("         Skipped (no content)")
            continue
        summary = summarize_one(title, text)
        results.append((title, summary))

    # Print to console
    print("\n" + "=" * 65)
    print("  EXAM-READY SUMMARY")
    print("=" * 65)
    for i, (title, summary) in enumerate(results, 1):
        print(f"\n{i}. {title}")
        print(f"   → {summary}")
    print("\n" + "=" * 65)

    # Send to Telegram
    message = build_telegram_message(results, today)
    send_to_telegram(message)

    print("\n  Done!")


if __name__ == "__main__":
    main()
