import os
import re
import csv
import time
import requests

from io import StringIO
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

MAX_NEWS_AGE_HOURS = 48

MAX_FINAL_STOCKS = 7

MAX_PRICE_CHECKS = 40

REQUEST_TIMEOUT = 15


# ============================================================
# TELEGRAM VARIABLES
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN"
)

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID"
)


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0 Safari/537.36"
    ),

    "Accept": (
        "application/json,text/plain,*/*"
    ),

    "Accept-Language":
        "en-US,en;q=0.9",

    "Referer":
        "https://www.nseindia.com/",

    "Connection":
        "keep-alive",
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

    diff = (
        now_ist()
        - dt.astimezone(IST)
    )

    if diff.total_seconds() < 0:
        return "0m"

    minutes = int(
        diff.total_seconds() / 60
    )

    days = minutes // 1440

    hours = (
        minutes % 1440
    ) // 60

    mins = minutes % 60

    if days > 0:
        return f"{days}d {hours}h"

    return f"{hours}h {mins}m"


# ============================================================
# PRICE LABEL
# ============================================================

def get_price_label():

    now = now_ist()

    # Saturday / Sunday
    if now.weekday() >= 5:
        return "Last Traded Close"

    # Before market
    if now.hour < 9 or (
        now.hour == 9
        and now.minute < 15
    ):
        return "Previous Close"

    # After market
    if now.hour > 15 or (
        now.hour == 15
        and now.minute >= 30
    ):
        return "Last Traded Close"

    return "Recent Price"


# ============================================================
# NORMALIZE TEXT
# ============================================================

def normalize_text(text):

    if text is None:
        return ""

    text = str(text).lower()

    text = text.replace(
        "&amp;",
        " and "
    )

    text = re.sub(
        r"<[^>]+>",
        " ",
        text
    )

    text = re.sub(
        r"[^a-z0-9₹%.,+\-]+",
        " ",
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


# ============================================================
# GET ALL ANNOUNCEMENT TEXT
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

            parts.append(
                str(value)
            )

    return " ".join(parts)


# ============================================================
# DATE PARSER
# ============================================================

def parse_news_datetime(item):

    fields = [

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

        "disseminationTime",
        "dissemination_time",
    ]

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

    for field in fields:

        value = item.get(field)

        if not value:
            continue

        value = str(value).strip()

        # Unix timestamp
        if value.isdigit():

            try:

                number = int(value)

                if number > 10_000_000_000:
                    number /= 1000

                return datetime.fromtimestamp(
                    number,
                    tz=IST
                )

            except Exception:
                pass

        for fmt in formats:

            try:

                dt = datetime.strptime(
                    value,
                    fmt
                )

                return dt.replace(
                    tzinfo=IST
                )

            except Exception:
                continue

    return None


# ============================================================
# FRESH NEWS
# ============================================================

def is_fresh_news(dt):

    if not dt:
        return False

    age = (
        now_ist()
        - dt.astimezone(IST)
    )

    return (
        age.total_seconds() >= 0
        and age <= timedelta(
            hours=MAX_NEWS_AGE_HOURS
        )
    )


# ============================================================
# NSE EQUITY LIST
# ============================================================

def load_equity_list():

    print(
        "Downloading NSE equity list..."
    )

    try:

        response = session.get(
            NSE_EQUITY_LIST,
            timeout=REQUEST_TIMEOUT
        )

        response.raise_for_status()

        reader = csv.reader(
            StringIO(response.text)
        )

        rows = list(reader)

        if not rows:
            return set(), {}

        header = rows[0]

        indexes = {
            x.strip().upper(): i
            for i, x in enumerate(header)
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

        if symbol_index is None:

            print(
                "❌ SYMBOL column not found"
            )

            return set(), {}

        valid_symbols = set()

        company_names = {}

        for row in rows[1:]:

            try:

                if (
                    symbol_index
                    >= len(row)
                ):
                    continue

                symbol = (
                    row[symbol_index]
                    .strip()
                    .upper()
                )

                if not symbol:
                    continue

                # Only EQ
                if series_index is not None:

                    if (
                        series_index
                        < len(row)
                    ):

                        series = (
                            row[series_index]
                            .strip()
                            .upper()
                        )

                        if (
                            series
                            and series != "EQ"
                        ):
                            continue

                valid_symbols.add(
                    symbol
                )

                if (
                    name_index is not None
                    and name_index < len(row)
                ):

                    name = (
                        row[name_index]
                        .strip()
                    )

                    normalized_name = (
                        normalize_text(name)
                    )

                    if normalized_name:

                        company_names[
                            normalized_name
                        ] = symbol

            except Exception:
                continue

        print(
            "Valid EQ stocks:",
            len(valid_symbols)
        )

        print(
            "Company mappings:",
            len(company_names)
        )

        return (
            valid_symbols,
            company_names
        )

    except Exception as e:

        print(
            "❌ Equity list error:",
            repr(e)
        )

        return set(), {}


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
                response.text[:500]
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

        return records

    except Exception as e:

        print(
            "❌ NSE API error:",
            repr(e)
        )

        return None


# ============================================================
# DIRECT NSE SYMBOL
# ============================================================

def get_direct_nse_symbol(
    item,
    valid_symbols
):

    """
    NSE symbol is the strongest identification.

    NEVER guess from random company words.
    """

    fields = [

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

        "symbolCode",
        "symbol_code",
    ]

    for field in fields:

        value = item.get(field)

        if not value:
            continue

        symbol = (
            str(value)
            .strip()
            .upper()
        )

        symbol = re.sub(
            r"[^A-Z0-9&\-]",
            "",
            symbol
        )

        if symbol in valid_symbols:

            return symbol

    return None


# ============================================================
# SAFE STOCK IDENTIFICATION
# ============================================================

def identify_stock(
    item,
    valid_symbols,
    company_names
):

    # --------------------------------------------------------
    # 1. DIRECT NSE SYMBOL
    # --------------------------------------------------------

    direct = get_direct_nse_symbol(
        item,
        valid_symbols
    )

    if direct:

        return direct, "NSE_SYMBOL"

    # --------------------------------------------------------
    # 2. COMPANY NAME FIELD
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

    text = normalize_text(
        announcement_text(item)
    )

    for field in company_fields:

        value = item.get(field)

        if not value:
            continue

        company = normalize_text(
            value
        )

        if len(company) < 8:
            continue

        symbol = company_names.get(
            company
        )

        if symbol in valid_symbols:

            return (
                symbol,
                "COMPANY_FIELD"
            )

    # --------------------------------------------------------
    # 3. EXACT FULL COMPANY NAME
    # --------------------------------------------------------

    for company, symbol in (
        company_names.items()
    ):

        if len(company) < 10:
            continue

        if company in text:

            return (
                symbol,
                "FULL_COMPANY_NAME"
            )

    # --------------------------------------------------------
    # IMPORTANT
    # --------------------------------------------------------
    # NO SINGLE WORD MATCHING
    # --------------------------------------------------------

    return None, "UNIDENTIFIED"


# ============================================================
# NEWS CLASSIFICATION
# ============================================================

VERY_HIGH_IMPACT = {

    "large order": 20,
    "major order": 20,
    "mega order": 22,

    "order win": 18,
    "order received": 18,

    "award of order": 18,
    "award letter": 18,

    "work order": 18,

    "acquisition": 22,
    "acquire": 20,

    "merger": 22,
    "takeover": 22,

    "usfda approval": 22,
    "us fda approval": 22,

    "regulatory approval": 18,
    "drug approval": 18,

    "commercial production": 18,

    "defence order": 22,
    "government order": 18,

    "export order": 17,
}


HIGH_IMPACT = {

    "order": 10,
    "contract": 10,

    "purchase order": 14,
    "contract received": 14,

    "project awarded": 15,
    "project order": 13,

    "capacity expansion": 12,
    "capacity addition": 12,

    "new plant": 12,
    "new facility": 12,

    "joint venture": 12,
    "strategic partnership": 10,

    "fund raising": 8,
    "fundraise": 8,

    "qip": 8,
    "preferential issue": 8,

    "commissioned": 10,
    "commissioning": 10,
}


POSITIVE_EVENTS = {

    "revenue": 5,
    "sales": 4,

    "profit": 7,
    "net profit": 8,

    "ebitda": 6,

    "growth": 5,

    "record revenue": 12,
    "record profit": 12,

    "strong results": 10,

    "approval": 6,
    "approved": 6,

    "capex": 5,

    "new product": 6,

    "dividend": 4,

    "bonus": 3,

    "expansion": 5,
}


NEGATIVE_KEYWORDS = {

    "fraud": 25,
    "default": 20,

    "insolvency": 25,
    "bankruptcy": 25,

    "downgrade": 15,
    "credit downgrade": 20,

    "net loss": 15,
    "loss": 8,

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

    "board meeting": 4,

}


# ============================================================
# PERCENTAGE EXTRACTION
# ============================================================

def extract_percentages(text):

    pattern = r"(\d+(?:\.\d+)?)\s*%"

    values = []

    for match in re.findall(
        pattern,
        text
    ):

        try:

            value = float(match)

            if 0 < value <= 1000:
                values.append(value)

        except Exception:
            pass

    return values


# ============================================================
# MONEY VALUE EXTRACTION
# ============================================================

def extract_money_values(text):

    values = []

    pattern = (
        r"(?:₹|rs\.?|inr)\s*"
        r"([\d,]+(?:\.\d+)?)"
        r"\s*"
        r"(crore|cr|million|mn|lakh|lac)?"
    )

    matches = re.findall(
        pattern,
        text,
        flags=re.IGNORECASE
    )

    for number, unit in matches:

        try:

            value = float(
                number.replace(
                    ",",
                    ""
                )
            )

            unit = unit.lower()

            if unit in [
                "crore",
                "cr"
            ]:
                value *= 1

            elif unit in [
                "million",
                "mn"
            ]:
                value *= 0.1

            elif unit in [
                "lakh",
                "lac"
            ]:
                value *= 0.01

            values.append(
                value
            )

        except Exception:
            continue

    return values


# ============================================================
# NEWS IMPACT SCORE
# ============================================================

def calculate_news_score(text):

    normalized = normalize_text(
        text
    )

    score = 0

    triggers = []

    positive_hits = 0

    negative_hits = 0

    routine_hits = 0

    # --------------------------------------------------------
    # VERY HIGH IMPACT
    # --------------------------------------------------------

    for keyword, points in (
        VERY_HIGH_IMPACT.items()
    ):

        if keyword in normalized:

            score += points

            positive_hits += 1

            triggers.append(
                keyword
            )

    # --------------------------------------------------------
    # HIGH IMPACT
    # --------------------------------------------------------

    for keyword, points in (
        HIGH_IMPACT.items()
    ):

        if keyword in normalized:

            score += points

            positive_hits += 1

            triggers.append(
                keyword
            )

    # --------------------------------------------------------
    # POSITIVE EVENTS
    # --------------------------------------------------------

    for keyword, points in (
        POSITIVE_EVENTS.items()
    ):

        if keyword in normalized:

            score += points

            positive_hits += 1

            triggers.append(
                keyword
            )

    # --------------------------------------------------------
    # NEGATIVE
    # --------------------------------------------------------

    for keyword, points in (
        NEGATIVE_KEYWORDS.items()
    ):

        if keyword in normalized:

            score -= points

            negative_hits += 1

    # --------------------------------------------------------
    # ROUTINE
    # --------------------------------------------------------

    for keyword, points in (
        ROUTINE_KEYWORDS.items()
    ):

        if keyword in normalized:

            score -= points

            routine_hits += 1

    # --------------------------------------------------------
    # PERCENTAGE / GROWTH STRENGTH
    # --------------------------------------------------------

    percentages = (
        extract_percentages(
            text
        )
    )

    max_pct = (
        max(percentages)
        if percentages
        else 0
    )

    # Strong growth numbers
    if max_pct >= 50:
        score += 15
        triggers.append(
            f"{max_pct:.0f}% growth"
        )

    elif max_pct >= 30:
        score += 12
        triggers.append(
            f"{max_pct:.0f}% growth"
        )

    elif max_pct >= 20:
        score += 9
        triggers.append(
            f"{max_pct:.0f}% growth"
        )

    elif max_pct >= 10:
        score += 5

    # --------------------------------------------------------
    # MONEY / ORDER VALUE
    # --------------------------------------------------------

    money_values = (
        extract_money_values(
            text
        )
    )

    if money_values:

        max_money = max(
            money_values
        )

        # Order / contract value
        if (
            "order" in normalized
            or "contract" in normalized
        ):

            if max_money >= 500:
                score += 20

            elif max_money >= 200:
                score += 17

            elif max_money >= 100:
                score += 14

            elif max_money >= 50:
                score += 11

            elif max_money >= 20:
                score += 8

            elif max_money >= 10:
                score += 5

    # --------------------------------------------------------
    # MULTIPLE POSITIVE SIGNALS
    # --------------------------------------------------------

    if positive_hits >= 2:
        score += 5

    if positive_hits >= 4:
        score += 5

    # --------------------------------------------------------
    # NEGATIVE PENALTY
    # --------------------------------------------------------

    if negative_hits >= 1:
        score -= 12

    if negative_hits >= 2:
        score -= 12

    # --------------------------------------------------------
    # ROUTINE-ONLY FILTER
    # --------------------------------------------------------

    if (
        routine_hits > 0
        and positive_hits == 0
    ):

        score -= 20

    # --------------------------------------------------------
    # PURE GENERIC REVENUE UPDATE
    # --------------------------------------------------------
    # Don't automatically reject it.
    # Financial percentages can rescue it.
    # --------------------------------------------------------

    # --------------------------------------------------------
    # LIMIT
    # --------------------------------------------------------

    score = max(
        0,
        min(100, score)
    )

    triggers = list(
        dict.fromkeys(
            triggers
        )
    )

    return (
        score,
        triggers
    )


# ============================================================
# PRICE
# ============================================================

def get_yahoo_price(symbol):

    url = (
        "https://query1.finance.yahoo.com/"
        "v8/finance/chart/"
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
                .get(
                    "indicators",
                    {}
                )
                .get(
                    "quote",
                    [{}]
                )[0]
                .get(
                    "close",
                    []
                )
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
                (
                    price
                    - previous_close
                )
                / previous_close
            ) * 100

        return {
            "price": float(price),

            "previous_close": (
                float(
                    previous_close
                )
                if previous_close
                else None
            ),

            "change_pct": change_pct,
        }

    except Exception as e:

        print(
            f"Price error {symbol}:",
            repr(e)
        )

        return None


# ============================================================
# PRICE CONFIRMATION
# ============================================================

def price_score(price_data):

    if not price_data:
        return 0

    change = price_data.get(
        "change_pct"
    )

    if change is None:
        return 0

    if change >= 5:
        return 12

    if change >= 3:
        return 10

    if change >= 2:
        return 8

    if change >= 1:
        return 5

    if change > 0:
        return 2

    # Negative price DOES NOT remove news
    if change <= -5:
        return -8

    if change <= -3:
        return -6

    if change < -1:
        return -3

    return 0


# ============================================================
# FINAL SCORE
# ============================================================

def final_score(
    news_score,
    price_data,
    news_dt
):

    score = news_score

    # --------------------------------------------------------
    # Price confirmation
    # --------------------------------------------------------

    score += price_score(
        price_data
    )

    # --------------------------------------------------------
    # Freshness
    # --------------------------------------------------------

    if news_dt:

        age_hours = (
            now_ist()
            - news_dt.astimezone(IST)
        ).total_seconds() / 3600

        if age_hours <= 2:
            score += 10

        elif age_hours <= 6:
            score += 8

        elif age_hours <= 12:
            score += 6

        elif age_hours <= 24:
            score += 3

        elif age_hours <= 36:
            score += 1

    return max(
        0,
        min(100, int(score))
    )


# ============================================================
# NEWS SUMMARY
# ============================================================

def build_summary(item):

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

        value = item.get(
            field
        )

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

        return (
            "NSE corporate announcement"
        )

    if len(text) > 400:

        text = (
            text[:397]
            + "..."
        )

    return text


# ============================================================
# NSE ATTACHMENT
# ============================================================

def get_attachment_link(item):

    fields = [

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

    for field in fields:

        value = item.get(
            field
        )

        if not value:
            continue

        url = str(
            value
        ).strip()

        if url.startswith(
            "http"
        ):

            return url

        if url.startswith(
            "/"
        ):

            return (
                NSE_HOME
                + url
            )

    return None


def nse_verification_link(
    symbol
):

    return (
        "https://www.nseindia.com/"
        "companies-listing/"
        "corporate-filings-announcements"
        "?symbol="
        + quote(symbol)
        + "&tabIndex=equity"
    )


# ============================================================
# BUILD CANDIDATE
# ============================================================

def build_candidate(
    item,
    symbol
):

    text = announcement_text(
        item
    )

    if not text:
        return None

    news_dt = parse_news_datetime(
        item
    )

    if not news_dt:
        return None

    news_score, triggers = (
        calculate_news_score(
            text
        )
    )

    # Only reject genuinely non-positive
    if news_score <= 0:
        return None

    return {

        "symbol": symbol,

        "item": item,

        "text": text,

        "news_dt": news_dt,

        "news_score":
            news_score,

        "triggers":
            triggers,

        "price_data":
            None,

        "impact_score":
            news_score,
    }


# ============================================================
# RANK
# ============================================================

def rank_candidates(
    candidates
):

    # First sort by news strength
    candidates.sort(
        key=lambda x:
            x["news_score"],
        reverse=True
    )

    # Price check top 40
    price_candidates = (
        candidates[
            :MAX_PRICE_CHECKS
        ]
    )

    print(
        "Price checks:",
        len(price_candidates)
    )

    for i, candidate in enumerate(
        price_candidates,
        start=1
    ):

        symbol = candidate[
            "symbol"
        ]

        print(
            f"Price check "
            f"{i}/"
            f"{len(price_candidates)}: "
            f"{symbol}"
        )

        price_data = (
            get_yahoo_price(
                symbol
            )
        )

        candidate[
            "price_data"
        ] = price_data

        candidate[
            "impact_score"
        ] = final_score(
            candidate[
                "news_score"
            ],
            price_data,
            candidate[
                "news_dt"
            ]
        )

        time.sleep(
            0.15
        )

    # --------------------------------------------------------
    # VERY IMPORTANT:
    # Do NOT apply 75/60/50 threshold now.
    # Rank ALL usable positive candidates.
    # --------------------------------------------------------

    candidates.sort(
        key=lambda x: (
            x["impact_score"],
            x["news_score"]
        ),
        reverse=True
    )

    return candidates


# ============================================================
# TOP 7
# ============================================================

def select_top_candidates(
    candidates
):

    # ALL genuine positive candidates
    # are already ranked.

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
    candidates
):

    lines = []

    lines.append(
        "🚨 HIGH-IMPACT STOCK NEWS"
    )

    lines.append("")

    lines.append(
        f"🕒 Bot Time: "
        f"{format_ist(now_ist())}"
    )

    lines.append(
        f"🔎 News Window: "
        f"Last {MAX_NEWS_AGE_HOURS} Hours"
    )

    lines.append(
        f"📊 Showing Top "
        f"{len(candidates)} Genuine Positive News"
    )

    lines.append("")

    price_label = (
        get_price_label()
    )

    for index, candidate in enumerate(
        candidates,
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

        price_data = candidate[
            "price_data"
        ]

        triggers = candidate[
            "triggers"
        ]

        item = candidate[
            "item"
        ]

        summary = build_summary(
            item
        )

        # Score level
        if score >= 80:

            level = "🔥 VERY HIGH"

        elif score >= 65:

            level = "🟢 HIGH"

        elif score >= 50:

            level = "🟡 MEDIUM"

        elif score >= 35:

            level = "🟠 MODERATE"

        else:

            level = "⚠️ LOWER"

        lines.append(
            f"{index}. {symbol}"
        )

        lines.append(
            f"🎯 Impact Score: "
            f"{score}/100 {level}"
        )

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

        # ----------------------------------------------------
        # PRICE
        # ----------------------------------------------------

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

                    if change >= 2:

                        price_note = (
                            "🟢 Positive"
                        )

                    elif change <= -2:

                        price_note = (
                            "🔴 Negative"
                        )

                    else:

                        price_note = (
                            "⚪ Neutral"
                        )

                    lines.append(
                        f"💰 {price_label}: "
                        f"₹{price:.2f} "
                        f"({sign}{change:.2f}%) "
                        f"{price_note}"
                    )

                else:

                    lines.append(
                        f"💰 {price_label}: "
                        f"₹{price:.2f}"
                    )

        # ----------------------------------------------------
        # TRIGGERS
        # ----------------------------------------------------

        if triggers:

            trigger_text = ", ".join(
                triggers[:8]
            )

            lines.append(
                f"🔥 Trigger: "
                f"{trigger_text}"
            )

        # ----------------------------------------------------
        # NSE FILING
        # ----------------------------------------------------

        filing = (
            get_attachment_link(
                item
            )
        )

        if not filing:

            filing = (
                nse_verification_link(
                    symbol
                )
            )

        lines.append(
            "🔗 Verify Original NSE Filing:"
        )

        lines.append(
            filing
        )

        lines.append("")

    lines.append(
        "📌 Ranking = news impact + "
        "financial strength + freshness + "
        "price confirmation."
    )

    lines.append(
        "⚠️ Research/watchlist only. "
        "Not investment advice."
    )

    lines.append(
        "Made by Prakash Kanki"
    )

    return "\n".join(
        lines
    )


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(
    message
):

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

    chat_ids = [

        x.strip()

        for x in
        TELEGRAM_CHAT_ID.split(",")

        if x.strip()
    ]

    url = (
        f"{TELEGRAM_API}/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    success = False

    for chat_id in chat_ids:

        try:

            response = requests.post(

                url,

                data={

                    "chat_id":
                        chat_id,

                    "text":
                        message,

                    "disable_web_page_preview":
                        False,
                },

                timeout=
                    REQUEST_TIMEOUT
            )

            print(
                f"Telegram HTTP "
                f"{chat_id}: "
                f"{response.status_code}"
            )

            if (
                response.status_code
                == 200
            ):

                success = True

            else:

                print(
                    response.text[
                        :500
                    ]
                )

        except Exception as e:

            print(
                "Telegram error:",
                repr(e)
            )

    return success


# ============================================================
# DATA UNAVAILABLE
# ============================================================

def send_data_unavailable():

    message = (

        "⚠️ NSE NEWS DATA UNAVAILABLE\n\n"

        f"🕒 {format_ist(now_ist())}\n\n"

        "NSE corporate announcement data "
        "could not be fetched successfully.\n\n"

        "❌ No stock has been invented "
        "or randomly selected.\n\n"

        "Made by Prakash Kanki"
    )

    send_telegram(
        message
    )


# ============================================================
# MAIN
# ============================================================

def process_news():

    print("")
    print("=" * 70)

    print(
        "STOCK NEWS BOT"
    )

    print(
        format_ist(
            now_ist()
        )
    )

    print("=" * 70)

    # --------------------------------------------------------
    # NSE ANNOUNCEMENTS
    # --------------------------------------------------------

    records = (
        get_nse_announcements()
    )

    if records is None:

        send_data_unavailable()

        return

    if not records:

        print(
            "❌ NSE returned zero records"
        )

        send_data_unavailable()

        return

    # --------------------------------------------------------
    # EQUITY LIST
    # --------------------------------------------------------

    valid_symbols, company_names = (
        load_equity_list()
    )

    if not valid_symbols:

        send_data_unavailable()

        return

    # --------------------------------------------------------
    # FRESH NEWS
    # --------------------------------------------------------

    fresh = []

    for item in records:

        dt = parse_news_datetime(
            item
        )

        if not dt:
            continue

        if is_fresh_news(dt):

            fresh.append(
                item
            )

    print(
        "Fresh announcements:",
        len(fresh)
    )

    # --------------------------------------------------------
    # IDENTIFICATION + POSITIVE FILTER
    # --------------------------------------------------------

    candidates = []

    seen_news = set()

    identified = 0

    rejected = 0

    for item in fresh:

        text = announcement_text(
            item
        )

        if not text:
            continue

        symbol, method = (
            identify_stock(
                item,
                valid_symbols,
                company_names
            )
        )

        if not symbol:

            rejected += 1

            print(
                "⚠️ Unidentified:",
                text[:150]
            )

            continue

        identified += 1

        print(
            f"✓ {symbol} "
            f"[{method}]"
        )

        candidate = build_candidate(
            item,
            symbol
        )

        if not candidate:

            continue

        # ----------------------------------------------------
        # Deduplicate
        # ----------------------------------------------------

        news_dt = candidate[
            "news_dt"
        ]

        subject = normalize_text(
            item.get(
                "subject",
                ""
            )
        )

        unique_key = (
            symbol,
            subject,
            news_dt.strftime(
                "%Y%m%d%H%M"
            )
        )

        if unique_key in seen_news:

            continue

        seen_news.add(
            unique_key
        )

        candidates.append(
            candidate
        )

    print("")
    print(
        "Identified:",
        identified
    )

    print(
        "Unidentified:",
        rejected
    )

    print(
        "Positive candidates:",
        len(candidates)
    )

    # --------------------------------------------------------
    # NO POSITIVE NEWS
    # --------------------------------------------------------

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

        send_telegram(
            message
        )

        return

    # --------------------------------------------------------
    # RANK EVERYTHING
    # --------------------------------------------------------

    ranked = rank_candidates(
        candidates
    )

    # --------------------------------------------------------
    # TOP 7
    # --------------------------------------------------------

    final = select_top_candidates(
        ranked
    )

    print("")
    print(
        "=" * 70
    )

    print(
        "FINAL TOP STOCKS"
    )

    print(
        "=" * 70
    )

    for i, candidate in enumerate(
        final,
        start=1
    ):

        print(
            i,
            candidate["symbol"],
            "| News:",
            candidate["news_score"],
            "| Impact:",
            candidate["impact_score"]
        )

    print(
        "=" * 70
    )

    # --------------------------------------------------------
    # SEND
    # --------------------------------------------------------

    if final:

        message = build_message(
            final
        )

        send_telegram(
            message
        )

    else:

        send_data_unavailable()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    print(
        "Stock News Bot started:",
        format_ist(
            now_ist()
        )
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

                f"{str(e)[:500]}\n\n"

                "Made by Prakash Kanki"
            )

        except Exception:
            pass
