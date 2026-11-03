"""Mobile-first, read-only market data watchlist with GitHub-backed editing."""

import base64
import json
import math
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import pandas as pd
import streamlit as st
import yfinance as yf

DEFAULTS = ["HIMS", "SOFI", "GOOG", "APP", "NFLX", "AMZN", "CELH"]
WATCHLIST_FILE = Path(__file__).with_name("watchlist.json")
GITHUB_OWNER = "BlessedUpJasonJr"
GITHUB_REPO = "Stock-Watch"
GITHUB_BRANCH = "main"
API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/watchlist.json"
TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-^]{0,14}$")

st.set_page_config(page_title="Stock Watch", page_icon="📈", layout="centered")
st.markdown(
    """<style>
    .block-container {max-width: 680px; padding-top: 1rem; padding-left: 1rem; padding-right: 1rem;}
    h1 {font-size: 1.9rem !important; margin-bottom: .1rem;}
    div[data-testid="stMetricValue"] {font-size: 2rem;}
    div[data-testid="stMetricDelta"] {font-size: 1rem;}
    [data-testid="stVerticalBlockBorderWrapper"] {border-radius: 13px;}
    </style>""",
    unsafe_allow_html=True,
)


def token_from_secrets():
    try:
        return str(st.secrets.get("GITHUB_TOKEN", "")).strip()
    except (FileNotFoundError, KeyError):
        return ""


def validate_list(payload):
    if isinstance(payload, dict):
        payload = payload.get("tickers", [])
    if not isinstance(payload, list):
        raise ValueError("Watchlist JSON must be a list or an object with a 'tickers' list.")
    result = []
    for entry in payload:
        if not isinstance(entry, str):
            continue
        symbol = entry.strip().upper()
        if TICKER_PATTERN.fullmatch(symbol) and symbol not in result:
            result.append(symbol)
    return result


def local_watchlist():
    if not WATCHLIST_FILE.exists():
        return DEFAULTS.copy()
    try:
        return validate_list(json.loads(WATCHLIST_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError):
        st.warning("Local watchlist could not be read; showing defaults.")
        return DEFAULTS.copy()


def github_request(method, token, payload=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "stock-watch-streamlit",
    }
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(API_URL + (f"?ref={quote(GITHUB_BRANCH)}" if method == "GET" else ""),
                      data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:220]
        raise RuntimeError(f"GitHub API HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"GitHub is unavailable: {exc}") from exc


def read_github(token):
    try:
        response = github_request("GET", token)
        contents = base64.b64decode(response["content"]).decode("utf-8")
        return validate_list(json.loads(contents)), response["sha"]
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            return None, None
        raise


def save_github(tickers, token):
    _, sha = read_github(token)
    payload = {
        "message": "Update stock watchlist",
        "content": base64.b64encode((json.dumps(tickers, indent=2) + "\n").encode()).decode(),
        "branch": GITHUB_BRANCH,
    }
    if sha is not None:
        payload["sha"] = sha
    github_request("PUT", token, payload)



@st.cache_data(ttl=300, show_spinner=False)
def cached_history(symbol, period="1y", interval="1d"):
    return yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=False)


@st.cache_data(ttl=300, show_spinner=False)
def cached_info(symbol):
    try:
        return yf.Ticker(symbol).get_info() or {}
    except Exception:
        return {}


@st.cache_data(ttl=300, show_spinner=False)
def cached_news(symbol):
    try:
        return yf.Ticker(symbol).news or []
    except Exception:
        return []


def numeric(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def formatted(value, style="number"):
    number = numeric(value)
    if number is None:
        return "—"
    if style == "money":
        return f"$" + f"{number:,.2f}"
    if style == "cap":
        for divisor, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
            if abs(number) >= divisor:
                return f"$" + f"{number / divisor:,.2f}{suffix}"
        return f"$" + f"{number:,.0f}"
    if style == "percent":
        return f"{number:+.2f}%"
    return f"{number:,.2f}"


def rsi14(closes):
    if len(closes) < 15:
        return None
    change = closes.diff()
    gain = change.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().iloc[-1]
    loss = (-change.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().iloc[-1]
    if pd.isna(gain) or pd.isna(loss):
        return None
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100 - 100 / (1 + gain / loss)


def range_history(closes, selected):
    months = {"1M": 1, "6M": 6, "1Y": 12}[selected]
    cutoff = closes.index.max() - pd.DateOffset(months=months)
    result = closes.loc[closes.index >= cutoff]
    return result if not result.empty else closes


def news_items(symbol):
    items = []
    for item in cached_news(symbol):
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        data = content if isinstance(content, dict) else item
        title = data.get("title")
        provider = data.get("provider")
        publisher = (provider.get("displayName") if isinstance(provider, dict) else None) or data.get("publisher") or "Unknown publisher"
        link = data.get("link")
        for key in ("canonicalUrl", "clickThroughUrl"):
            candidate = data.get(key)
            if not link and isinstance(candidate, dict):
                link = candidate.get("url")
        if title and isinstance(link, str) and link.startswith(("https://", "http://")):
            items.append((str(title), str(publisher), link))
        if len(items) == 3:
            break
    return items


@st.cache_data(ttl=300, show_spinner=False)
def stock_data(symbol):
    ticker = yf.Ticker(symbol)
    daily = cached_history(symbol, period="1y")
    if daily.empty or "Close" not in daily:
        raise ValueError("No market data available")
    closes = daily["Close"].dropna()
    if closes.empty:
        raise ValueError("No closing prices available")

    latest = cached_history(symbol, period="1d", interval="5m")
    if not latest.empty and "Close" in latest and not latest["Close"].dropna().empty:
        price = float(latest["Close"].dropna().iloc[-1])
        previous_close = float(closes.iloc[-2]) if len(closes) > 1 else None
        try:
            yahoo_previous = ticker.fast_info.get("previous_close")
            if yahoo_previous and float(yahoo_previous) > 0:
                previous_close = float(yahoo_previous)
        except Exception:
            pass
    else:
        price = float(closes.iloc[-1])
        previous_close = float(closes.iloc[-2]) if len(closes) > 1 else None

    name = symbol
    try:
        info = cached_info(symbol)
        name = info.get("shortName") or info.get("longName") or symbol
    except Exception:
        pass
    change = price - previous_close if previous_close else None
    change_pct = 100 * change / previous_close if previous_close else None
    return name, price, change, change_pct, closes


def change_label(amount, percentage):
    if amount is None or percentage is None:
        return "Daily change unavailable"
    return f"{amount:+,.2f} USD ({percentage:+.2f}%)"


token = token_from_secrets()
can_save = bool(token)

if "tickers" not in st.session_state:
    initial = local_watchlist()
    if can_save:
        try:
            remote, _ = read_github(token)
            if remote is not None:
                initial = remote
        except (RuntimeError, ValueError, KeyError) as exc:
            st.warning(f"Couldn't load GitHub watchlist. Showing local list: {exc}")
    st.session_state.tickers = initial

st.title("Stock Watch")
st.caption("Quotes can be delayed. Prices are informational only.")
header_left, header_right = st.columns([3, 1], vertical_alignment="center")
with header_right:
    if st.button("↻ Refresh", use_container_width=True):
        stock_data.clear()
        cached_history.clear()
        cached_info.clear()
        cached_news.clear()
        if can_save:
            try:
                latest_list, _ = read_github(token)
                if latest_list is not None:
                    st.session_state.tickers = latest_list
            except (RuntimeError, ValueError, KeyError) as exc:
                st.warning(f"Could not reload list: {exc}")
        st.rerun()

if not can_save:
    st.caption("Saving is off: add GITHUB_TOKEN to Streamlit secrets to enable editing.")

with st.form("add_ticker", clear_on_submit=True):
    a, b = st.columns([3, 1], vertical_alignment="bottom")
    with a:
        new_symbol = st.text_input("Add a stock", placeholder="e.g. META", disabled=not can_save)
    with b:
        submitted = st.form_submit_button("Add", use_container_width=True, disabled=not can_save)
    if submitted:
        symbol = new_symbol.strip().upper()
        if not TICKER_PATTERN.fullmatch(symbol):
            st.error("Enter a valid ticker (letters, digits, dots, hyphens or ^).")
        elif symbol in st.session_state.tickers:
            st.info(f"{symbol} is already listed.")
        else:
            try:
                stock_data(symbol)
                revised = st.session_state.tickers + [symbol]
                save_github(revised, token)
                st.session_state.tickers = revised
                st.rerun()
            except Exception as exc:
                st.error(f"Could not add {symbol}: {exc}")

for symbol in st.session_state.tickers:
    with st.container(border=True):
        left, right = st.columns([4, 1], vertical_alignment="top")
        try:
            name, price, amount, percentage, prices = stock_data(symbol)
        except Exception as exc:
            name, price, amount, percentage, prices = symbol, None, None, None, None
            st.caption(f"{symbol}: price temporarily unavailable ({exc})")
        with left:
            st.markdown(f"**{symbol}** · {name}")
        with right:
            if st.button("Remove", key=f"rm_{symbol}", disabled=not can_save, use_container_width=True):
                revised = [ticker for ticker in st.session_state.tickers if ticker != symbol]
                try:
                    save_github(revised, token)
                    st.session_state.tickers = revised
                    st.rerun()
                except (RuntimeError, ValueError, KeyError) as exc:
                    st.error(f"Could not save removal: {exc}")
        if price is not None:
            delta = change_label(amount, percentage)
            st.metric("Latest price", f"${price:,.2f}", delta=delta if amount is not None else None)
            st.line_chart(prices)

if not st.session_state.tickers:
    st.info("No stocks in your watchlist. Add a ticker above.")
