import argparse
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from binance.client import Client
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
DEFAULT_QUOTE = "USDT"
PRICE_BRIDGES = ("USDT", "BUSD", "USDC", "BTC", "ETH", "BNB")


def parse_decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def load_client():
    if not API_KEY or not API_SECRET:
        raise SystemExit(
            "Missing Binance credentials. Set BINANCE_API_KEY and BINANCE_API_SECRET in environment or .env."
        )
    return Client(API_KEY, API_SECRET)


def build_price_map(client):
    tickers = client.get_all_tickers()
    return {ticker["symbol"]: parse_decimal(ticker["price"]) for ticker in tickers}


def normalize_asset(asset):
    if asset == "RWUSD":
        return "USD"
    if asset.startswith("LD") and len(asset) > 2:
        return asset[2:]
    return asset


def quote_price(asset, quote_asset, prices):
    asset = normalize_asset(asset)

    if asset == "USD":
        if quote_asset in {"USD", "USDT", "BUSD", "USDC"}:
            return Decimal(1)
        return quote_price("USDT", quote_asset, prices)

    if asset == quote_asset:
        return Decimal(1)

    direct = f"{asset}{quote_asset}"
    reverse = f"{quote_asset}{asset}"

    if direct in prices:
        return prices[direct]

    if reverse in prices and prices[reverse] > 0:
        return Decimal(1) / prices[reverse]

    if asset in prices and asset.endswith(quote_asset):
        return prices[asset]

    for bridge in PRICE_BRIDGES:
        if bridge == asset or bridge == quote_asset:
            continue

        asset_bridge = f"{asset}{bridge}"
        bridge_asset = f"{bridge}{asset}"
        bridge_quote = f"{bridge}{quote_asset}"
        quote_bridge = f"{quote_asset}{bridge}"

        if asset_bridge in prices and bridge_quote in prices:
            return prices[asset_bridge] * prices[bridge_quote]

        if bridge_asset in prices and bridge_quote in prices and prices[bridge_asset] > 0:
            return (Decimal(1) / prices[bridge_asset]) * prices[bridge_quote]

        if asset_bridge in prices and quote_bridge in prices and prices[quote_bridge] > 0:
            return prices[asset_bridge] / prices[quote_bridge]

        if bridge_asset in prices and quote_bridge in prices:
            return (Decimal(1) / prices[bridge_asset]) / prices[quote_bridge]

    if asset in prices:
        return prices[asset]

    return None


def get_snapshot_total_btc(client):
    try:
        snapshot = client.get_account_snapshot(type="SPOT", size=1)
        snapshot_vos = snapshot.get("snapshotVos", [])
        if not snapshot_vos:
            return None
        total_btc = snapshot_vos[0].get("data", {}).get("totalAssetOfBtc")
        return parse_decimal(total_btc) if total_btc else None
    except Exception:
        return None


def load_balances(client):
    account = client.get_account()
    balances = []
    for item in account.get("balances", []):
        free = parse_decimal(item.get("free"))
        locked = parse_decimal(item.get("locked"))
        total = free + locked
        if total > 0:
            balances.append({"asset": item["asset"], "total": total})
    return balances


def format_ms(ms):
    if not ms:
        return "N/A"
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def load_auto_invest_status(client):
    try:
        response = client.margin_v1_get_lending_auto_invest_plan_list()
        plans = response.get("plans", [])
        summary = {
            "planValueInUSD": parse_decimal(response.get("planValueInUSD", "0")),
            "planValueInBTC": parse_decimal(response.get("planValueInBTC", "0")),
            "pnlInUSD": parse_decimal(response.get("pnlInUSD", "0")),
            "roi": Decimal(str(response.get("roi", "0"))) if response.get("roi") is not None else Decimal(0),
        }
        return plans, summary
    except Exception:
        return None, None


def load_auto_invest_plan_details(client, plan_id):
    try:
        response = client.margin_v1_get_lending_auto_invest_plan_id(planId=plan_id)
        return response.get("details", [])
    except Exception:
        return []


def calculate_total_value(balances, prices, quote_asset):
    total_value = Decimal(0)
    rows = []
    missing = []

    for balance in balances:
        asset = balance["asset"]
        amount = balance["total"]
        price = quote_price(asset, quote_asset, prices)

        if price is None:
            missing.append(asset)
            continue

        value = amount * price
        total_value += value
        rows.append((asset, amount, price, value))

    return total_value, rows, missing


def convert_btc_snapshot(snapshot_btc, quote_asset, prices):
    if snapshot_btc is None:
        return None
    if quote_asset == "BTC":
        return snapshot_btc
    btc_price = quote_price("BTC", quote_asset, prices)
    if btc_price is None:
        return None
    return snapshot_btc * btc_price


def main():
    parser = argparse.ArgumentParser(description="輸出 Binance 帳戶總餘額 (預設 USDT)。")
    parser.add_argument(
        "--quote",
        default=os.getenv("BN_TOTAL_QUOTE", DEFAULT_QUOTE),
        help="要計算的目標計價資產，預設 USDT。",
    )
    parser.add_argument(
        "--detail",
        action="store_true",
        help="顯示各資產金額與價格明細。",
    )

    args = parser.parse_args()
    quote_asset = args.quote.upper()

    client = load_client()
    prices = build_price_map(client)
    balances = load_balances(client)

    snapshot_total_btc = get_snapshot_total_btc(client)
    snapshot_total_value = convert_btc_snapshot(snapshot_total_btc, quote_asset, prices)

    total_value, rows, missing = calculate_total_value(balances, prices, quote_asset)

    if missing and snapshot_total_value is not None:
        print(
            f"Total balance in {quote_asset} (snapshot fallback): {snapshot_total_value:.2f}"
        )
        print("Note: some assets could not be priced individually, using Binance account snapshot for the full balance.")
    else:
        print(f"Total balance in {quote_asset}: {total_value:.2f}")

    print(f"Assets with non-zero balance: {len(balances)}")

    plans, plan_summary = load_auto_invest_status(client)
    if plan_summary is not None:
        print("\nAuto-invest summary:")
        print(f"- Active plans: {len(plans)}")
        print(f"- Total plan value: ${plan_summary['planValueInUSD']:.2f} ({plan_summary['planValueInBTC']:.8f} BTC)")
        print(f"- PnL: ${plan_summary['pnlInUSD']:.2f}")
        print(f"- ROI: {plan_summary['roi']:.4f}")
        if plans:
            print("\nAuto-invest plans:")
            for plan in plans:
                plan_id = plan.get("planId", "N/A")
                status = plan.get("status", "N/A")
                source_asset = plan.get("sourceAsset", "N/A")
                target_asset = plan.get("targetAsset", "N/A")
                subscription_amount = plan.get("subscriptionAmount", "N/A")
                subscription_cycle = plan.get("subscriptionCycle", "N/A")
                next_execution = format_ms(plan.get("nextExecutionDateTime"))
                total_invested = parse_decimal(plan.get("totalInvestedInUSD", "0"))
                plan_value = parse_decimal(plan.get("planValueInUSD", "0"))
                print(
                    f"- planId {plan_id}: {status} | {source_asset} -> {target_asset} | "
                    f"{subscription_amount} per {subscription_cycle} | next: {next_execution} | "
                    f"invested: ${total_invested:.2f} | value: ${plan_value:.2f}"
                )
                details = load_auto_invest_plan_details(client, plan_id)
                if details:
                    print("  Plan assets:")
                    for detail in details:
                        target = detail.get("targetAsset", "N/A")
                        avg_price = parse_decimal(detail.get("averagePriceInUSD", "0"))
                        total_invested_asset = parse_decimal(detail.get("totalInvestedInUSD", "0"))
                        purchased = detail.get("purchasedAmount", "N/A")
                        purchased_unit = detail.get("purchasedAmountUnit", "N/A")
                        pnl = parse_decimal(detail.get("pnlInUSD", "0"))
                        roi = Decimal(str(detail.get("roi", "0"))) if detail.get("roi") is not None else Decimal(0)
                        percent = detail.get("percentage", "N/A")
                        asset_status = detail.get("assetStatus", "N/A")
                        print(
                            f"    - {target}: {purchased} {purchased_unit} | avg ${avg_price:.2f} | invested ${total_invested_asset:.2f} | pnl ${pnl:.2f} | roi {roi:.4f} | {percent}% | {asset_status}"
                        )
                else:
                    print("  No plan asset details available.")

    if args.detail:
        print("\nAsset details:")
        for asset, amount, price, value in sorted(rows, key=lambda row: row[3], reverse=True):
            print(f"- {asset}: {amount} × {price:.8f} = {value:.2f} {quote_asset}")

    if missing:
        print(f"\nCould not price the following assets in {quote_asset}:")
        print(", ".join(sorted(missing)))
        if snapshot_total_value is None:
            print("請確認這些資產是否有對應的交易對價格。")


if __name__ == "__main__":
    main()
