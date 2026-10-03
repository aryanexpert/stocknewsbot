import os
import re
import time
import html
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

import requests


# ============================================================
# SETTINGS
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

NSE_HOME = "https://www.nseindia.com"
NSE_ANNOUNCEMENTS = "https://www.nseindia.com/api/corporate-announcements"
NSE_EQUITY_LIST = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}.NS"

TELEGRAM_URL = "https://api.telegram.org/bot{}/sendMessage"

MAX_FINAL_STOCKS = 7
MAX_PRICE_CHECKS = 30

# News can be up to 48 hours old
MAX_NEWS_AGE_HOURS = 48

REQUEST_TIMEOUT = 12

# Show rejected news in terminal
SHOW_FILTER_DIAGNOSTICS = True


# ============================================================
# HEADERS
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
    "Origin": "https://www.nseindia.com",
    "Connection": "keep-alive",
}


# ============================================================
# NEWS IMPACT KEYWORDS
# ============================================================

IMPACT_POINTS = {
    "order win": 25,
    "large order": 25,
    "order": 18,
    "contract": 18,
    "work order": 20,

    "acquisition": 18,
    "merger": 18,
    "stake acquisition": 18,
    "stake": 10,

    "fund raise": 14,
    "fundraising": 14,
    "fund raising": 14,
    "qip": 15,
    "preferential": 13,

    "regulatory approval": 18,
    "approval": 13,
    "approved": 13,

    "commercial production": 18,
    "production": 8,

    "capacity expansion": 16,
    "capacity": 10,
    "expansion": 9,
    "plant": 7,
    "new facility": 12,

    "results": 10,
    "profit": 10,
    "profit growth": 15,
    "revenue": 8,
    "revenue growth": 12,
    "ebitda": 9,
    "guidance": 10,

    "dividend": 7,
    "buyback": 12,
    "upgrade": 8,

    "wins": 18,
    "won": 18,

    "partnership": 10,
    "joint venture": 12,
    "investment": 9,
    "capex": 10,
    "new project": 12,
}


# ============================================================
# POSITIVE / NEGATIVE WORDS
# ============================================================

POSITIVE_WORDS = {
    "order",
    "order win",
    "large order",
    "contract",
    "work order",
    "approval",
    "approved",
    "commission",
    "capacity",
    "capacity expansion",
    "expansion",
    "profit",
    "profit growth",
    "revenue growth",
    "ebitda",
    "fund raise",
    "fundraising",
    "fund raising",
    "qip",
    "acquisition",
    "buyback",
    "dividend",
    "upgrade",
    "wins",
    "won",
    "partnership",
    "joint venture",
    "investment",
    "capex",
    "new project",
    "commercial production",
}


NEGATIVE_WORDS = {
    "fraud",
    "default",
    "downgrade",
    "rating downgrade",
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
    "notice",
    "delay",
    "debt restructuring",
}


IRRELEVANT_WORDS = {
    "market outlook",
    "sensex",
    "nifty",
    "technical",
    "should you buy",
    "stock market today",
    "top stocks",
    "stocks to watch",
    "brokerage target",
    "share market today",
    "intraday picks",
    "multibagger",
}


# ============================================================
# GENERIC COMPANY WORDS
# ============================================================

GENERIC_WORDS = {
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
    "PUBLIC",
    "GROUP",
}


# ============================================================
# GLOBAL CACHE
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

    value = re.sub(
        r"[^A-Z0-9& ]+",
        " ",
        value
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value.strip()


def now_ist():

    return datetime.now(IST)


# ============================================================
# NSE EQUITY LIST
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

            symbol = (
                parts[symbol_index]
                .strip()
                .upper()
            )

            series = ""

            if (
                series_index is not None
                and len(parts) > series_index
            ):
                series = (
                    parts[series_index]
                    .strip()
                    .upper()
                )

            if series and series != "EQ":
                continue

            company_name = ""

            if (
                name_index is not None
                and len(parts) > name_index
            ):
                company_name = (
                    parts[name_index]
                    .strip()
                )

            if symbol:

                stocks.append({
                    "symbol": symbol,
                    "name": company_name,
                    "name_normalized": normalize_text(
                        company_name
                    ),
                })

        print(
            f"NSE symbols: {len(stocks)}"
        )

        return stocks

    except Exception as e:

        print(
            "NSE equity list error:",
            str(e)
        )

        return []


# ============================================================
# BUILD OPTIMIZED COMPANY LOOKUP
# ============================================================

def build_company_lookup(stocks):

    print("Building optimized company lookup...")

    symbol_lookup = {}
    exact_name_lookup = {}
    token_lookup = {}

    for stock in stocks:

        symbol = stock["symbol"]
        name = stock["name_normalized"]

        # Direct symbol lookup
        if symbol:
            symbol_lookup[symbol] = symbol

        # Exact company name lookup
        if name:
            exact_name_lookup[name] = symbol

        # Meaningful company tokens
        words = name.split()

        for word in words:

            if len(word) < 4:
                continue

            if word in GENERIC_WORDS:
                continue

            token_lookup.setdefault(
                word,
                set()
            ).add(symbol)

    print(
        f"Symbol lookup: {len(symbol_lookup)}"
    )

    print(
        f"Company lookup: {len(exact_name_lookup)}"
    )

    print(
        f"Company tokens: {len(token_lookup)}"
    )

    return {
        "symbol": symbol_lookup,
        "name": exact_name_lookup,
        "token": token_lookup,
    }


# ============================================================
# NSE ANNOUNCEMENTS
# ============================================================

def get_nse_announcements():

    print("\n----- NSE ANNOUNCEMENTS -----")
    print("Getting NSE corporate announcements...")

    for attempt in range(1, 4):

        print(
            f"NSE announcement attempt "
            f"{attempt}/3..."
        )

        session = requests.Session()

        session.headers.update(
            HEADERS
        )

        try:

            try:

                home = session.get(
                    NSE_HOME,
                    timeout=12
                )

                print(
                    "NSE homepage response:",
                    home.status_code
                )

            except Exception as e:

                print(
                    "NSE homepage warning:",
                    type(e).__name__
                )

            time.sleep(1)

            response = session.get(
                NSE_ANNOUNCEMENTS,
                params={
                    "index": "equities"
                },
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

                time.sleep(2)

                continue

            raw = response.text.strip()

            if not raw:

                print(
                    "NSE returned EMPTY response."
                )

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

                print(
                    raw[:300]
                )

                time.sleep(2)

                continue

            if isinstance(data, dict):

                records = data.get(
                    "data",
                    []
                )

            elif isinstance(data, list):

                records = data

            else:

                records = []

            if not isinstance(
                records,
                list
            ):

                print(
                    "Unexpected NSE data format."
                )

                time.sleep(2)

                continue

            print(
                "NSE announcements received:",
                len(records)
            )

            if not records:

                time.sleep(2)

                continue

            return records

        except requests.exceptions.Timeout:

            print(
                "NSE request timed out."
            )

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

    print(
        "Could not get NSE announcements."
    )

    return []


# ============================================================
# DATE PARSER
# ============================================================

def parse_news_datetime(item):

    fields = [
        "an_dt",
        "broadcastDate",
        "sort_date",
        "date",
        "time",
        "timestamp",
    ]

    for field in fields:

        value = item.get(field)

        if not value:
            continue

        value = str(value).strip()

        try:

            dt = datetime.fromisoformat(
                value.replace(
                    "Z",
                    "+00:00"
                )
            )

            if dt.tzinfo is None:
                dt = dt.replace(
                    tzinfo=IST
                )

            return dt.astimezone(IST)

        except Exception:
            pass

        try:

            dt = parsedate_to_datetime(
                value
            )

            if dt.tzinfo is None:
                dt = dt.replace(
                    tzinfo=IST
                )

            return dt.astimezone(IST)

        except Exception:
            pass

    return None


# ============================================================
# FRESH NEWS
# ============================================================

def is_fresh_news(item):

    dt = parse_news_datetime(item)

    if dt is None:
        return True

    age = now_ist() - dt

    if age.total_seconds() < 0:
        return True

    return (
        age <= timedelta(
            hours=MAX_NEWS_AGE_HOURS
        )
    )


# ============================================================
# GET FULL ANNOUNCEMENT TEXT
# ============================================================

def announcement_text(item):

    fields = [
        "desc",
        "subject",
        "details",
        "attchmntText",
        "description",
        "headline",
    ]

    values = []

    for field in fields:

        value = item.get(field)

        if value:
            values.append(
                clean_text(value)
            )

    # Remove repeated text
    result = " ".join(values)

    return result.strip()


# ============================================================
# GET TITLE
# ============================================================

def get_news_title(item):

    title = (
        item.get("subject")
        or item.get("headline")
        or item.get("desc")
        or item.get("description")
        or ""
    )

    title = clean_text(title)

    if len(title) > 250:

        title = title[:247] + "..."

    return title


# ============================================================
# RELEVANCE CHECK
# ============================================================

def check_relevance(text):

    text_lower = text.lower()

    irrelevant_count = 0

    for word in IRRELEVANT_WORDS:

        if word in text_lower:
            irrelevant_count += 1

    # If heavily dominated by market commentary
    if irrelevant_count >= 2:

        return False, "Market/technical commentary"

    positive_found = any(
        keyword in text_lower
        for keyword in IMPACT_POINTS
    )

    negative_found = any(
        keyword in text_lower
        for keyword in NEGATIVE_WORDS
    )

    if not positive_found and not negative_found:

        return False, "No stock-moving event keyword"

    return True, "Relevant"


# ============================================================
# SCORE NEWS
# ============================================================

def score_news(text):

    text_lower = text.lower()

    score = 0

    matched_positive = []
    matched_negative = []

    for keyword, points in IMPACT_POINTS.items():

        if keyword in text_lower:

            score += points

            matched_positive.append(
                keyword
            )

    # Positive bonus
    positive_count = 0

    for word in POSITIVE_WORDS:

        if word in text_lower:
            positive_count += 1

    score += min(
        positive_count * 3,
        15
    )

    # Negative penalty
    for word in NEGATIVE_WORDS:

        if word in text_lower:

            score -= 20

            matched_negative.append(
                word
            )

    # Irrelevant penalty
    for word in IRRELEVANT_WORDS:

        if word in text_lower:

            score -= 8

    # Determine sentiment
    if matched_negative and score < 15:

        sentiment = "NEGATIVE"

    elif matched_negative and not matched_positive:

        sentiment = "NEGATIVE"

    elif matched_positive:

        sentiment = "POSITIVE"

    else:

        sentiment = "NEUTRAL"

    # Impact
    if score >= 30:

        impact = "HIGH"

    elif score >= 20:

        impact = "MEDIUM"

    elif score >= 10:

        impact = "LOW"

    else:

        impact = "VERY LOW"

    return {
        "score": score,
        "positive": matched_positive,
        "negative": matched_negative,
        "sentiment": sentiment,
        "impact": impact,
    }


# ============================================================
# FAST STOCK IDENTIFICATION
# ============================================================

def identify_stock_fast(
    item,
    lookup
):

    # --------------------------------------------------------
    # STEP 1: Direct NSE symbol fields
    # --------------------------------------------------------

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

        symbol = (
            str(value)
            .strip()
            .upper()
        )

        if symbol in lookup["symbol"]:

            return symbol

    # --------------------------------------------------------
    # STEP 2: Announcement text
    # --------------------------------------------------------

    text = normalize_text(
        announcement_text(item)
    )

    if not text:
        return None

    # --------------------------------------------------------
    # STEP 3: Exact company name
    # --------------------------------------------------------

    for company_name, symbol in lookup["name"].items():

        if len(company_name) >= 8:

            if company_name in text:

                return symbol

    # --------------------------------------------------------
    # STEP 4: Token based matching
    # --------------------------------------------------------

    words = set(text.split())

    possible_matches = {}

    for word in words:

        if len(word) < 5:
            continue

        symbols = lookup["token"].get(
            word
        )

        if not symbols:
            continue

        for symbol in symbols:

            possible_matches[symbol] = (
                possible_matches.get(
                    symbol,
                    0
                ) + 1
            )

    if not possible_matches:

        return None

    # Require at least one meaningful token.
    # If multiple companies match, choose the one
    # with the highest number of matching tokens.

    best_symbol = max(
        possible_matches,
        key=possible_matches.get
    )

    best_count = possible_matches[
        best_symbol
    ]

    # Single distinctive token is acceptable
    if best_count >= 1:

        return best_symbol

    return None


# ============================================================
# NEWS SUMMARY GENERATOR
# ============================================================

def generate_news_summary(
    text,
    scoring
):

    clean = clean_text(text)

    lower = clean.lower()

    positive = scoring["positive"]
    negative = scoring["negative"]

    # --------------------------------------------------------
    # EVENT TYPE
    # --------------------------------------------------------

    if (
        "order" in lower
        or "contract" in lower
        or "work order" in lower
    ):

        summary = (
            "The company has reported a new order/"
            "contract or business win."
        )

        why = (
            "The development may improve order visibility "
            "and future revenue prospects."
        )

    elif (
        "acquisition" in lower
        or "merger" in lower
    ):

        summary = (
            "The company has announced an acquisition "
            "or merger-related development."
        )

        why = (
            "The transaction could change the company's "
            "business scale, assets or growth profile."
        )

    elif (
        "stake" in lower
        or "investment" in lower
    ):

        summary = (
            "A significant stake/investment-related "
            "corporate development has been announced."
        )

        why = (
            "A material stake or investment can affect "
            "ownership structure and investor sentiment."
        )

    elif (
        "qip" in lower
        or "fund raise" in lower
        or "fundraising" in lower
        or "preferential" in lower
    ):

        summary = (
            "The company has announced a fund-raising "
            "or capital-raising exercise."
        )

        why = (
            "Fresh capital can support expansion or "
            "reduce funding constraints, while dilution "
            "may also need to be considered."
        )

    elif (
        "approval" in lower
        or "approved" in lower
    ):

        summary = (
            "The company has received or announced "
            "a regulatory/business approval."
        )

        why = (
            "Approval can remove a regulatory hurdle "
            "and potentially enable the related business activity."
        )

    elif (
        "capacity" in lower
        or "expansion" in lower
        or "plant" in lower
        or "new facility" in lower
    ):

        summary = (
            "The company has announced a capacity, "
            "plant or business expansion."
        )

        why = (
            "Additional capacity may support future "
            "production and revenue growth."
        )

    elif (
        "commercial production" in lower
        or "production" in lower
    ):

        summary = (
            "The company has reported a production-related "
            "milestone or commercial production update."
        )

        why = (
            "Commercial production can indicate progress "
            "toward revenue generation from the project."
        )

    elif (
        "profit" in lower
        or "revenue" in lower
        or "ebitda" in lower
        or "results" in lower
    ):

        summary = (
            "The company has reported a financial/results "
            "or business-performance update."
        )

        why = (
            "Profit, revenue and EBITDA trends can affect "
            "earnings expectations and market sentiment."
        )

    elif negative:

        summary = (
            "The company has reported a potentially "
            "negative corporate or regulatory development."
        )

        why = (
            "The development may affect investor sentiment "
            "and the company's business outlook."
        )

    else:

        summary = (
            "The company has reported a material "
            "corporate development."
        )

        why = (
            "The announcement may influence short-term "
            "market sentiment depending on its financial impact."
        )

    # --------------------------------------------------------
    # ADD SPECIFIC KEYWORDS
    # --------------------------------------------------------

    if positive:

        triggers = ", ".join(
            positive[:4]
        )

        summary += (
            f" Key trigger: {triggers}."
        )

    if negative:

        negatives = ", ".join(
            negative[:3]
        )

        summary += (
            f" Negative factors mentioned: {negatives}."
        )

    return summary, why


# ============================================================
# PROCESS ANNOUNCEMENTS
# ============================================================

def process_announcements(
    records,
    stocks
):

    print(
        f"\nProcessing {len(records)} "
        f"NSE announcements..."
    )

    lookup = build_company_lookup(
        stocks
    )

    candidates = []

    fresh_count = 0
    relevant_count = 0
    identified_count = 0

    reject_reasons = {}

    seen = set()

    # --------------------------------------------------------
    # Sort by newest first
    # --------------------------------------------------------

    processed = []

    for item in records:

        if not isinstance(item, dict):
            continue

        if not is_fresh_news(item):
            continue

        fresh_count += 1

        dt = parse_news_datetime(
            item
        )

        if dt is None:

            dt = datetime.min.replace(
                tzinfo=IST
            )

        processed.append(
            (
                dt,
                item
            )
        )

    processed.sort(
        key=lambda x: x[0],
        reverse=True
    )

    print(
        f"Fresh announcements: "
        f"{fresh_count}"
    )

    # --------------------------------------------------------
    # Process
    # --------------------------------------------------------

    for index, (_, item) in enumerate(
        processed,
        start=1
    ):

        text_value = announcement_text(
            item
        )

        title = get_news_title(
            item
        )

        # No text
        if not text_value:

            reason = "Empty announcement"

            reject_reasons[reason] = (
                reject_reasons.get(
                    reason,
                    0
                ) + 1
            )

            if SHOW_FILTER_DIAGNOSTICS:

                print(
                    f"[FILTERED] {title} | "
                    f"{reason}"
                )

            continue

        # ----------------------------------------------------
        # Relevance
        # ----------------------------------------------------

        relevant, reason = check_relevance(
            text_value
        )

        if not relevant:

            reject_reasons[reason] = (
                reject_reasons.get(
                    reason,
                    0
                ) + 1
            )

            if SHOW_FILTER_DIAGNOSTICS:

                print(
                    f"[FILTERED] {title[:100]} | "
                    f"{reason}"
                )

            continue

        relevant_count += 1

        # ----------------------------------------------------
        # Identify stock
        # ----------------------------------------------------

        symbol = identify_stock_fast(
            item,
            lookup
        )

        if not symbol:

            reason = "Stock/company not identified"

            reject_reasons[reason] = (
                reject_reasons.get(
                    reason,
                    0
                ) + 1
            )

            if SHOW_FILTER_DIAGNOSTICS:

                print(
                    f"[FILTERED] {title[:100]} | "
                    f"{reason}"
                )

            continue

        identified_count += 1

        # ----------------------------------------------------
        # Score
        # ----------------------------------------------------

        scoring = score_news(
            text_value
        )

        score = scoring["score"]

        # Do not allow very weak news
        if score < 10:

            reason = (
                f"Very low impact score ({score})"
            )

            reject_reasons[reason] = (
                reject_reasons.get(
                    reason,
                    0
                ) + 1
            )

            if SHOW_FILTER_DIAGNOSTICS:

                print(
                    f"[FILTERED] "
                    f"{symbol} | "
                    f"{title[:90]} | "
                    f"{reason}"
                )

            continue

        # ----------------------------------------------------
        # Duplicate
        # ----------------------------------------------------

        key = (
            symbol,
            normalize_text(title)
        )

        if key in seen:

            reason = "Duplicate"

            reject_reasons[reason] = (
                reject_reasons.get(
                    reason,
                    0
                ) + 1
            )

            continue

        seen.add(key)

        # ----------------------------------------------------
        # Summary
        # ----------------------------------------------------

        summary, why = generate_news_summary(
            text_value,
            scoring
        )

        dt = parse_news_datetime(
            item
        )

        candidates.append({

            "symbol": symbol,

            "score": score,

            "title": title,

            "text": text_value,

            "summary": summary,

            "why": why,

            "datetime": dt,

            "positive": scoring[
                "positive"
            ],

            "negative": scoring[
                "negative"
            ],

            "sentiment": scoring[
                "sentiment"
            ],

            "impact": scoring[
                "impact"
            ],
        })

        # ----------------------------------------------------
        # Progress
        # ----------------------------------------------------

        if (
            index % 100 == 0
            or index == len(processed)
        ):

            print(
                f"Processed "
                f"{index}/{len(processed)} | "
                f"Relevant: {relevant_count} | "
                f"Identified: {identified_count} | "
                f"Candidates: {len(candidates)}"
            )

    # --------------------------------------------------------
    # Sort
    # --------------------------------------------------------

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    print(
        "\n----- NSE PROCESSING COMPLETE -----"
    )

    print(
        "Fresh:",
        fresh_count
    )

    print(
        "Relevant:",
        relevant_count
    )

    print(
        "Identified:",
        identified_count
    )

    print(
        "Candidates:",
        len(candidates)
    )

    # --------------------------------------------------------
    # Rejection report
    # --------------------------------------------------------

    if reject_reasons:

        print(
            "\n----- FILTER REPORT -----"
        )

        for reason, count in sorted(
            reject_reasons.items(),
            key=lambda x: x[1],
            reverse=True
        ):

            print(
                f"{reason}: {count}"
            )

    # --------------------------------------------------------
    # Price check limit
    # --------------------------------------------------------

    candidates = candidates[
        :MAX_PRICE_CHECKS
    ]

    print(
        f"\nCandidates sent for "
        f"price check: {len(candidates)}"
    )

    return candidates


# ============================================================
# YAHOO PRICE
# ============================================================

def get_yahoo_price(symbol):

    if symbol in PRICE_CACHE:

        return PRICE_CACHE[
            symbol
        ]

    url = YAHOO_CHART.format(
        symbol
    )

    try:

        response = requests.get(
            url,
            params={
                "range": "2d",
                "interval": "1d",
            },
            headers={
                "User-Agent":
                    HEADERS[
                        "User-Agent"
                    ]
            },
            timeout=8
        )

        if response.status_code != 200:

            PRICE_CACHE[
                symbol
            ] = None

            return None

        data = response.json()

        result = (
            data
            .get("chart", {})
            .get("result")
        )

        if not result:

            PRICE_CACHE[
                symbol
            ] = None

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

            PRICE_CACHE[
                symbol
            ] = None

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

        result_data = {
            "price": price,
            "change_pct": change_pct,
        }

        PRICE_CACHE[
            symbol
        ] = result_data

        return result_data

    except Exception as e:

        print(
            f"Yahoo error {symbol}: "
            f"{type(e).__name__}"
        )

        PRICE_CACHE[
            symbol
        ] = None

        return None


# ============================================================
# FINAL RANKING
# ============================================================

def rank_candidates(
    candidates
):

    print(
        "\n----- PRICE CHECK -----"
    )

    final = []

    for index, candidate in enumerate(
        candidates,
        start=1
    ):

        symbol = candidate[
            "symbol"
        ]

        print(
            f"Price check "
            f"{index}/{len(candidates)}: "
            f"{symbol}"
        )

        price_data = get_yahoo_price(
            symbol
        )

        if price_data:

            candidate[
                "price"
            ] = price_data[
                "price"
            ]

            candidate[
                "change_pct"
            ] = price_data[
                "change_pct"
            ]

        else:

            candidate[
                "price"
            ] = None

            candidate[
                "change_pct"
            ] = None

        # ----------------------------------------------------
        # Price confirmation
        # ----------------------------------------------------

        price_change = candidate[
            "change_pct"
        ]

        if price_change is not None:

            if price_change >= 2:

                candidate[
                    "score"
                ] += 12

            elif price_change >= 1:

                candidate[
                    "score"
                ] += 7

            elif price_change > 0:

                candidate[
                    "score"
                ] += 3

            elif price_change < -3:

                candidate[
                    "score"
                ] -= 8

        # Negative news penalty
        if candidate[
            "negative"
        ]:

            candidate[
                "score"
            ] -= 15

        final.append(
            candidate
        )

    final.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    return final[
        :MAX_FINAL_STOCKS
    ]


# ============================================================
# GOOGLE NEWS
# ============================================================

GOOGLE_QUERIES = [
    "Indian stocks order win contract company shares",
    "Indian stocks acquisition merger stake company shares",
    "Indian stocks fundraising QIP preferential issue company",
    "Indian stocks expansion capacity plant company",
    "Indian stocks regulatory approval company shares",
    "Indian stocks results business update company shares",
    "Indian stocks promoter stake block deal company",
    "Indian stocks positive negative corporate news",
]


def get_google_news():

    print(
        "\n----- GOOGLE NEWS -----"
    )

    all_items = []

    for query in GOOGLE_QUERIES:

        url = (
            "https://news.google.com/rss/search?q="
            + quote_plus(query)
            + "&hl=en-IN&gl=IN&ceid=IN:en"
        )

        try:

            response = requests.get(
                url,
                headers={
                    "User-Agent":
                        HEADERS[
                            "User-Agent"
                        ]
                },
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code != 200:
                continue

            root = ET.fromstring(
                response.content
            )

            for item in root.findall(
                ".//item"
            ):

                title = (
                    item.findtext(
                        "title"
                    )
                    or ""
                )

                link = (
                    item.findtext(
                        "link"
                    )
                    or ""
                )

                pub_date = (
                    item.findtext(
                        "pubDate"
                    )
                    or ""
                )

                all_items.append({

                    "title":
                        clean_text(title),

                    "link":
                        link,

                    "date":
                        pub_date,
                })

        except Exception as e:

            print(
                "Google News error:",
                type(e).__name__
            )

    print(
        "Google News articles collected:",
        len(all_items)
    )

    return all_items


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(
    message
):

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
                "chat_id":
                    chat_id,

                "text":
                    message,

                "parse_mode":
                    "HTML",

                "disable_web_page_preview":
                    True,
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
            type(e).__name__
        )

        return False


# ============================================================
# TELEGRAM MESSAGE
# ============================================================

def build_message(
    final_candidates
):

    current_time = now_ist().strftime(
        "%d-%m-%Y %H:%M:%S"
    )

    lines = []

    lines.append(
        "<b>🚨 STOCK NEWS ALERT</b>"
    )

    lines.append(
        f"🕒 {current_time} IST"
    )

    lines.append("")

    if not final_candidates:

        lines.append(
            "⚠️ No quality stock-moving "
            "news candidate found."
        )

        lines.append(
            "NSE corporate announcements checked."
        )

        lines.append("")

        lines.append(
            "Made by Prakash Kanki"
        )

        return "\n".join(lines)

    for i, item in enumerate(
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

        why = html.escape(
            item["why"]
        )

        score = item["score"]

        sentiment = item[
            "sentiment"
        ]

        impact = item[
            "impact"
        ]

        # ----------------------------------------------------
        # Sentiment icon
        # ----------------------------------------------------

        if sentiment == "POSITIVE":

            sentiment_icon = "🟢"

        elif sentiment == "NEGATIVE":

            sentiment_icon = "🔴"

        else:

            sentiment_icon = "🟡"

        # ----------------------------------------------------
        # News date
        # ----------------------------------------------------

        news_dt = item.get(
            "datetime"
        )

        if news_dt:

            news_time = news_dt.strftime(
                "%d-%m-%Y %H:%M"
            )

        else:

            news_time = "N/A"

        # ----------------------------------------------------
        # Price
        # ----------------------------------------------------

        price = item.get(
            "price"
        )

        change_pct = item.get(
            "change_pct"
        )

        lines.append(
            f"<b>{i}. {symbol}</b>"
        )

        lines.append(
            f"{sentiment_icon} "
            f"<b>{sentiment}</b> | "
            f"Impact: <b>{impact}</b>"
        )

        lines.append(
            f"🔥 News Score: <b>{score}</b>"
        )

        if price is not None:

            if change_pct is not None:

                lines.append(
                    f"💰 Price: ₹{price:.2f} "
                    f"({change_pct:+.2f}%)"
                )

            else:

                lines.append(
                    f"💰 Price: ₹{price:.2f}"
                )

        lines.append("")

        # ----------------------------------------------------
        # ORIGINAL NEWS
        # ----------------------------------------------------

        lines.append(
            f"📰 <b>News:</b> {title}"
        )

        # ----------------------------------------------------
        # SUMMARY
        # ----------------------------------------------------

        lines.append(
            f"📋 <b>Summary:</b> {summary}"
        )

        # ----------------------------------------------------
        # WHY IT MATTERS
        # ----------------------------------------------------

        lines.append(
            f"🎯 <b>Why it matters:</b> {why}"
        )

        # ----------------------------------------------------
        # TRIGGERS
        # ----------------------------------------------------

        if item["positive"]:

            triggers = ", ".join(
                item["positive"][:5]
            )

            lines.append(
                "✅ <b>Triggers:</b> "
                + html.escape(
                    triggers
                )
            )

        if item["negative"]:

            negatives = ", ".join(
                item["negative"][:3]
            )

            lines.append(
                "⚠️ <b>Negative:</b> "
                + html.escape(
                    negatives
                )
            )

        lines.append(
            f"🕒 News time: {news_time} IST"
        )

        lines.append("")

        lines.append(
            "━━━━━━━━━━━━━━"
        )

        lines.append("")

    lines.append(
        "⚠️ News-based watchlist. "
        "Not investment advice."
    )

    lines.append(
        "Made by Prakash Kanki"
    )

    return "\n".join(lines)


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 50)

    print(
        "STOCK NEWS BOT STARTED"
    )

    print(
        now_ist().isoformat()
    )

    print("=" * 50)

    # --------------------------------------------------------
    # 1. STOCK LIST
    # --------------------------------------------------------

    print(
        "\n========== BUILDING CANDIDATES =========="
    )

    stocks = get_nse_symbols()

    if not stocks:

        print(
            "No NSE stocks loaded."
        )

        return

    # --------------------------------------------------------
    # 2. NSE NEWS
    # --------------------------------------------------------

    records = get_nse_announcements()

    if not records:

        print(
            "No NSE announcements received."
        )

        return

    # --------------------------------------------------------
    # 3. PROCESS
    # --------------------------------------------------------

    candidates = process_announcements(
        records,
        stocks
    )

    # --------------------------------------------------------
    # 4. PRICE CHECK
    # --------------------------------------------------------

    final_candidates = rank_candidates(
        candidates
    )

    # --------------------------------------------------------
    # 5. FINAL RESULT
    # --------------------------------------------------------

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
            item["score"],
            "| Sentiment:",
            item["sentiment"],
            "| Impact:",
            item["impact"]
        )

        print(
            "  News:",
            item["title"]
        )

        print(
            "  Summary:",
            item["summary"]
        )

        print(
            "  Why:",
            item["why"]
        )

    # --------------------------------------------------------
    # 6. TELEGRAM
    # --------------------------------------------------------

    message = build_message(
        final_candidates
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
