import argparse
import os
from datetime import datetime, timezone
from typing import TypedDict

from binance.client import Client
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
DEFAULT_QUOTE = "USDT"
PRICE_BRIDGES = ("USDT", "BUSD", "USDC", "BTC", "ETH", "BNB")
API_PAGE_SIZE = 100
# PRICE_BRIDGES 是用來當作間接換匯的橋樑資產，當沒有直接交易對時會嘗試透過這些資產計算價格。


def parse_float(value: object) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def load_client() -> Client:
    if not API_KEY or not API_SECRET:
        raise SystemExit(
            "Missing Binance credentials. Set BINANCE_API_KEY and BINANCE_API_SECRET in environment or .env."
        )
    return Client(API_KEY, API_SECRET)


def build_price_map(client: Client) -> dict[str, float]:
    tickers = client.get_all_tickers()
    return {ticker["symbol"]: parse_float(ticker["price"]) for ticker in tickers}


STABLE_ASSET_MAP = {
    "RWUSD": "USDC",
    "USD": "USDC",
}


def normalize_asset(asset: object) -> str:
    asset = str(asset).upper()
    # Binance may report locked savings / staking assets with an LD prefix.
    if asset.startswith("LD") and len(asset) > 2:
        asset = asset[2:]
    return STABLE_ASSET_MAP.get(asset, asset)


def quote_price(asset: object, quote_asset: object, prices: dict[str, float]) -> float | None:
    asset = normalize_asset(asset)
    quote_asset = normalize_asset(quote_asset)

    if asset == quote_asset:
        return 1.0

    direct = f"{asset}{quote_asset}"
    reverse = f"{quote_asset}{asset}"

    if direct in prices:
        return prices[direct]

    if reverse in prices and prices[reverse] > 0:
        return 1.0 / prices[reverse]

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
            return (1.0 / prices[bridge_asset]) * prices[bridge_quote]

        if asset_bridge in prices and quote_bridge in prices and prices[quote_bridge] > 0:
            return prices[asset_bridge] / prices[quote_bridge]

        if bridge_asset in prices and quote_bridge in prices and prices[bridge_asset] > 0:
            return (1.0 / prices[bridge_asset]) / prices[quote_bridge]

    return None


def get_snapshot_total_btc(client: Client) -> float | None:
    try:
        snapshot = client.get_account_snapshot(type="SPOT", size=1)
        snapshot_vos = snapshot.get("snapshotVos", [])
        if not snapshot_vos:
            return None
        total_btc = snapshot_vos[0].get("data", {}).get("totalAssetOfBtc")
        return parse_float(total_btc) if total_btc else None
    except Exception:
        return None


def _normalize_earn_position_asset(asset: object) -> str:
    asset = str(asset).upper()
    return asset if asset.startswith("LD") else f"LD{asset}"


def _load_simple_earn_flexible_positions(client: Client) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    current = 1
    while True:
        response = client.get_simple_earn_flexible_product_position(current=current, size=API_PAGE_SIZE)
        page_rows = response.get("rows", [])
        if not page_rows:
            break
        rows.extend(page_rows)
        total = int(str(response.get("total", 0) or 0))
        if current * API_PAGE_SIZE >= total:
            break
        current += 1
    return rows


def _load_simple_earn_locked_positions(client: Client) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    current = 1
    while True:
        response = client.get_simple_earn_locked_product_position(current=current, size=API_PAGE_SIZE)
        page_rows = response.get("rows", [])
        if not page_rows:
            break
        rows.extend(page_rows)
        total = int(str(response.get("total", 0) or 0))
        if current * API_PAGE_SIZE >= total:
            break
        current += 1
    return rows


class AssetBalance(TypedDict):
    asset: str
    total: float


def _build_earn_balances(rows: list[dict[str, object]]) -> list[AssetBalance]:
    balances: dict[str, float] = {}
    for item in rows:
        asset_value = item.get("asset")
        asset = str(asset_value or "")
        total_amount = parse_float(item.get("totalAmount"))
        if not asset or total_amount <= 0:
            continue
        asset_label = _normalize_earn_position_asset(asset)
        balances[asset_label] = balances.get(asset_label, 0.0) + total_amount
    return [{"asset": asset, "total": total} for asset, total in balances.items()]


def load_balances(client: Client) -> list[AssetBalance]:
    account = client.get_account()
    balances_by_asset: dict[str, float] = {}
    for item in account.get("balances", []):
        free = parse_float(item.get("free"))
        locked = parse_float(item.get("locked"))
        total = free + locked
        asset = str(item.get("asset", ""))
        if asset and total > 0:
            balances_by_asset[asset] = balances_by_asset.get(asset, 0.0) + total

    try:
        flexible_rows = _load_simple_earn_flexible_positions(client)
        for record in _build_earn_balances(flexible_rows):
            balances_by_asset[record["asset"]] = record["total"]
    except Exception:
        pass

    try:
        locked_rows = _load_simple_earn_locked_positions(client)
        for record in _build_earn_balances(locked_rows):
            balances_by_asset[record["asset"]] = balances_by_asset.get(record["asset"], 0.0) + record["total"]
    except Exception:
        pass

    return [{"asset": asset, "total": total} for asset, total in balances_by_asset.items()]


def format_ms(ms: object | None) -> str:
    if not ms:
        return "N/A"
    timestamp_ms = parse_float(ms)
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def load_auto_invest_status(client: Client) -> tuple[list[dict[str, object]] | None, dict[str, float] | None]:
    try:
        response = client.margin_v1_get_lending_auto_invest_plan_list()
        plans = response.get("plans", [])
        summary = {
            "planValueInUSD": parse_float(response.get("planValueInUSD", "0")),
            "planValueInBTC": parse_float(response.get("planValueInBTC", "0")),
            "pnlInUSD": parse_float(response.get("pnlInUSD", "0")),
            "roi": parse_float(response.get("roi", "0")) if response.get("roi") is not None else 0.0,
        }
        return plans, summary
    except Exception:
        return None, None


def load_auto_invest_plan_details(client: Client, plan_id: object) -> list[dict[str, object]]:
    try:
        response = client.margin_v1_get_lending_auto_invest_plan_id(planId=plan_id)
        return response.get("details", [])
    except Exception:
        return []


def calculate_total_value(
    balances: list[AssetBalance],
    prices: dict[str, float],
    quote_asset: str,
) -> tuple[float, list[tuple[str, float, float, float]], list[str]]:
    total_value = 0.0
    rows: list[tuple[str, float, float, float]] = []
    missing: list[str] = []

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


def convert_btc_snapshot(snapshot_btc: object, quote_asset: str, prices: dict[str, float]) -> float | None:
    if snapshot_btc is None:
        return None
    btc_value = parse_float(snapshot_btc)
    if quote_asset == "BTC":
        return btc_value
    btc_price = quote_price("BTC", quote_asset, prices)
    if btc_price is None:
        return None
    return btc_value * btc_price


def main():
    parser = argparse.ArgumentParser(description="輸出 Binance 帳戶總餘額 (USDT)。")
    parser.add_argument(
        "--detail",
        action="store_true",
        help="顯示各資產金額與價格明細。",
    )

    args = parser.parse_args()
    quote_asset = DEFAULT_QUOTE

    client = load_client()
    prices = build_price_map(client)
    balances = load_balances(client)

    snapshot_total_btc = get_snapshot_total_btc(client)
    snapshot_total_value = convert_btc_snapshot(snapshot_total_btc, quote_asset, prices)

    total_value, rows, missing = calculate_total_value(balances, prices, quote_asset)

    if missing:
        print(f"Priced total balance in {quote_asset}: {total_value:.2f}")
        if snapshot_total_value is not None:
            print(
                f"Snapshot fallback total in {quote_asset}: {snapshot_total_value:.2f}"
            )
            print(
                "Note: some assets could not be priced individually; snapshot fallback is shown for reference."
            )
        else:
            print("Note: some assets could not be priced individually.")
    else:
        print(f"Total balance in {quote_asset}: {total_value:.2f}")

    print(f"Assets with non-zero balance: {len(balances)}")

    plans, plan_summary = load_auto_invest_status(client)
    if plan_summary is not None:
        print("\nAuto-invest summary:")
        print(f"- Active plans: {len(plans)}") # type: ignore
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
                total_invested = parse_float(plan.get("totalInvestedInUSD", "0"))
                plan_value = parse_float(plan.get("planValueInUSD", "0"))
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
                        avg_price = parse_float(detail.get("averagePriceInUSD", "0"))
                        total_invested_asset = parse_float(detail.get("totalInvestedInUSD", "0"))
                        purchased = detail.get("purchasedAmount", "N/A")
                        purchased_unit = detail.get("purchasedAmountUnit", "N/A")
                        pnl = parse_float(detail.get("pnlInUSD", "0"))
                        roi = parse_float(detail.get("roi", "0")) if detail.get("roi") is not None else 0.0
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
