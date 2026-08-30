import os
import sys
import json
import math
from datetime import datetime, timedelta
import requests
import matplotlib.pyplot as plt
import yfinance as yf

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

WEBHOOK_URL = os.environ.get("DISCORD_PORTFOLIO_WEBHOOK")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORTFOLIO_FILE = os.path.join(BASE_DIR, "marks_portfolio.json")
CHART_FILE = os.path.join(BASE_DIR, "portfolio_chart.png")
JAN2_DATE = "2026-01-02"   # First trading day of the year

# Cache for Jan 2 prices (to avoid fetching multiple times per run)
jan2_cache = {}

def get_price_on_date(ticker, date_str):
    """Fetch closing price for a ticker on a specific date (or nearest trading day)."""
    try:
        stock = yf.Ticker(ticker)
        # Try exact date
        hist = stock.history(start=date_str, end=date_str)
        if not hist.empty:
            return float(hist['Close'].iloc[0])
        # Fallback: try next few days (holiday handling)
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        for i in range(1, 5):
            new_date = (dt + timedelta(days=i)).strftime("%Y-%m-%d")
            hist = stock.history(start=new_date, end=new_date)
            if not hist.empty:
                return float(hist['Close'].iloc[0])
        return None
    except Exception as e:
        print(f"Warning: Could not fetch price for {ticker} on {date_str}: {e}")
        return None

def get_jan2_price(ticker):
    """Get Jan 2 price with caching."""
    if ticker not in jan2_cache:
        jan2_cache[ticker] = get_price_on_date(ticker, JAN2_DATE)
    return jan2_cache[ticker]

# -----------------------------------------------------------------------------
# Portfolio I/O (atomic)
# -----------------------------------------------------------------------------

def load_portfolio():
    try:
        with open(PORTFOLIO_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}

    data.setdefault("closed_positions", [])
    return data

def save_portfolio(portfolio):
    tmp = PORTFOLIO_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(portfolio, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, PORTFOLIO_FILE)
        print("Portfolio saved.")
    except Exception as e:
        print(f"Error saving: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)
        raise

def compute_cost_basis(transactions):
    total = 0.0
    for t in transactions:
        if t.get("action") == "BUY":
            total += t["shares"] * t["price"]
    return total

# -----------------------------------------------------------------------------
# Fetch current values and YTD
# -----------------------------------------------------------------------------

def fetch_portfolio_values(portfolio):
    results = []
    total_val = 0.0
    total_jan2_val = 0.0

    for ticker, info in portfolio.items():
        if ticker == "closed_positions":
            continue

        shares = info["shares"]
        avg_price = info["avg_price"]
        current_price = get_current_price(ticker)
        if current_price is None or current_price == 0.0:
            print(f"Warning: Using avg_price as fallback for {ticker}")
            current_price = avg_price

        val = shares * current_price
        cost = shares * avg_price
        unrealized_pct = ((val - cost) / cost * 100) if cost > 0 else 0

        # ---- YTD calculation ----
        jan2_price = get_jan2_price(ticker)
        if jan2_price is not None:
            jan2_val = shares * jan2_price
            ytd_pct = ((current_price / jan2_price) - 1) * 100
            total_jan2_val += jan2_val
        else:
            ytd_pct = None
            # still add to total? We'll skip for portfolio YTD if missing
            # We'll include it in the table as N/A

        total_val += val

        results.append({
            "ticker": ticker,
            "value": val,
            "unrealized_pct": unrealized_pct,
            "ytd_pct": ytd_pct,
            "shares": shares,
            "avg_price": avg_price,
            "current_price": current_price
        })

    # Compute allocation percentages
    for r in results:
        r["allocation_pct"] = (r["value"] / total_val * 100) if total_val > 0 else 0

    # Compute portfolio YTD (only for tickers with jan2 price)
    portfolio_ytd = None
    if total_val > 0 and total_jan2_val > 0:
        portfolio_ytd = ((total_val / total_jan2_val) - 1) * 100

    return results, total_val, portfolio_ytd

def get_current_price(ticker):
    try:
        stock = yf.Ticker(ticker)
        price = getattr(stock.fast_info, 'last_price', None) or getattr(stock.fast_info, 'lastPrice', None)
        if price is not None and math.isfinite(price):
            return float(price)
    except Exception:
        pass
    return None

def build_summary_table(results):
    """Build a Markdown table with columns: Ticker, Alloc%, Unreal%, YTD%."""
    lines = [
        "```",
        f"{'Ticker':<8} {'Alloc%':>7} {'Unreal%':>9} {'YTD%':>7}",
        "-------- ------- -------- -------"
    ]
    for r in results:
        ytd_str = f"{r['ytd_pct']:>6.1f}%" if r['ytd_pct'] is not None else "   N/A"
        lines.append(
            f"{r['ticker']:<8} {r['allocation_pct']:>6.1f}% {r['unrealized_pct']:>8.1f}% {ytd_str}"
        )
    lines.append("```")
    return "\n".join(lines)

# -----------------------------------------------------------------------------
# Chart generation
# -----------------------------------------------------------------------------

def generate_pie_chart(results):
    results.sort(key=lambda x: x['value'], reverse=True)
    labels = [f"{item['ticker']} ({item['allocation_pct']:.1f}%)" for item in results]
    values = [item['value'] for item in results]

    plt.figure(figsize=(10, 8))
    colors = plt.cm.tab20c.colors
    plt.pie(values, labels=labels, autopct='%1.1f%%', startangle=140,
            colors=colors, pctdistance=0.85, wedgeprops={'edgecolor': 'white', 'linewidth': 1})
    centre_circle = plt.Circle((0, 0), 0.70, fc='white')
    plt.gca().add_artist(centre_circle)
    plt.title('Portfolio Allocation', fontsize=14, fontweight='bold')
    plt.tight_layout()
    plt.savefig(CHART_FILE, dpi=200)
    plt.close()
    return CHART_FILE

# -----------------------------------------------------------------------------
# Discord posting
# -----------------------------------------------------------------------------

def post_to_discord(title, description, color, chart_path=None):
    if not WEBHOOK_URL:
        print("Error: DISCORD_PORTFOLIO_WEBHOOK not set.")
        return

    payload = {
        "username": "Marks Portfolio",
        "embeds": [{"title": title, "description": description, "color": color}]
    }

    try:
        if chart_path and os.path.exists(chart_path):
            payload["embeds"][0]["image"] = {"url": "attachment://portfolio_chart.png"}
            with open(chart_path, "rb") as f:
                files = {"file": (chart_path, f, "image/png")}
                requests.post(WEBHOOK_URL, data={"payload_json": json.dumps(payload)},
                              files=files, timeout=15)
        else:
            requests.post(WEBHOOK_URL, json=payload, timeout=15)
        print("Discord sent.")
    except requests.Timeout:
        print("Discord timeout.")
    except Exception as e:
        print(f"Discord error: {e}")

# -----------------------------------------------------------------------------
# STATUS
# -----------------------------------------------------------------------------

def post_status_summary():
    portfolio = load_portfolio()
    results, total_val, portfolio_ytd = fetch_portfolio_values(portfolio)
    chart_path = generate_pie_chart(results)
    table = build_summary_table(results)

    # Total return (based on actual cost basis + realised gains)
    open_positions = {k: v for k, v in portfolio.items() if k != "closed_positions"}
    total_cost = sum(info["shares"] * info["avg_price"] for info in open_positions.values())
    total_unrealized = total_val - total_cost
    total_realized = sum(info.get("realized_pl", 0) for info in open_positions.values())
    # Also include realized from closed positions
    closed_positions = portfolio.get("closed_positions", [])
    total_realized += sum(pos.get("total_realized_pl", 0) for pos in closed_positions)
    lifetime_return = ((total_unrealized + total_realized) / total_cost * 100) if total_cost > 0 else 0

    # Build description
    description = f"**Lifetime Return (based on actual cost):** {lifetime_return:+.1f}%\n"
    if portfolio_ytd is not None:
        description += f"**Portfolio YTD (since {JAN2_DATE}):** {portfolio_ytd:+.1f}%\n\n"
    else:
        description += "\n"
    description += table

    post_to_discord("📊 MARKS PORTFOLIO", description, color=3447003, chart_path=chart_path)

# -----------------------------------------------------------------------------
# BUY / SELL (unchanged)
# -----------------------------------------------------------------------------

def execute_trade(action, ticker, shares_change, price):
    if shares_change <= 0:
        raise ValueError("Shares must be positive.")
    if price <= 0:
        raise ValueError("Price must be positive.")
    if action not in ("BUY", "SELL"):
        raise ValueError("Action must be BUY or SELL.")

    portfolio = load_portfolio()
    portfolio.setdefault("closed_positions", [])

    if action == "BUY":
        old_info = portfolio.get(ticker, {})
        old_shares = old_info.get("shares", 0)
        old_avg = old_info.get("avg_price", 0.0)

        new_shares = old_shares + shares_change
        new_avg = ((old_shares * old_avg) + (shares_change * price)) / new_shares if new_shares > 0 else 0

        transaction = {
            "date": datetime.now().date().isoformat(),
            "action": "BUY",
            "shares": shares_change,
            "price": price,
            "fee": 0.0
        }

        portfolio[ticker] = {
            "shares": new_shares,
            "avg_price": round(new_avg, 2),
            "transactions": old_info.get("transactions", []) + [transaction],
            "realized_pl": old_info.get("realized_pl", 0.0)
        }

    elif action == "SELL":
        if ticker not in portfolio:
            raise ValueError(f"Cannot sell {ticker}: position not found.")
        old_info = portfolio[ticker]
        old_shares = old_info["shares"]
        old_avg = old_info["avg_price"]

        if shares_change > old_shares:
            raise ValueError(f"Cannot sell {shares_change} shares; only {old_shares} held.")

        realized_gain = (price - old_avg) * shares_change
        new_shares = old_shares - shares_change

        transaction = {
            "date": datetime.now().date().isoformat(),
            "action": "SELL",
            "shares": shares_change,
            "price": price,
            "fee": 0.0
        }

        new_realized_pl = old_info.get("realized_pl", 0.0) + realized_gain

        if new_shares <= 0:
            # Archive closed position
            total_cost_basis = compute_cost_basis(old_info.get("transactions", []))
            portfolio["closed_positions"].append({
                "ticker": ticker,
                "closure_date": datetime.now().date().isoformat(),
                "total_cost_basis": total_cost_basis,
                "total_realized_pl": new_realized_pl,
                "transactions": old_info.get("transactions", []) + [transaction],
            })
            del portfolio[ticker]
        else:
            portfolio[ticker]["shares"] = new_shares
            portfolio[ticker]["avg_price"] = old_avg
            portfolio[ticker]["transactions"] = old_info.get("transactions", []) + [transaction]
            portfolio[ticker]["realized_pl"] = new_realized_pl

    save_portfolio(portfolio)

    # Post updated status after trade
    results, total_val, portfolio_ytd = fetch_portfolio_values(portfolio)
    chart_path = generate_pie_chart(results)
    table = build_summary_table(results)

    ticker_info = next((r for r in results if r["ticker"] == ticker), None)
    new_weight = ticker_info["allocation_pct"] if ticker_info else 0.0

    desc = f"**{action}** {shares_change} shares of **{ticker}**\nNew weight: **{new_weight:.1f}%**\n"
    if portfolio_ytd is not None:
        desc += f"Portfolio YTD: {portfolio_ytd:+.1f}%\n"
    desc += "\n" + table

    color = 3066993 if action == "BUY" else 15158332
    post_to_discord(f"📈 PORTFOLIO UPDATE: {action} {ticker}", desc, color, chart_path=chart_path)

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1].upper() == "STATUS":
            post_status_summary()
        elif len(sys.argv) >= 5:
            action = sys.argv[1].upper()
            ticker = sys.argv[2].upper()
            shares = int(sys.argv[3])
            price = float(sys.argv[4])
            execute_trade(action, ticker, shares, price)
        else:
            print("Usage:")
            print("  STATUS: python marks_portfolio_update.py STATUS")
            print("  BUY:    python marks_portfolio_update.py BUY TICKER SHARES PRICE")
            print("  SELL:   python marks_portfolio_update.py SELL TICKER SHARES PRICE")
            sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
