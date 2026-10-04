import os
import re
import time
import html
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import requests


# ============================================================
# CONFIGURATION
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

NSE_HOME = "https://www.nseindia.com"
NSE_ANNOUNCEMENTS = "https://www.nseindia.com/api/corporate-announcements"
NSE_EQUITY_LIST = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}.NS"

TELEGRAM_URL = "https://api.telegram.org/bot{}/sendMessage"

MAX_FINAL_STOCKS = 7

# Rolling window from current IST time
MAX_NEWS_AGE_HOURS = 48

# Number of best candidates that will receive price checking
MAX_PRICE_CHECKS = 25

REQUEST_TIMEOUT = 15


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_HOME + "/",
    "Origin": NSE_HOME,
    "Connection": "keep-alive",
}


# ============================================================
# HIGH IMPACT NEWS KEYWORDS
# ============================================================

VERY_HIGH_IMPACT = {
    "order win": 30,
    "large order": 30,
    "major order": 30,
    "order worth": 28,
    "contract worth": 28,
    "acquisition": 28,
    "acquires": 28,
    "acquired": 28,
    "merger": 28,
    "takeover": 30,
    "strategic investment": 25,
    "commercial production": 25,
    "commercial operations": 25,
    "regulatory approval": 25,
    "final approval": 25,
    "drug approval": 28,
    "usfda": 28,
    "us fda": 28,
}

HIGH_IMPACT = {
    "order": 20,
    "contract": 20,
    "new project": 18,
    "project win": 22,
    "business win": 22,
    "capacity expansion": 20,
    "capacity": 14,
    "expansion": 14,
    "new plant": 18,
    "plant expansion": 20,
    "production": 13,
    "fund raising": 17,
    "fundraising": 17,
    "qip": 18,
    "preferential issue": 18,
    "preferential": 15,
    "investment": 13,
    "joint venture": 18,
    "partnership": 13,
    "stake acquisition": 22,
    "stake purchase": 22,
    "buyback": 18,
    "guidance": 16,
    "revenue growth": 16,
    "profit growth": 18,
    "ebitda growth": 18,
    "turnaround": 18,
}

MEDIUM_POSITIVE = {
    "approval": 12,
    "approved": 12,
    "profit": 10,
    "revenue": 8,
    "ebitda": 9,
    "dividend": 8,
    "results": 10,
    "growth": 8,
    "capex": 10,
    "commissioned": 12,
    "commissioning": 12,
    "new facility": 12,
    "new order": 20,
    "won": 18,
    "wins": 18,
}


# ============================================================
# NEGATIVE NEWS
# ============================================================

NEGATIVE_WORDS = {
    "fraud",
    "default",
    "downgrade",
    "loss",
    "penalty",
    "fine",
    "resign",
    "resignation",
    "shutdown",
    "investigation",
    "regulatory action",
    "decline",
    "misses",
    "pledge",
    "insolvency",
    "bankruptcy",
    "litigation",
    "warning",
    "delay",
    "debt restructuring",
    "rating downgrade",
    "fire incident",
    "fire broke",
    "fire accident",
    "accident",
    "injury",
    "casualty",
}


# ============================================================
# LOW IMPACT / ROUTINE NEWS
# ============================================================

ROUTINE_WORDS = {
    "trading window",
    "shareholders meeting",
    "annual general meeting",
    "agm",
    "change in director",
    "change in directors",
    "change in management",
    "appointment of director",
    "appointment of directors",
    "secretarial audit",
    "scrutinizer",
    "postal ballot",
    "investor presentation",
    "credit rating",
    "newspaper publication",
    "compliance certificate",
    "certificate",
    "business responsibility",
    "related party",
    "board meeting intimation",
    "meeting intimation",
}


# ============================================================
# PRICE CACHE
# ============================================================

PRICE_CACHE = {}


# ============================================================
# BASIC HELPERS
# ============================================================

def clean_text(value):
    if value is None:
        return ""

    value = html.unescape(str(value))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def normalize_text(value):
    value = clean_text(value).upper()
    value = re.sub(r"[^A-Z0-9& ]+", " ", value)
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def now_ist():
    return datetime.now(IST)


def format_news_age(dt):
    if not dt:
        return "Unknown"

    seconds = int((now_ist() - dt).total_seconds())

    if seconds < 0:
        return "Future/Unknown"

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60

    if hours > 0:
        return f"{hours}h {minutes}m"

    return f"{minutes}m"


# ============================================================
# NSE STOCK LIST
# ============================================================

def get_nse_symbols():

    print("Downloading NSE equity list...")

    try:
        response = requests.get(
            NSE_EQUITY_LIST,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT
        )

        response.raise_for_status()

        text = response.content.decode(
            "utf-8-sig",
            errors="ignore"
        )

        lines = text.splitlines()

        if not lines:
            print("NSE equity list empty.")
            return []

        header = lines[0].split(",")

        symbol_index = None
        name_index = None
        series_index = None

        for i, column in enumerate(header):

            column_clean = column.strip().upper()

            if column_clean == "SYMBOL":
                symbol_index = i

            elif column_clean == "NAME OF COMPANY":
                name_index = i

            elif column_clean == "SERIES":
                series_index = i

        if symbol_index is None:
            print("SYMBOL column not found.")
            return []

        stocks = []

        for line in lines[1:]:

            parts = line.split(",")

            if len(parts) <= symbol_index:
                continue

            symbol = parts[symbol_index].strip().upper()

            series = ""

            if series_index is not None and len(parts) > series_index:
                series = parts[series_index].strip().upper()

            if series and series != "EQ":
                continue

            company_name = ""

            if name_index is not None and len(parts) > name_index:
                company_name = parts[name_index].strip()

            if symbol:

                stocks.append({
                    "symbol": symbol,
                    "name": company_name
                })

        print(f"NSE symbols loaded: {len(stocks)}")

        return stocks

    except Exception as e:

        print("NSE equity list error:", type(e).__name__, str(e))

        return []


# ============================================================
# FAST COMPANY LOOKUP
# ============================================================

def build_company_lookup(stocks):

    print("Building fast company lookup...")

    symbol_set = set()
    name_lookup = {}
    word_lookup = {}

    ignored_words = {
        "LIMITED",
        "LIMIT",
        "INDIA",
        "INDIAN",
        "CORPORATION",
        "CORP",
        "COMPANY",
        "LTD",
        "SERVICES",
        "ENTERPRISES",
        "INDUSTRIES",
        "HOLDINGS",
        "TECHNOLOGIES",
        "TECHNOLOGY",
        "PRIVATE",
        "PVT",
    }

    for stock in stocks:

        symbol = stock["symbol"]
        name = normalize_text(stock["name"])

        symbol_set.add(symbol)

        if name:
            name_lookup[name] = symbol

            for word in name.split():

                if len(word) < 5:
                    continue

                if word in ignored_words:
                    continue

                word_lookup.setdefault(word, set()).add(symbol)

    print(
        f"Symbols: {len(symbol_set)} | "
        f"Company names: {len(name_lookup)} | "
        f"Words: {len(word_lookup)}"
    )

    return {
        "symbols": symbol_set,
        "names": name_lookup,
        "words": word_lookup,
    }


# ============================================================
# NSE SESSION
# ============================================================

def create_nse_session():

    session = requests.Session()

    session.headers.update(HEADERS)

    try:
        response = session.get(
            NSE_HOME,
            timeout=REQUEST_TIMEOUT
        )

        print(
            "NSE homepage response:",
            response.status_code
        )

    except Exception as e:

        print(
            "NSE homepage warning:",
            type(e).__name__
        )

    return session


# ============================================================
# NSE ANNOUNCEMENTS
# ============================================================

def get_nse_announcements():

    print("\n----- NSE ANNOUNCEMENTS -----")

    for attempt in range(1, 4):

        print(f"NSE announcement attempt {attempt}/3...")

        session = create_nse_session()

        try:

            time.sleep(1)

            response = session.get(
                NSE_ANNOUNCEMENTS,
                params={"index": "equities"},
                timeout=20
            )

            print(
                "NSE announcements HTTP:",
                response.status_code
            )

            print(
                "NSE response size:",
                len(response.content),
                "bytes"
            )

            if response.status_code != 200:

                print(
                    "NSE returned HTTP:",
                    response.status_code
                )

                time.sleep(2)

                continue

            raw = response.text.strip()

            if not raw:

                print("NSE returned EMPTY response.")

                time.sleep(2)

                continue

            try:

                data = response.json()

            except ValueError:

                print(
                    "NSE response is NOT valid JSON."
                )

                print(
                    "First 300 characters:"
                )

                print(raw[:300])

                time.sleep(2)

                continue

            if isinstance(data, dict):

                records = data.get("data", [])

            elif isinstance(data, list):

                records = data

            else:

                records = []

            if not isinstance(records, list):

                print(
                    "Unexpected NSE data format."
                )

                time.sleep(2)

                continue

            print(
                "NSE announcements received:",
                len(records)
            )

            if records:

                return records

        except requests.exceptions.Timeout:

            print("NSE request timed out.")

        except requests.exceptions.RequestException as e:

            print(
                "NSE request error:",
                str(e)
            )

        except Exception as e:

            print(
                "NSE unexpected error:",
                type(e).__name__,
                str(e)
            )

        time.sleep(2)

    print("Could not get NSE announcements.")

    return []


# ============================================================
# DATE/TIME PARSER
# ============================================================

def parse_news_datetime(item):

    possible_fields = [
        "an_dt",
        "broadcastDate",
        "sort_date",
        "date",
        "time",
        "timestamp",
    ]

    for field in possible_fields:

        value = item.get(field)

        if not value:
            continue

        value = str(value).strip()

        # ISO format
        try:

            dt = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)

            return dt.astimezone(IST)

        except Exception:
            pass

        # RFC email format
        try:

            dt = parsedate_to_datetime(value)

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)

            return dt.astimezone(IST)

        except Exception:
            pass

        # NSE common formats
        formats = [
            "%d-%b-%Y %H:%M:%S",
            "%d-%m-%Y %H:%M:%S",
            "%d-%b-%Y %H:%M",
            "%d-%m-%Y %H:%M",
        ]

        for fmt in formats:

            try:

                dt = datetime.strptime(
                    value,
                    fmt
                )

                return dt.replace(tzinfo=IST)

            except Exception:
                pass

    return None


# ============================================================
# FRESHNESS
# ============================================================

def is_fresh_news(item):

    dt = parse_news_datetime(item)

    if dt is None:
        return True

    age = now_ist() - dt

    if age.total_seconds() < 0:
        return True

    return age <= timedelta(
        hours=MAX_NEWS_AGE_HOURS
    )


# ============================================================
# ANNOUNCEMENT TEXT
# ============================================================

def announcement_text(item):

    fields = [
        "subject",
        "desc",
        "details",
        "description",
        "attchmntText",
        "headline",
    ]

    values = []

    for field in fields:

        value = item.get(field)

        if value:

            cleaned = clean_text(value)

            if cleaned:
                values.append(cleaned)

    # Remove duplicate pieces
    final_parts = []

    seen = set()

    for value in values:

        key = normalize_text(value)

        if key in seen:
            continue

        seen.add(key)

        final_parts.append(value)

    return " ".join(final_parts)


# ============================================================
# SUMMARY
# ============================================================

def build_news_summary(item, max_len=360):

    subject = clean_text(
        item.get("subject")
        or item.get("desc")
        or item.get("headline")
        or ""
    )

    details = clean_text(
        item.get("details")
        or item.get("description")
        or item.get("attchmntText")
        or ""
    )

    if details:

        text = details

    else:

        text = announcement_text(item)

    text = clean_text(text)

    if not text:
        return subject

    # Remove repeated subject from beginning
    if subject:

        normalized_text = normalize_text(text)
        normalized_subject = normalize_text(subject)

        if normalized_text.startswith(
            normalized_subject
        ):

            text = text[len(subject):].strip(
                " :-–—."
            )

    if not text:
        return subject

    # Try sentence based summary
    sentences = re.split(
        r"(?<=[.!?])\s+",
        text
    )

    summary = ""

    for sentence in sentences:

        sentence = sentence.strip()

        if not sentence:
            continue

        candidate = (
            summary + " " + sentence
        ).strip()

        if len(candidate) <= max_len:

            summary = candidate

        else:

            break

    if summary:
        return summary

    # Hard truncate
    shortened = text[:max_len]

    if " " in shortened:
        shortened = shortened.rsplit(
            " ",
            1
        )[0]

    return shortened + "..."


# ============================================================
# STOCK IDENTIFICATION
# ============================================================

def identify_stock_fast(
    item,
    stocks,
    lookup
):

    # 1. Direct NSE symbol fields
    possible_fields = [
        "symbol",
        "symbolName",
        "ticker",
        "sm_name",
    ]

    for field in possible_fields:

        value = item.get(field)

        if not value:
            continue

        value = str(value).strip().upper()

        if value in lookup["symbols"]:
            return value

    text = announcement_text(item)

    normalized = normalize_text(text)

    # 2. Exact symbol from words
    words = set(normalized.split())

    for word in words:

        if word in lookup["symbols"]:
            return word

    # 3. Exact company name
    for company_name, symbol in lookup["names"].items():

        if len(company_name) >= 8:

            if company_name in normalized:
                return symbol

    # 4. Unique company keyword
    for word in words:

        if word not in lookup["words"]:
            continue

        possible_symbols = lookup["words"][word]

        if len(possible_symbols) == 1:

            return next(
                iter(possible_symbols)
            )

    return None


# ============================================================
# ROUTINE NEWS CHECK
# ============================================================

def routine_news(text):

    text_lower = text.lower()

    matches = []

    for word in ROUTINE_WORDS:

        if word in text_lower:
            matches.append(word)

    return matches


# ============================================================
# IMPACT SCORING
# ============================================================

def calculate_news_score(text):

    text_lower = text.lower()

    score = 0

    matched = []
    negative = []
    routine = []

    # Very high impact
    for keyword, points in VERY_HIGH_IMPACT.items():

        if keyword in text_lower:

            score += points
            matched.append(keyword)

    # High impact
    for keyword, points in HIGH_IMPACT.items():

        if keyword in text_lower:

            score += points
            matched.append(keyword)

    # Medium positive
    for keyword, points in MEDIUM_POSITIVE.items():

        if keyword in text_lower:

            score += points
            matched.append(keyword)

    # Positive diversity bonus
    unique_positive = len(set(matched))

    score += min(
        unique_positive * 2,
        12
    )

    # Negative news
    for word in NEGATIVE_WORDS:

        if word in text_lower:

            negative.append(word)

            score -= 25

    # Routine news penalty
    routine = routine_news(text)

    if routine:

        score -= min(
            len(routine) * 18,
            45
        )

    return {
        "raw_score": max(score, 0),
        "matched": list(dict.fromkeys(matched)),
        "negative": list(dict.fromkeys(negative)),
        "routine": list(dict.fromkeys(routine)),
    }


# ============================================================
# PRICE DATA
# ============================================================

def get_yahoo_price(symbol):

    if symbol in PRICE_CACHE:
        return PRICE_CACHE[symbol]

    url = YAHOO_CHART.format(symbol)

    try:

        response = requests.get(
            url,
            params={
                "range": "2d",
                "interval": "1d"
            },
            headers={
                "User-Agent":
                    HEADERS["User-Agent"]
            },
            timeout=8
        )

        if response.status_code != 200:

            PRICE_CACHE[symbol] = None

            return None

        data = response.json()

        result = (
            data
            .get("chart", {})
            .get("result")
        )

        if not result:

            PRICE_CACHE[symbol] = None

            return None

        result = result[0]

        meta = result.get(
            "meta",
            {}
        )

        price = meta.get(
            "regularMarketPrice"
        )

        previous_close = meta.get(
            "previousClose"
        )

        if price is None:

            PRICE_CACHE[symbol] = None

            return None

        change_pct = None

        if previous_close:

            change_pct = (
                (price - previous_close)
                / previous_close
            ) * 100

        result_data = {
            "price": price,
            "change_pct": change_pct
        }

        PRICE_CACHE[symbol] = result_data

        return result_data

    except Exception as e:

        print(
            f"Yahoo error {symbol}:",
            type(e).__name__
        )

        PRICE_CACHE[symbol] = None

        return None


# ============================================================
# PRICE CONFIRMATION
# ============================================================

def add_price_score(candidate):

    price_data = get_yahoo_price(
        candidate["symbol"]
    )

    candidate["price"] = None
    candidate["change_pct"] = None
    candidate["price_score"] = 0

    if not price_data:
        return candidate

    price = price_data["price"]
    change = price_data["change_pct"]

    candidate["price"] = price
    candidate["change_pct"] = change

    if change is None:
        return candidate

    # Positive price reaction
    if change >= 5:

        candidate["price_score"] = 12

    elif change >= 3:

        candidate["price_score"] = 10

    elif change >= 2:

        candidate["price_score"] = 8

    elif change >= 1:

        candidate["price_score"] = 5

    elif change > 0:

        candidate["price_score"] = 2

    # Negative reaction
    elif change <= -5:

        candidate["price_score"] = -12

    elif change <= -3:

        candidate["price_score"] = -8

    elif change < 0:

        candidate["price_score"] = -3

    return candidate


# ============================================================
# BUILD CANDIDATES
# ============================================================

def process_announcements(
    records,
    stocks
):

    print(
        f"\nProcessing {len(records)} "
        "NSE announcements..."
    )

    lookup = build_company_lookup(
        stocks
    )

    fresh_count = 0
    relevant_count = 0
    identified_count = 0
    candidate_count = 0

    stale_count = 0
    unidentified_count = 0

    candidates = []

    seen = set()

    # --------------------------------------------------------
    # STEP 1: Fresh announcements
    # --------------------------------------------------------

    processed = []

    for item in records:

        if not isinstance(item, dict):
            continue

        dt = parse_news_datetime(item)

        if not is_fresh_news(item):

            stale_count += 1

            continue

        fresh_count += 1

        processed.append(
            (
                dt
                if dt
                else datetime.min.replace(
                    tzinfo=IST
                ),
                item
            )
        )

    processed.sort(
        key=lambda x: x[0],
        reverse=True
    )

    # --------------------------------------------------------
    # STEP 2: Analyse
    # --------------------------------------------------------

    for _, item in processed:

        text = announcement_text(item)

        if not text:
            continue

        scoring = calculate_news_score(
            text
        )

        raw_score = scoring["raw_score"]

        # We do not completely reject weak news here.
        # Ranking/fallback happens later.
        if raw_score <= 0:
            continue

        relevant_count += 1

        symbol = identify_stock_fast(
            item,
            stocks,
            lookup
        )

        if not symbol:

            unidentified_count += 1

            continue

        identified_count += 1

        dt = parse_news_datetime(item)

        title = clean_text(
            item.get("subject")
            or item.get("desc")
            or item.get("headline")
            or text[:180]
        )

        summary = build_news_summary(
            item
        )

        # Duplicate protection
        duplicate_key = (
            symbol,
            normalize_text(title)
        )

        if duplicate_key in seen:
            continue

        seen.add(duplicate_key)

        candidate = {
            "symbol": symbol,
            "title": title,
            "summary": summary,
            "datetime": dt,

            "base_score": min(
                raw_score,
                88
            ),

            "positive": scoring[
                "matched"
            ],

            "negative": scoring[
                "negative"
            ],

            "routine": scoring[
                "routine"
            ],

            "price": None,
            "change_pct": None,
            "price_score": 0,
        }

        candidates.append(candidate)

        candidate_count += 1

    print(
        "\n----- NSE PROCESSING COMPLETE -----"
    )

    print("Fresh:", fresh_count)
    print("Stale:", stale_count)
    print("Relevant:", relevant_count)
    print("Identified:", identified_count)
    print("Unidentified:", unidentified_count)
    print("Candidates:", candidate_count)

    # --------------------------------------------------------
    # Initial ranking before price
    # --------------------------------------------------------

    candidates.sort(
        key=lambda x: x["base_score"],
        reverse=True
    )

    candidates = candidates[
        :MAX_PRICE_CHECKS
    ]

    print(
        "Candidates sent for price check:",
        len(candidates)
    )

    # --------------------------------------------------------
    # Price confirmation
    # --------------------------------------------------------

    for index, candidate in enumerate(
        candidates,
        start=1
    ):

        print(
            f"Price check "
            f"{index}/{len(candidates)}: "
            f"{candidate['symbol']}"
        )

        add_price_score(candidate)

        candidate["final_score"] = min(
            100,
            max(
                0,
                candidate["base_score"]
                + candidate["price_score"]
            )
        )

        # Negative news gets an additional penalty
        if candidate["negative"]:

            candidate["final_score"] = max(
                0,
                candidate["final_score"] - 15
            )

        # Routine news gets additional penalty
        if candidate["routine"]:

            candidate["final_score"] = max(
                0,
                candidate["final_score"] - 10
            )

    candidates.sort(
        key=lambda x: (
            x["final_score"],
            x["price_score"],
            x["base_score"]
        ),
        reverse=True
    )

    return candidates


# ============================================================
# SMART FALLBACK SELECTION
# ============================================================

def select_final_candidates(
    candidates
):

    if not candidates:
        return []

    # --------------------------------------------------------
    # Tier 1: Very strong
    # --------------------------------------------------------

    tier1 = [
        x for x in candidates
        if x["final_score"] >= 75
    ]

    if tier1:

        print(
            "Tier 1 selected:",
            len(tier1)
        )

        return tier1[
            :MAX_FINAL_STOCKS
        ]

    # --------------------------------------------------------
    # Tier 2: Strong / Good
    # --------------------------------------------------------

    tier2 = [
        x for x in candidates
        if x["final_score"] >= 60
    ]

    if tier2:

        print(
            "Tier 1 empty -> Tier 2 selected:",
            len(tier2)
        )

        return tier2[
            :MAX_FINAL_STOCKS
        ]

    # --------------------------------------------------------
    # Tier 3: Acceptable
    # --------------------------------------------------------

    tier3 = [
        x for x in candidates
        if x["final_score"] >= 50
    ]

    if tier3:

        print(
            "Tier 2 empty -> Tier 3 selected:",
            len(tier3)
        )

        return tier3[
            :MAX_FINAL_STOCKS
        ]

    # --------------------------------------------------------
    # FINAL FALLBACK
    #
    # Never intentionally return zero if we have a usable
    # positive candidate.
    # --------------------------------------------------------

    usable = [
        x for x in candidates
        if x["final_score"] > 0
    ]

    if usable:

        print(
            "Strict tiers empty -> "
            "Best available candidates selected:",
            len(usable)
        )

        return usable[
            :MAX_FINAL_STOCKS
        ]

    return []


# ============================================================
# NSE VERIFICATION LINK
# ============================================================

def nse_verification_link(symbol):

    return (
        "https://www.nseindia.com/"
        "companies-listing/"
        "corporate-filings-announcements"
        "?symbol="
        + quote_plus(symbol)
        + "&tabIndex=equity"
    )


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    token = os.getenv(
        "TELEGRAM_BOT_TOKEN"
    )

    chat_id = os.getenv(
        "TELEGRAM_CHAT_ID"
    )

    if not token or not chat_id:

        print(
            "Telegram credentials missing."
        )

        return False

    url = TELEGRAM_URL.format(
        token
    )

    try:

        response = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT
        )

        print(
            "Telegram HTTP:",
            response.status_code
        )

        if response.status_code != 200:

            print(
                response.text[:500]
            )

            return False

        return True

    except Exception as e:

        print(
            "Telegram error:",
            type(e).__name__,
            str(e)
        )

        return False


# ============================================================
# TELEGRAM MESSAGE
# ============================================================

def build_message(
    final_candidates,
    data_available=True
):

    current_time = now_ist().strftime(
        "%d-%m-%Y %H:%M:%S"
    )

    lines = []

    lines.append(
        "<b>🚨 HIGH-IMPACT STOCK NEWS</b>"
    )

    lines.append(
        f"🕒 Bot Time: {current_time} IST"
    )

    lines.append(
        f"🔎 News Window: Last "
        f"{MAX_NEWS_AGE_HOURS} Hours"
    )

    lines.append("")

    if not data_available:

        lines.append(
            "⚠️ <b>NSE NEWS DATA UNAVAILABLE</b>"
        )

        lines.append(
            "No fresh verified NSE data was received."
        )

        lines.append(
            "Please check the bot/API."
        )

        lines.append("")

        lines.append(
            "Made by Prakash Kanki"
        )

        return "\n".join(lines)

    if not final_candidates:

        lines.append(
            "⚠️ No usable positive candidate "
            "was found in the available NSE data."
        )

        lines.append(
            "The bot did not invent or force a stock."
        )

        lines.append("")

        lines.append(
            "Made by Prakash Kanki"
        )

        return "\n".join(lines)

    for index, item in enumerate(
        final_candidates,
        start=1
    ):

        symbol = html.escape(
            item["symbol"]
        )

        title = html.escape(
            item["title"]
        )

        summary = html.escape(
            item["summary"]
        )

        score = item[
            "final_score"
        ]

        dt = item[
            "datetime"
        ]

        price = item.get(
            "price"
        )

        change_pct = item.get(
            "change_pct"
        )

        # ----------------------------------------------------
        # Confidence label
        # ----------------------------------------------------

        if score >= 75:

            confidence = "🔥 VERY HIGH"

        elif score >= 60:

            confidence = "🟢 HIGH"

        elif score >= 50:

            confidence = "🟡 GOOD"

        else:

            confidence = "⚠️ LOWER"

        lines.append(
            f"<b>{index}. {symbol}</b>"
        )

        lines.append(
            f"🎯 Impact Score: "
            f"<b>{score}/100</b> "
            f"{confidence}"
        )

        # News time
        if dt:

            news_time = dt.strftime(
                "%d-%m-%Y %H:%M:%S"
            )

            lines.append(
                f"🕒 News Time: "
                f"{news_time} IST"
            )

            lines.append(
                f"⏱ Age: "
                f"{format_news_age(dt)}"
            )

        lines.append(
            f"📰 <b>{title}</b>"
        )

        if summary:

            # Prevent extremely long Telegram messages
            if len(summary) > 650:

                summary = (
                    summary[:647]
                    + "..."
                )

            lines.append(
                f"📝 {summary}"
            )

        if price is not None:

            if change_pct is not None:

                lines.append(
                    f"💰 Price: "
                    f"₹{price:.2f} "
                    f"({change_pct:+.2f}%)"
                )

            else:

                lines.append(
                    f"💰 Price: "
                    f"₹{price:.2f}"
                )

        if item["positive"]:

            positive_text = ", ".join(
                item["positive"][:6]
            )

            lines.append(
                "🔥 Trigger: "
                + html.escape(
                    positive_text
                )
            )

        if item["negative"]:

            negative_text = ", ".join(
                item["negative"][:4]
            )

            lines.append(
                "⚠️ Risk: "
                + html.escape(
                    negative_text
                )
            )

        if item["routine"]:

            routine_text = ", ".join(
                item["routine"][:3]
            )

            lines.append(
                "ℹ️ Routine element: "
                + html.escape(
                    routine_text
                )
            )

        verify_url = nse_verification_link(
            item["symbol"]
        )

        lines.append(
            f'🔗 <a href="{html.escape(verify_url)}">'
            "Verify Original NSE Filing"
            "</a>"
        )

        lines.append("")

    lines.append(
        "📌 Ranking considers news impact, "
        "freshness and price reaction."
    )

    lines.append(
        "⚠️ Research/watchlist only. "
        "Not investment advice."
    )

    lines.append(
        "Made by Prakash Kanki"
    )

    message = "\n".join(lines)

    # Telegram maximum is around 4096 characters.
    # Keep safe margin.
    if len(message) > 4000:

        print(
            "Telegram message too long. "
            "Trimming..."
        )

        message = message[:3950]

        message += (
            "\n\n⚠️ Message trimmed."
        )

    return message


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)

    print(
        "HIGH-IMPACT STOCK NEWS BOT STARTED"
    )

    print(
        "Current IST:",
        now_ist().isoformat()
    )

    print(
        "News window:",
        MAX_NEWS_AGE_HOURS,
        "hours"
    )

    print("=" * 60)

    # --------------------------------------------------------
    # NSE STOCKS
    # --------------------------------------------------------

    stocks = get_nse_symbols()

    if not stocks:

        print(
            "ERROR: NSE stock list unavailable."
        )

        message = build_message(
            [],
            data_available=False
        )

        send_telegram(message)

        return

    # --------------------------------------------------------
    # NSE NEWS
    # --------------------------------------------------------

    records = get_nse_announcements()

    if not records:

        print(
            "ERROR: NSE announcements unavailable."
        )

        message = build_message(
            [],
            data_available=False
        )

        send_telegram(message)

        return

    # --------------------------------------------------------
    # PROCESS
    # --------------------------------------------------------

    candidates = process_announcements(
        records,
        stocks
    )

    print(
        "\n----- RANKED CANDIDATES -----"
    )

    for item in candidates[:15]:

        print(
            item["symbol"],
            "| Impact:",
            item["final_score"],
            "| Base:",
            item["base_score"],
            "| Price:",
            item["change_pct"]
        )

    # --------------------------------------------------------
    # SMART FALLBACK
    # --------------------------------------------------------

    final_candidates = (
        select_final_candidates(
            candidates
        )
    )

    print(
        "\n----- FINAL RESULT -----"
    )

    print(
        "Final stocks:",
        len(final_candidates)
    )

    for item in final_candidates:

        print(
            item["symbol"],
            "| Score:",
            item["final_score"],
            "| Price:",
            item["change_pct"]
        )

    # --------------------------------------------------------
    # TELEGRAM
    # --------------------------------------------------------

    message = build_message(
        final_candidates,
        data_available=True
    )

    print(
        "\n----- SENDING TELEGRAM -----"
    )

    success = send_telegram(
        message
    )

    if success:

        print(
            "Telegram message sent successfully."
        )

    else:

        print(
            "Telegram message was NOT sent."
        )

    print(
        "\nBOT RUN COMPLETE"
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
