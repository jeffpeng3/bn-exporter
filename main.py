import asyncio
import os
from dotenv import load_dotenv
from aiohttp import web
from prometheus_client import Gauge
from prometheus_client.aiohttp import make_aiohttp_handler

from parser import Binance

load_dotenv()

BALANCE_TOTAL = Gauge("binance_balance_total_usdt", "Total Binance account balance in USDT")
BALANCE_ASSET = Gauge("binance_balance_asset_usdt", "Binance asset value in USDT", ["asset"])
AUTO_INVEST_ACTIVE_PLANS = Gauge("binance_auto_invest_active_plans", "Number of active Binance auto-invest plans")
AUTO_INVEST_PLAN_VALUE = Gauge("binance_auto_invest_plan_value_usd", "Auto-invest plan current value in USD", ["plan_id"])
AUTO_INVEST_PLAN_INVESTED = Gauge("binance_auto_invest_plan_invested_usd", "Auto-invest plan total invested in USD", ["plan_id"])
AUTO_INVEST_PLAN_PNL = Gauge("binance_auto_invest_plan_pnl_usd", "Auto-invest plan profit and loss in USD", ["plan_id"])
AUTO_INVEST_PLAN_ROI = Gauge("binance_auto_invest_plan_roi", "Auto-invest plan ROI", ["plan_id"])
AUTO_INVEST_PLAN_ASSET_INVESTED = Gauge("binance_auto_invest_plan_asset_invested_usd", "Auto-invest plan asset invested amount in USD", ["plan_id", "asset"])
AUTO_INVEST_PLAN_ASSET_PNL = Gauge("binance_auto_invest_plan_asset_pnl_usd", "Auto-invest plan asset PnL in USD", ["plan_id", "asset"])
AUTO_INVEST_PLAN_ASSET_ROI = Gauge("binance_auto_invest_plan_asset_roi", "Auto-invest plan asset ROI", ["plan_id", "asset"])
AUTO_INVEST_PLAN_ASSET_PERCENT = Gauge("binance_auto_invest_plan_asset_percentage", "Auto-invest plan asset target percentage", ["plan_id", "asset"])

app = web.Application()
app.router.add_get("/metrics", make_aiohttp_handler())

LAST_BALANCE_ASSETS = set()
LAST_PLAN_IDS = set()
LAST_PLAN_ASSETS = set()


def parse_float(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


binance = Binance()


def update_metrics():
    balances = binance.get_balances()

    total_value, rows = binance.calculate_total_value(balances)
    BALANCE_TOTAL.set(total_value)

    current_balance_assets = set()
    for asset, amount, price, value in rows:
        BALANCE_ASSET.labels(asset=asset).set(value)
        current_balance_assets.add(asset)

    for old_asset in LAST_BALANCE_ASSETS - current_balance_assets:
        try:
            BALANCE_ASSET.remove(old_asset)
        except KeyError:
            pass
    LAST_BALANCE_ASSETS.clear()
    LAST_BALANCE_ASSETS.update(current_balance_assets)

    plans, _ = binance.load_auto_invest_status()
    current_plan_ids = set()
    current_plan_assets = set()

    if plans is not None:
        AUTO_INVEST_ACTIVE_PLANS.set(len(plans))
        for plan in plans:
            plan_id = str(plan.get("planId", "unknown"))
            current_plan_ids.add(plan_id)
            AUTO_INVEST_PLAN_VALUE.labels(plan_id=plan_id).set(parse_float(plan.get("planValueInUSD")))
            AUTO_INVEST_PLAN_INVESTED.labels(plan_id=plan_id).set(parse_float(plan.get("totalInvestedInUSD")))
            AUTO_INVEST_PLAN_PNL.labels(plan_id=plan_id).set(parse_float(plan.get("pnlInUSD")))
            AUTO_INVEST_PLAN_ROI.labels(plan_id=plan_id).set(parse_float(plan.get("roi")))

            details = binance.load_auto_invest_plan_details(plan.get("planId"))
            for detail in details:
                asset_label = detail.get("targetAsset", "unknown")
                current_plan_assets.add((plan_id, asset_label))
                AUTO_INVEST_PLAN_ASSET_INVESTED.labels(plan_id=plan_id, asset=asset_label).set(parse_float(detail.get("totalInvestedInUSD")))
                AUTO_INVEST_PLAN_ASSET_PNL.labels(plan_id=plan_id, asset=asset_label).set(parse_float(detail.get("pnlInUSD")))
                AUTO_INVEST_PLAN_ASSET_ROI.labels(plan_id=plan_id, asset=asset_label).set(parse_float(detail.get("roi")))
                AUTO_INVEST_PLAN_ASSET_PERCENT.labels(plan_id=plan_id, asset=asset_label).set(parse_float(detail.get("percentage")))

    for old_plan_id in LAST_PLAN_IDS - current_plan_ids:
        try:
            AUTO_INVEST_PLAN_VALUE.remove(old_plan_id)
            AUTO_INVEST_PLAN_INVESTED.remove(old_plan_id)
            AUTO_INVEST_PLAN_PNL.remove(old_plan_id)
            AUTO_INVEST_PLAN_ROI.remove(old_plan_id)
        except KeyError:
            pass
    LAST_PLAN_IDS.clear()
    LAST_PLAN_IDS.update(current_plan_ids)

    for old_plan_id, old_asset in LAST_PLAN_ASSETS - current_plan_assets:
        try:
            AUTO_INVEST_PLAN_ASSET_INVESTED.remove(old_plan_id, old_asset)
            AUTO_INVEST_PLAN_ASSET_PNL.remove(old_plan_id, old_asset)
            AUTO_INVEST_PLAN_ASSET_ROI.remove(old_plan_id, old_asset)
            AUTO_INVEST_PLAN_ASSET_PERCENT.remove(old_plan_id, old_asset)
        except KeyError:
            pass
    LAST_PLAN_ASSETS.clear()
    LAST_PLAN_ASSETS.update(current_plan_assets)


async def exporter_loop(app: web.Application):
    while True:
        try:
            await asyncio.to_thread(update_metrics)
        except Exception as exc:
            print(f"Exporter update error: {exc}")
        await asyncio.sleep(60)


async def start_background_tasks(app: web.Application):
    app["exporter_task"] = asyncio.create_task(exporter_loop(app))


async def cleanup_background_tasks(app: web.Application):
    task = app.get("exporter_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app.on_startup.append(start_background_tasks)
app.on_cleanup.append(cleanup_background_tasks)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))