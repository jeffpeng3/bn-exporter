import os
from collections import defaultdict
from binance.client import Client
from dotenv import load_dotenv
from time import time

load_dotenv()

API_PAGE_SIZE = 100

STABLE_ASSET_MAP = {
    "RWUSD": "USDC",
    "USD": "USDC",
}


class Binance:
    def __init__(self):
        API_KEY = os.getenv("BINANCE_API_KEY")
        API_SECRET = os.getenv("BINANCE_API_SECRET")
        if not API_KEY or not API_SECRET:
            raise SystemExit(
                "Missing Binance credentials. Set BINANCE_API_KEY and BINANCE_API_SECRET in environment or .env."
            )
        self.client: Client = Client(API_KEY, API_SECRET)
        self.prices: dict[str, float] = {}
        self.balances: dict[str, float] | None = None
        self.earn_balances: dict[str, float] | None = None
        self.price_cache_timestamp: float = 0.0

    def update_price_map(self) -> None:
        tickers = self.client.get_all_tickers()
        self.prices = {ticker["symbol"]: float(ticker["price"]) for ticker in tickers}
        self.price_cache_timestamp = time()

    def quote_price(self, asset: str) -> float:
        if time() - self.price_cache_timestamp > 60:  # Update every 60 seconds
            self.update_price_map()
        asset = STABLE_ASSET_MAP.get(asset, asset)
        if asset == "USDT":
            return 1.0

        direct = f"{asset}USDT"
        if direct in self.prices:
            return self.prices[direct]

        print(f"!!!!! Price for {asset} against USDT not found !!!!!")
        return 0.0

    def get_earn_balances(self) -> dict[str, float]:
        earns = [
            self.client.get_simple_earn_flexible_product_position,
            self.client.get_simple_earn_locked_product_position,
        ]
        ret = defaultdict(float)
        for earn_func in earns:
            for page in range(1, 1000):
                response = earn_func(current=page, size=API_PAGE_SIZE)
                page_rows = response.get("rows", [])
                if not page_rows:
                    break
                for item in page_rows:
                    asset = item["asset"]
                    total_amount = float(item["totalAmount"])
                    ret[asset] += total_amount
                total = int(response.get("total", "0"))
                if page * API_PAGE_SIZE >= total:
                    break
        self.earn_balances = ret
        return ret

    def get_balances(self) -> dict[str, float]:
        account = self.client.get_account()
        balances_by_asset = defaultdict(float)
        for item in account.get("balances", []):
            free = float(item.get("free"))
            locked = float(item.get("locked"))
            total = free + locked
            asset = str(item.get("asset", ""))
            if asset.startswith("LD") and len(asset) > 2:
                continue
            if asset and total > 0:
                balances_by_asset[asset] += total

        earn_balances = self.get_earn_balances()
        for asset, total in earn_balances.items():
            balances_by_asset[asset] += total

        self.balances = balances_by_asset
        return balances_by_asset

    def load_auto_invest_status(self) -> tuple[list[dict[str, str]], dict[str, float]]:
        response = self.client.margin_v1_get_lending_auto_invest_plan_list()
        plans = response.get("plans", [])
        summary = {
            "planValueInUSD": float(response.get("planValueInUSD", "0")),
            "planValueInBTC": float(response.get("planValueInBTC", "0")),
            "pnlInUSD": float(response.get("pnlInUSD", "0")),
            "roi": float(response.get("roi", "0")),
        }
        return plans, summary

    def load_auto_invest_plan_details(self, plan_id: object) -> list[dict[str, str]]:
        if plan_id is None:
            return []
        try:
            response = self.client.margin_v1_get_lending_auto_invest_plan_id(
                planId=plan_id
            )
            return response.get("details", [])
        except Exception:
            return []

    def calculate_total_value(
        self,
        balances: dict[str, float],
    ) -> tuple[float, list[tuple[str, float, float, float]]]:
        total_value = 0.0
        rows: list[tuple[str, float, float, float]] = []
        for asset, amount in balances.items():
            price = self.quote_price(asset)
            value = amount * price
            total_value += value
            rows.append((asset, amount, price, value))
        return total_value, rows


if __name__ == "__main__":
    binance = Binance()
    balances = binance.get_balances()
    total_value, rows = binance.calculate_total_value(balances)

    print(f"Total balance in USDT: {total_value:.2f}")

    print(f"Assets with non-zero balance: {len(balances)}")

    print("\nAsset details:")
    for asset, amount, price, value in sorted(
        rows, key=lambda row: row[3], reverse=True
    ):
        print(f"- {asset}: {amount} × {price:.8f} = {value:.2f} USDT")

    plans, plan_summary = binance.load_auto_invest_status()
    if plan_summary is not None:
        print("\nAuto-invest summary:")
        print(f"- Active plans: {len(plans)}")  # type: ignore
        print(
            f"- Total plan value: ${plan_summary['planValueInUSD']:.2f} ({plan_summary['planValueInBTC']:.8f} BTC)"
        )
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
                total_invested = float(plan.get("totalInvestedInUSD", "0"))
                plan_value = float(plan.get("planValueInUSD", "0"))
                print(
                    f"- planId {plan_id}: {status} | {source_asset} -> {target_asset} | "
                    f"{subscription_amount} per {subscription_cycle} | "
                    f"invested: ${total_invested:.2f} | value: ${plan_value:.2f}"
                )
                details = binance.load_auto_invest_plan_details(plan_id)
                if details:
                    print("  Plan assets:")
                    for detail in details:
                        target = detail.get("targetAsset", "N/A")
                        avg_price = float(detail.get("averagePriceInUSD", "0"))
                        total_invested_asset = float(
                            detail.get("totalInvestedInUSD", "0")
                        )
                        purchased = detail.get("purchasedAmount", "N/A")
                        purchased_unit = detail.get("purchasedAmountUnit", "N/A")
                        pnl = float(detail.get("pnlInUSD", "0"))
                        roi = (
                            float(detail.get("roi", "0"))
                            if detail.get("roi") is not None
                            else 0.0
                        )
                        percent = detail.get("percentage", "N/A")
                        asset_status = detail.get("assetStatus", "N/A")
                        print(
                            f"    - {target}: {purchased} {purchased_unit} | avg ${avg_price:.2f} | invested ${total_invested_asset:.2f} | pnl ${pnl:.2f} | roi {roi:.4f} | {percent}% | {asset_status}"
                        )
                else:
                    print("  No plan asset details available.")
