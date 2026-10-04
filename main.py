import os
import re
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote


# ============================================================
# CONFIG
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

NSE_HOME = "https://www.nseindia.com"

NSE_ANNOUNCEMENTS = (
    "https://www.nseindia.com/api/corporate-announcements"
)

NSE_EQUITY_LIST = (
    "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
)

TELEGRAM_API = "https://api.telegram.org"

MAX_FINAL_STOCKS = 7

MAX_NEWS_AGE_HOURS = 48

MAX_PRICE_CHECKS = 25

REQUEST_TIMEOUT = 15


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ============================================================
# SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_HOME + "/",
    "Connection": "keep-alive",
})


# ============================================================
# TIME
# ============================================================

def now_ist():
    return datetime.now(IST)


def format_ist(dt):
    if not dt:
        return "Unknown"

    return dt.astimezone(IST).strftime(
        "%d-%m-%Y %H:%M:%S IST"
    )


def news_age_text(dt):
    if not dt:
        return "Unknown"

    now = now_ist()

    diff = now - dt.astimezone(IST)

    if diff.total_seconds() < 0:
        return "0m"

    minutes = int(diff.total_seconds() / 60)

    days = minutes // 1440
    hours = (minutes % 1440) // 60
    mins = minutes % 60

    if days > 0:
        return f"{days}d {hours}h"

    return f"{hours}h {mins}m"


# ============================================================
# MARKET / PRICE LABEL
# ============================================================

def get_price_label():

    now = now_ist()

    # Saturday / Sunday
    if now.weekday() >= 5:
        return "Last Traded Close"

    # Before market opens
    if now.hour < 9 or (
        now.hour == 9 and now.minute < 15
    ):
        return "Previous Close"

    # After market closes
    if now.hour > 15 or (
        now.hour == 15 and now.minute >= 30
    ):
        return "Last Traded Close"

    return "Recent Price"


# ============================================================
# TEXT NORMALIZATION
# ============================================================

def normalize_text(text):

    if text is None:
        return ""

    text = str(text).lower()

    text = text.replace("&amp;", " and ")

    text = re.sub(r"<[^>]+>", " ", text)

    text = re.sub(r"[^a-z0-9]+", " ", text)

    text = re.sub(r"\s+", " ", text)

    return text.strip()


# ============================================================
# ANNOUNCEMENT TEXT
# ============================================================

def announcement_text(item):

    fields = [
        "subject",
        "Subject",
        "desc",
        "description",
        "details",
        "Details",
        "attchmntText",
        "attachmentText",
        "headline",
        "title",
        "remark",
        "remarks",
    ]

    parts = []

    for field in fields:

        value = item.get(field)

        if value:
            parts.append(str(value))

    return " ".join(parts)


# ============================================================
# NSE DATE PARSER
# ============================================================

def parse_news_datetime(item):

    possible_fields = [
        "an_dt",
        "broadcastDate",
        "broadcast_date",
        "broadcastDateTime",
        "sort_date",
        "date",
        "time",
        "timestamp",
        "exchangeReceivedTime",
        "exchange_received_time",
    ]

    for field in possible_fields:

        value = item.get(field)

        if not value:
            continue

        value = str(value).strip()

        # Unix timestamp
        if value.isdigit():

            try:

                number = int(value)

                if number > 10_000_000_000:
                    number = number / 1000

                return datetime.fromtimestamp(
                    number,
                    tz=IST
                )

            except Exception:
                pass

        formats = [
            "%d-%b-%Y %H:%M:%S",
            "%d-%b-%Y %H:%M",
            "%d-%m-%Y %H:%M:%S",
            "%d-%m-%Y %H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
        ]

        for fmt in formats:

            try:

                dt = datetime.strptime(value, fmt)

                return dt.replace(tzinfo=IST)

            except Exception:
                continue

    return None


# ============================================================
# FRESHNESS
# ============================================================

def is_fresh_news(dt):

    if not dt:
        return False

    now = now_ist()

    age = now - dt.astimezone(IST)

    return (
        age.total_seconds() >= 0
        and age <= timedelta(hours=MAX_NEWS_AGE_HOURS)
    )


# ============================================================
# NSE SYMBOL / COMPANY IDENTIFICATION
# ============================================================

def identify_stock_fast(item, stocks, lookup):

    """
    SAFE IDENTIFICATION

    Priority:

    1. Direct NSE symbol field
    2. Direct company-name field
    3. Exact full company name
    4. Exact symbol inside text

    IMPORTANT:
    Generic single-word company matching is NOT used.

    If confidence is low:
        return None

    Wrong stock assignment is worse than missing a stock.
    """

    valid_symbols = set(stocks)

    # --------------------------------------------------------
    # 1. DIRECT NSE SYMBOL
    # --------------------------------------------------------

    direct_symbol_fields = [
        "symbol",
        "Symbol",
        "ticker",
        "Ticker",
        "sm_symbol",
        "smSymbol",
        "securitySymbol",
        "security_symbol",
        "scripSymbol",
        "scrip_symbol",
    ]

    for field in direct_symbol_fields:

        value = item.get(field)

        if not value:
            continue

        symbol = str(value).strip().upper()

        symbol = re.sub(
            r"[^A-Z0-9&\-]",
            "",
            symbol
        )

        if symbol in valid_symbols:

            return symbol

    # --------------------------------------------------------
    # FULL ANNOUNCEMENT TEXT
    # --------------------------------------------------------

    text = announcement_text(item)

    normalized_text = normalize_text(text)

    # --------------------------------------------------------
    # 2. DIRECT COMPANY NAME FIELDS
    # --------------------------------------------------------

    company_fields = [
        "companyName",
        "company_name",
        "company",
        "CompanyName",
        "symbolName",
        "symbol_name",
        "sm_name",
        "securityName",
        "security_name",
        "issuerName",
        "issuer_name",
    ]

    for field in company_fields:

        value = item.get(field)

        if not value:
            continue

        company_name = normalize_text(str(value))

        if len(company_name) < 8:
            continue

        symbol = lookup["names"].get(
            company_name
        )

        if symbol and symbol in valid_symbols:
            return symbol

        if company_name in normalized_text:

            symbol = lookup["names"].get(
                company_name
            )

            if symbol and symbol in valid_symbols:
                return symbol

    # --------------------------------------------------------
    # 3. EXACT FULL COMPANY NAME
    # --------------------------------------------------------

    for company_name, symbol in lookup["names"].items():

        if not company_name:
            continue

        if len(company_name) < 10:
            continue

        if company_name in normalized_text:

            if symbol in valid_symbols:
                return symbol

    # --------------------------------------------------------
    # 4. EXACT SYMBOL IN TEXT
    # --------------------------------------------------------

    words = set(normalized_text.split())

    for symbol in valid_symbols:

        if not symbol:
            continue

        if len(symbol) < 3:
            continue

        if symbol.lower() in words:
            return symbol

    # --------------------------------------------------------
    # FAILED
    # --------------------------------------------------------

    print(
        "⚠️ STOCK IDENTIFICATION FAILED"
    )

    print(
        text[:500]
    )

    return None


# ============================================================
# BUILD NSE COMPANY LOOKUP
# ============================================================

def load_equity_list():

    print("Downloading NSE equity list...")

    try:

        response = session.get(
            NSE_EQUITY_LIST,
            timeout=REQUEST_TIMEOUT
        )

        response.raise_for_status()

        lines = response.text.splitlines()

        if len(lines) < 2:
            return [], {
                "names": {}
            }

        header = lines[0].split(",")

        symbol_index = None
        name_index = None
        series_index = None

        for i, col in enumerate(header):

            col_clean = col.strip().upper()

            if col_clean == "SYMBOL":
                symbol_index = i

            elif col_clean in [
                "NAME OF COMPANY",
                "NAME_OF_COMPANY"
            ]:
                name_index = i

            elif col_clean == "SERIES":
                series_index = i

        stocks = []

        names = {}

        import csv
        from io import StringIO

        reader = csv.reader(
            StringIO(response.text)
        )

        rows = list(reader)

        if not rows:
            return [], {
                "names": {}
            }

        header = rows[0]

        indexes = {
            h.strip().upper(): i
            for i, h in enumerate(header)
        }

        symbol_index = indexes.get(
            "SYMBOL"
        )

        name_index = indexes.get(
            "NAME OF COMPANY"
        )

        series_index = indexes.get(
            "SERIES"
        )

        for row in rows[1:]:

            try:

                if (
                    symbol_index is None
                    or symbol_index >= len(row)
                ):
                    continue

                symbol = (
                    row[symbol_index]
                    .strip()
                    .upper()
                )

                if not symbol:
                    continue

                if series_index is not None:

                    if series_index < len(row):

                        series = (
                            row[series_index]
                            .strip()
                            .upper()
                        )

                        if series and series != "EQ":
                            continue

                stocks.append(symbol)

                if (
                    name_index is not None
                    and name_index < len(row)
                ):

                    company_name = (
                        row[name_index]
                        .strip()
                    )

                    normalized_name = (
                        normalize_text(
                            company_name
                        )
                    )

                    if normalized_name:

                        names[
                            normalized_name
                        ] = symbol

            except Exception:
                continue

        print(
            f"Equity lookup entries: {len(stocks)}"
        )

        return stocks, {
            "names": names
        }

    except Exception as e:

        print(
            "❌ Equity list error:",
            e
        )

        return [], {
            "names": {}
        }


# ============================================================
# NSE ANNOUNCEMENTS
# ============================================================
# ============================================================
# NSE ANNOUNCEMENTS
# ============================================================

def get_nse_announcements():

    print(
        "Fetching NSE corporate announcements..."
    )

    try:

        response = session.get(
            NSE_ANNOUNCEMENTS,
            params={
                "index": "equities"
            },
            timeout=REQUEST_TIMEOUT
        )

        print(
            "NSE HTTP:",
            response.status_code
        )

        if response.status_code != 200:

            print(
                "❌ NSE announcement request failed"
            )

            return None

        data = response.json()

        if isinstance(data, dict):

            records = data.get(
                "data",
                []
            )

        elif isinstance(data, list):

            records = data

        else:

            records = []

        print(
            "NSE records:",
            len(records)
        )

        # ----------------------------------------------------
        # DEBUG: FIRST NSE RECORD
        # ----------------------------------------------------

        if records:

            print("FIRST NSE RECORD:")
            print(records[0])

            # ------------------------------------------------
            # DEBUG: SICAL RECORD
            # ------------------------------------------------

            for x in records:

                if (
                    "Sical" in str(x)
                    or "SICALLOG" in str(x)
                ):

                    print("SICAL RECORD:")
                    print(x)

        return records

    except Exception as e:

        print(
            "❌ NSE announcement error:",
            e
        )

        return None

# ============================================================
# NEWS CATEGORIES
# ============================================================

VERY_HIGH_IMPACT = {

    "large order": 25,
    "major order": 25,
    "mega order": 25,
    "order win": 22,
    "order received": 22,
    "award of order": 22,
    "award letter": 22,
    "work order": 22,
    "acquisition": 25,
    "acquire": 22,
    "merger": 25,
    "takeover": 25,
    "commercial production": 22,
    "usfda approval": 25,
    "us fda approval": 25,
    "regulatory approval": 22,
    "drug approval": 22,
    "product approval": 20,
    "defence order": 25,
    "government order": 22,
    "export order": 20,
}


HIGH_IMPACT = {

    "order": 14,
    "contract": 14,
    "contract received": 18,
    "purchase order": 16,
    "project awarded": 18,
    "project order": 16,
    "capacity expansion": 14,
    "capacity addition": 14,
    "expansion": 10,
    "joint venture": 14,
    "jv": 10,
    "fund raising": 10,
    "fundraise": 10,
    "preferential issue": 8,
    "qip": 10,
    "strategic partnership": 12,
    "partnership": 8,
    "commissioned": 10,
    "commissioning": 10,
    "new plant": 12,
    "new facility": 12,
}


MEDIUM_POSITIVE = {

    "profit": 8,
    "profit growth": 10,
    "revenue growth": 10,
    "revenue": 5,
    "ebitda": 7,
    "ebitda growth": 9,
    "net profit": 8,
    "growth": 5,
    "sales growth": 7,
    "dividend": 5,
    "bonus": 4,
    "results": 4,
    "strong results": 8,
    "record revenue": 10,
    "record profit": 10,
    "capex": 6,
    "investment": 5,
    "approval": 7,
    "approved": 7,
    "production": 5,
    "new product": 7,
}


NEGATIVE_KEYWORDS = {

    "fraud": 25,
    "default": 20,
    "insolvency": 25,
    "bankruptcy": 25,
    "downgrade": 15,
    "credit downgrade": 20,
    "loss": 8,
    "net loss": 12,
    "penalty": 10,
    "fine": 8,
    "investigation": 15,
    "resignation": 7,
    "shutdown": 15,
    "closure": 12,
    "fire incident": 18,
    "fire": 10,
    "litigation": 10,
    "warning": 10,
    "delay": 8,
    "cancelled": 12,
    "cancellation": 12,
    "decline": 6,
    "pledge": 10,
    "pledged": 10,
    "promoter pledge": 15,
}


ROUTINE_KEYWORDS = {

    "trading window": 12,
    "secretarial audit": 10,
    "investor presentation": 7,
    "investor meet": 6,
    "analyst meet": 6,
    "agm": 8,
    "annual general meeting": 8,
    "compliance": 8,
    "credit rating": 6,
    "board meeting": 5,
    "appointment": 5,
}


# ============================================================
# NEWS SCORE
# ============================================================

def calculate_news_score(text):

    normalized = normalize_text(text)

    score = 0

    triggers = []

    positive_hits = 0

    negative_hits = 0

    routine_hits = 0

    # --------------------------------------------------------
    # VERY HIGH IMPACT
    # --------------------------------------------------------

    for keyword, points in VERY_HIGH_IMPACT.items():

        if keyword in normalized:

            score += points

            positive_hits += 1

            triggers.append(keyword)

    # --------------------------------------------------------
    # HIGH IMPACT
    # --------------------------------------------------------

    for keyword, points in HIGH_IMPACT.items():

        if keyword in normalized:

            score += points

            positive_hits += 1

            triggers.append(keyword)

    # --------------------------------------------------------
    # MEDIUM POSITIVE
    # --------------------------------------------------------

    for keyword, points in MEDIUM_POSITIVE.items():

        if keyword in normalized:

            score += points

            positive_hits += 1

            triggers.append(keyword)

    # --------------------------------------------------------
    # NEGATIVE
    # --------------------------------------------------------

    for keyword, points in NEGATIVE_KEYWORDS.items():

        if keyword in normalized:

            score -= points

            negative_hits += 1

    # --------------------------------------------------------
    # ROUTINE
    # --------------------------------------------------------

    for keyword, points in ROUTINE_KEYWORDS.items():

        if keyword in normalized:

            score -= points

            routine_hits += 1

    # --------------------------------------------------------
    # POSITIVE DIVERSITY BONUS
    # --------------------------------------------------------

    if positive_hits >= 2:
        score += 5

    if positive_hits >= 3:
        score += 5

    # --------------------------------------------------------
    # NEGATIVE PENALTY
    # --------------------------------------------------------

    if negative_hits >= 1:
        score -= 10

    if negative_hits >= 2:
        score -= 10

    # --------------------------------------------------------
    # ROUTINE ONLY NEWS
    # --------------------------------------------------------

    if (
        routine_hits > 0
        and positive_hits == 0
    ):
        score -= 15

    # --------------------------------------------------------
    # LIMIT
    # --------------------------------------------------------

    score = max(0, min(100, score))

    # Remove duplicate triggers
    triggers = list(dict.fromkeys(triggers))

    return score, triggers


# ============================================================
# PRICE DATA
# ============================================================

def get_yahoo_price(symbol):

    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + quote(symbol + ".NS")
    )

    params = {
        "range": "5d",
        "interval": "1d",
        "includePrePost": "false",
    }

    try:

        response = requests.get(
            url,
            params=params,
            timeout=REQUEST_TIMEOUT
        )

        if response.status_code != 200:
            return None

        data = response.json()

        result = (
            data
            .get("chart", {})
            .get("result", [])
        )

        if not result:
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

            closes = (
                result
                .get("indicators", {})
                .get("quote", [{}])[0]
                .get("close", [])
            )

            closes = [
                x for x in closes
                if x is not None
            ]

            if closes:
                price = closes[-1]

        if price is None:
            return None

        change_pct = None

        if previous_close:

            change_pct = (
                (price - previous_close)
                / previous_close
            ) * 100

        return {
            "price": float(price),
            "previous_close": (
                float(previous_close)
                if previous_close
                else None
            ),
            "change_pct": change_pct,
        }

    except Exception as e:

        print(
            f"Price error {symbol}:",
            e
        )

        return None


# ============================================================
# PRICE CONFIRMATION SCORE
# ============================================================

def price_confirmation(price_data):

    if not price_data:
        return 0

    change = price_data.get(
        "change_pct"
    )

    if change is None:
        return 0

    if change >= 3:
        return 15

    if change >= 2:
        return 12

    if change >= 1:
        return 8

    if change > 0:
        return 4

    if change <= -5:
        return -12

    if change <= -3:
        return -8

    if change < -1:
        return -4

    return 0


# ============================================================
# FINAL IMPACT SCORE
# ============================================================

def final_impact_score(
    news_score,
    price_data,
    news_dt
):

    score = news_score

    # Price confirmation
    score += price_confirmation(
        price_data
    )

    # Freshness bonus
    if news_dt:

        age_hours = (
            now_ist()
            - news_dt.astimezone(IST)
        ).total_seconds() / 3600

        if age_hours <= 3:
            score += 8

        elif age_hours <= 6:
            score += 6

        elif age_hours <= 12:
            score += 4

        elif age_hours <= 24:
            score += 2

    return max(
        0,
        min(100, int(score))
    )


# ============================================================
# NEWS SUMMARY
# ============================================================

def build_news_summary(item):

    fields = [
        "details",
        "Details",
        "desc",
        "description",
        "attchmntText",
        "attachmentText",
        "subject",
        "Subject",
    ]

    text = ""

    for field in fields:

        value = item.get(field)

        if value:

            value = re.sub(
                r"<[^>]+>",
                " ",
                str(value)
            )

            value = re.sub(
                r"\s+",
                " ",
                value
            ).strip()

            if len(value) > 20:

                text = value

                break

    if not text:
        return "NSE corporate announcement"

    # Keep Telegram compact
    if len(text) > 350:

        text = text[:347] + "..."

    return text


# ============================================================
# NSE ORIGINAL FILING LINK
# ============================================================

def get_attachment_link(item):

    possible_fields = [

        "attchmntFile",
        "attachmentFile",
        "attachment_file",

        "attchmntFilePath",
        "attachmentFilePath",

        "fileUrl",
        "file_url",

        "pdfUrl",
        "pdf_url",

        "attachmentUrl",
        "attachment_url",

    ]

    for field in possible_fields:

        value = item.get(field)

        if not value:
            continue

        url = str(value).strip()

        if not url:
            continue

        if url.startswith("http"):

            return url

        if url.startswith("/"):

            return NSE_HOME + url

    return None


def nse_verification_link(symbol):

    return (
        "https://www.nseindia.com/"
        "companies-listing/"
        "corporate-filings-announcements"
        "?symbol="
        + quote(symbol)
        + "&tabIndex=equity"
    )


# ============================================================
# CANDIDATE BUILDER
# ============================================================

def build_candidate(
    item,
    symbol
):

    text = announcement_text(item)

    news_dt = parse_news_datetime(
        item
    )

    news_score, triggers = (
        calculate_news_score(text)
    )

    if news_score <= 0:
        return None

    return {
        "symbol": symbol,
        "item": item,
        "text": text,
        "news_dt": news_dt,
        "news_score": news_score,
        "triggers": triggers,
        "price_data": None,
        "impact_score": news_score,
    }


# ============================================================
# RANK CANDIDATES
# ============================================================

def rank_candidates(candidates):

    print(
        "Candidates before price check:",
        len(candidates)
    )

    candidates = sorted(
        candidates,
        key=lambda x: x["news_score"],
        reverse=True
    )

    # Don't hit Yahoo for hundreds
    candidates = candidates[
        :MAX_PRICE_CHECKS
    ]

    for i, candidate in enumerate(
        candidates,
        start=1
    ):

        symbol = candidate["symbol"]

        print(
            f"Price check {i}/{len(candidates)}: "
            f"{symbol}"
        )

        price_data = get_yahoo_price(
            symbol
        )

        candidate["price_data"] = (
            price_data
        )

        candidate["impact_score"] = (
            final_impact_score(
                candidate["news_score"],
                price_data,
                candidate["news_dt"]
            )
        )

        time.sleep(0.15)

    candidates.sort(
        key=lambda x: (
            x["impact_score"],
            x["news_score"]
        ),
        reverse=True
    )

    return candidates


# ============================================================
# FALLBACK SELECTION
# ============================================================

def select_final_candidates(
    candidates
):

    if not candidates:
        return []

    # --------------------------------------------------------
    # Tier 1
    # --------------------------------------------------------

    tier1 = [
        x for x in candidates
        if x["impact_score"] >= 75
    ]

    if tier1:
        return tier1[
            :MAX_FINAL_STOCKS
        ]

    # --------------------------------------------------------
    # Tier 2
    # --------------------------------------------------------

    tier2 = [
        x for x in candidates
        if x["impact_score"] >= 60
    ]

    if tier2:
        return tier2[
            :MAX_FINAL_STOCKS
        ]

    # --------------------------------------------------------
    # Tier 3
    # --------------------------------------------------------

    tier3 = [
        x for x in candidates
        if x["impact_score"] >= 50
    ]

    if tier3:
        return tier3[
            :MAX_FINAL_STOCKS
        ]

    # --------------------------------------------------------
    # EMERGENCY FALLBACK
    # --------------------------------------------------------
    # If usable positive news exists,
    # don't return zero just because score is low.
    # But NEVER select negative/zero news.
    # --------------------------------------------------------

    usable = [
        x for x in candidates
        if x["impact_score"] > 0
    ]

    return usable[
        :MAX_FINAL_STOCKS
    ]


# ============================================================
# TELEGRAM MESSAGE
# ============================================================

def build_message(
    final_candidates
):

    now = now_ist()

    lines = []

    lines.append(
        "🚨 HIGH-IMPACT STOCK NEWS"
    )

    lines.append("")

    lines.append(
        f"🕒 Bot Time: "
        f"{format_ist(now)}"
    )

    lines.append(
        f"🔎 News Window: "
        f"Last {MAX_NEWS_AGE_HOURS} Hours"
    )

    lines.append("")

    price_label = get_price_label()

    for index, candidate in enumerate(
        final_candidates,
        start=1
    ):

        symbol = candidate[
            "symbol"
        ]

        score = candidate[
            "impact_score"
        ]

        news_dt = candidate[
            "news_dt"
        ]

        triggers = candidate[
            "triggers"
        ]

        price_data = candidate[
            "price_data"
        ]

        item = candidate[
            "item"
        ]

        summary = build_news_summary(
            item
        )

        # ----------------------------------------------------
        # Score label
        # ----------------------------------------------------

        if score >= 75:
            score_label = "🔥 VERY HIGH"

        elif score >= 60:
            score_label = "🟢 HIGH"

        elif score >= 50:
            score_label = "🟡 MEDIUM"

        else:
            score_label = "⚠️ LOWER"

        lines.append(
            f"{index}. {symbol}"
        )

        lines.append(
            f"🎯 Impact Score: "
            f"{score}/100 {score_label}"
        )

        if news_dt:

            lines.append(
                f"🕒 News Time: "
                f"{format_ist(news_dt)}"
            )

            lines.append(
                f"⏱ Age: "
                f"{news_age_text(news_dt)}"
            )

        lines.append(
            f"📰 {summary}"
        )

        if price_data:

            price = price_data.get(
                "price"
            )

            change = price_data.get(
                "change_pct"
            )

            if price is not None:

                if change is not None:

                    sign = (
                        "+"
                        if change >= 0
                        else ""
                    )

                    lines.append(
                        f"💰 {price_label}: "
                        f"₹{price:.2f} "
                        f"({sign}{change:.2f}%)"
                    )

                else:

                    lines.append(
                        f"💰 {price_label}: "
                        f"₹{price:.2f}"
                    )

        if triggers:

            trigger_text = ", ".join(
                triggers[:6]
            )

            lines.append(
                f"🔥 Trigger: "
                f"{trigger_text}"
            )

        filing_link = get_attachment_link(
            item
        )

        if not filing_link:

            filing_link = (
                nse_verification_link(
                    symbol
                )
            )

        lines.append(
            f"🔗 Verify Original NSE Filing:\n"
            f"{filing_link}"
        )

        lines.append("")

    lines.append(
        "📌 Ranking considers "
        "news impact, freshness and "
        "price reaction."
    )

    lines.append(
        "⚠️ Research/watchlist only. "
        "Not investment advice."
    )

    lines.append(
        "Made by Prakash Kanki"
    )

    return "\n".join(lines)


# ============================================================
# TELEGRAM SEND
# ============================================================

def send_telegram(message):

    if not TELEGRAM_BOT_TOKEN:

        print(
            "❌ TELEGRAM_BOT_TOKEN missing"
        )

        return False

    if not TELEGRAM_CHAT_ID:

        print(
            "❌ TELEGRAM_CHAT_ID missing"
        )

        return False

    # --------------------------------------------------------
    # Supports one or multiple chat IDs
    #
    # Example:
    # TELEGRAM_CHAT_ID=123456789
    #
    # Multiple:
    # TELEGRAM_CHAT_ID=123456789,987654321
    # --------------------------------------------------------

    chat_ids = [
        x.strip()
        for x in TELEGRAM_CHAT_ID.split(",")
        if x.strip()
    ]

    success = False

    url = (
        f"{TELEGRAM_API}/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    for chat_id in chat_ids:

        try:

            response = requests.post(
                url,
                data={
                    "chat_id": chat_id,
                    "text": message,
                    "disable_web_page_preview": False,
                },
                timeout=REQUEST_TIMEOUT
            )

            print(
                f"Telegram HTTP "
                f"{chat_id}:",
                response.status_code
            )

            if response.status_code == 200:

                success = True

            else:

                print(
                    response.text[:500]
                )

        except Exception as e:

            print(
                "Telegram error:",
                e
            )

    return success


# ============================================================
# TELEGRAM /START
# ============================================================

def send_start_welcome(chat_id):

    message = (
        "🚨 PREOPEN / STOCK NEWS ALERT BOT\n\n"
        "⚡ Fresh NSE Corporate News\n"
        "🎯 Smart Impact Ranking\n"
        "🕒 Exact IST News Time\n"
        "📊 Price Confirmation\n"
        "🔗 NSE Filing Verification\n\n"
        "Use this bot for research/watchlist alerts.\n\n"
        "Made by Prakash Kanki"
    )

    url = (
        f"{TELEGRAM_API}/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    try:

        requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": message,
            },
            timeout=REQUEST_TIMEOUT
        )

    except Exception as e:

        print(
            "Start message error:",
            e
        )


# ============================================================
# DATA UNAVAILABLE MESSAGE
# ============================================================

def send_data_unavailable():

    message = (
        "⚠️ NSE NEWS DATA UNAVAILABLE\n\n"
        f"🕒 {format_ist(now_ist())}\n\n"
        "NSE corporate announcement data "
        "could not be fetched successfully.\n\n"
        "❌ No stock has been invented or "
        "randomly selected.\n\n"
        "Please retry on the next scheduled run.\n\n"
        "Made by Prakash Kanki"
    )

    send_telegram(message)


# ============================================================
# MAIN NEWS PROCESSING
# ============================================================

def process_news():

    print("")
    print("=" * 60)
    print("STOCK NEWS BOT")
    print(format_ist(now_ist()))
    print("=" * 60)

    # --------------------------------------------------------
    # NSE data
    # --------------------------------------------------------

    records = get_nse_announcements()

    if records is None:

        send_data_unavailable()

        return

    if not records:

        print(
            "⚠️ NSE returned zero records"
        )

        send_data_unavailable()

        return

    # --------------------------------------------------------
    # Equity lookup
    # --------------------------------------------------------

    stocks, lookup = load_equity_list()

    if not stocks:

        print(
            "❌ NSE equity lookup unavailable"
        )

        send_data_unavailable()

        return

    # --------------------------------------------------------
    # Fresh news
    # --------------------------------------------------------

    fresh = []

    for item in records:

        news_dt = parse_news_datetime(
            item
        )

        if not news_dt:
            continue

        if is_fresh_news(
            news_dt
        ):

            fresh.append(item)

    print(
        "Fresh announcements:",
        len(fresh)
    )

    # --------------------------------------------------------
    # Identify + score
    # --------------------------------------------------------

    candidates = []

    seen = set()

    for item in fresh:

        text = announcement_text(
            item
        )

        if not text:
            continue

        symbol = identify_stock_fast(
            item,
            stocks,
            lookup
        )

        # IMPORTANT:
        # Never use random/default symbol
        if not symbol:

            print(
                "⚠️ Skipped unidentified news"
            )

            continue

        candidate = build_candidate(
            item,
            symbol
        )

        if not candidate:
            continue

        # ----------------------------------------------------
        # Prevent duplicate same-stock news
        # ----------------------------------------------------

        key = (
            symbol,
            normalize_text(text)[:200]
        )

        if key in seen:
            continue

        seen.add(key)

        candidates.append(
            candidate
        )

    print(
        "Positive candidates:",
        len(candidates)
    )

    if not candidates:

        message = (
            "ℹ️ NO USABLE POSITIVE STOCK NEWS\n\n"
            f"🕒 {format_ist(now_ist())}\n"
            f"🔎 Window: Last "
            f"{MAX_NEWS_AGE_HOURS} Hours\n\n"
            "NSE data was available, but no "
            "confident positive stock-news "
            "candidate passed the safety filter.\n\n"
            "No random stock has been added.\n\n"
            "Made by Prakash Kanki"
        )

        send_telegram(message)

        return

    # --------------------------------------------------------
    # Rank
    # --------------------------------------------------------

    ranked = rank_candidates(
        candidates
    )

    # --------------------------------------------------------
    # Final fallback selection
    # --------------------------------------------------------

    final_candidates = (
        select_final_candidates(
            ranked
        )
    )

    print("")
    print("FINAL RESULT")

    for candidate in final_candidates:

        print(
            candidate["symbol"],
            "| Impact:",
            candidate["impact_score"],
            "| News:",
            candidate["news_score"]
        )

    print(
        "Final stocks:",
        len(final_candidates)
    )

    # --------------------------------------------------------
    # Telegram
    # --------------------------------------------------------

    if final_candidates:

        message = build_message(
            final_candidates
        )

        send_telegram(message)

    else:

        message = (
            "ℹ️ NO USABLE STOCK NEWS\n\n"
            f"🕒 {format_ist(now_ist())}\n\n"
            "No reliable positive candidate "
            "was identified.\n\n"
            "No random stock has been added.\n\n"
            "Made by Prakash Kanki"
        )

        send_telegram(message)


# ============================================================
# PROGRAM START
# ============================================================

if __name__ == "__main__":

    print(
        "Stock News Bot started:",
        format_ist(now_ist())
    )

    try:

        process_news()

    except Exception as e:

        print(
            "❌ MAIN ERROR:",
            repr(e)
        )

        try:

            send_telegram(
                "🚨 STOCK NEWS BOT ERROR\n\n"
                f"Error: {str(e)[:500]}\n\n"
                "Made by Prakash Kanki"
            )

        except Exception:
            pass
