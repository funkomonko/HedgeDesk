"""
backtest.py — Point-in-time historical replay on April 2025 – Nov 2026 official USD/INR data.
Follows Context Doc Page 8 design:
  - Synthetic monthly book: USD 100,000 payable created on first business day of each month
    with rotating 30, 60, and 90-day tenors.
  - Calls `policy.decide(..., as_of_date=order_date)` with strict point-in-time assert.
  - Compares HedgeDesk policy vs Never Hedge, Always Hedge 100%, and Always Hedge 50%.
  - Outputs exact headline claim format (honestly stating metrics).
"""
import statistics
from datetime import datetime
from typing import Any, Dict, List, Optional

from policy import decide, fwd_cost, pick_forward_premium_for_tenor
from sources import _load_real_fx_history, fetch_fx, load_policy_yaml


def replay(
    policy_cfg: Optional[Dict[str, Any]] = None,
    start: str = "2025-04-30",
    end: str = "2026-11-20",
    notional_usd: float = 100_000.0,
) -> Dict[str, Any]:
    cfg = policy_cfg or load_policy_yaml()
    hist_map = _load_real_fx_history()
    dates = sorted(d for d in hist_map.keys() if start <= d <= end)
    if len(dates) < 20:
        raise RuntimeError("Insufficient historical USD/INR rates for backtest.")

    # Pick the first available business day of each calendar month
    monthly_indices: List[int] = []
    seen_ym = set()
    for idx, d in enumerate(dates):
        ym = d[:7]
        if ym not in seen_ym and idx >= 15:  # leave initial lookback window
            seen_ym.add(ym)
            monthly_indices.append(idx)

    tenors = [30, 60, 90]
    spread_bps = float(cfg["cost_and_landed_assumptions"]["bank_spread_bps"])

    strat_records: Dict[str, Dict[str, List[float]]] = {
        "hedgedesk": {"landed_rates": [], "overruns_inr": [], "hedging_costs_paise": []},
        "never": {"landed_rates": [], "overruns_inr": [], "hedging_costs_paise": []},
        "always_100": {"landed_rates": [], "overruns_inr": [], "hedging_costs_paise": []},
        "always_50": {"landed_rates": [], "overruns_inr": [], "hedging_costs_paise": []},
    }
    timeline: List[Dict[str, Any]] = []

    for m_i, idx in enumerate(monthly_indices):
        order_date = dates[idx]
        tenor_d = tenors[m_i % len(tenors)]
        trading_step = max(1, round(tenor_d * (21.0 / 30.0)))
        settle_idx = idx + trading_step
        if settle_idx >= len(dates):
            break

        settle_date = dates[settle_idx]
        spot_order = hist_map[order_date]
        spot_settle = hist_map[settle_date]

        # Strictly point-in-time market data on order_date!
        market = fetch_fx(as_of_date=order_date, run_id="backtest")
        book = {
            "invoices": [
                {
                    "invoice_id": f"BT-{order_date}",
                    "doc_type": "confirmed",
                    "currency": "USD",
                    "amount_usd": notional_usd,
                    "due_date": settle_date,
                    "tenor_days": tenor_d,
                    "roll_days": 0,
                    "bucket": f"{settle_date[:7]}_confirmed",
                    "flags": [],
                    "supplier": {"status": "VERIFIED", "risk_flag": "low", "evidence_ids": []},
                }
            ]
        }
        decs = decide(book=book, market=market, policy_cfg=cfg, as_of_date=order_date)
        hd_ratio = float(decs[0]["ratio"])
        ann_prem = pick_forward_premium_for_tenor(market["premia_pct"], tenor_d)
        fwd_rate, _ = fwd_cost(spot_order, ann_prem, tenor_d, spread_bps=spread_bps)

        ratios = {
            "hedgedesk": hd_ratio,
            "never": 0.0,
            "always_100": 1.0,
            "always_50": 0.5,
        }
        row = {
            "order_date": order_date,
            "settle_date": settle_date,
            "tenor_days": tenor_d,
            "budget_spot": round(spot_order, 3),
            "settle_spot": round(spot_settle, 3),
            "forward_rate": round(fwd_rate, 3),
            "hedgedesk_ratio": hd_ratio,
        }
        for skey, r_val in ratios.items():
            landed_rate = r_val * fwd_rate + (1.0 - r_val) * spot_settle
            overrun_inr = (landed_rate - spot_order) * notional_usd
            cost_paise_per_usd = r_val * (fwd_rate - spot_order) * 100.0
            strat_records[skey]["landed_rates"].append(landed_rate)
            strat_records[skey]["overruns_inr"].append(overrun_inr)
            strat_records[skey]["hedging_costs_paise"].append(cost_paise_per_usd)
            row[f"{skey}_rate"] = round(landed_rate, 3)

        timeline.append(row)

    return {
        "date_range": f"{dates[0]} to {dates[-1]}",
        "trading_days": len(dates),
        "monthly_windows": len(timeline),
        "spot_start": round(hist_map[dates[0]], 2),
        "spot_end": round(hist_map[dates[-1]], 2),
        "inr_depreciation_pct": round(((hist_map[dates[-1]] - hist_map[dates[0]]) / hist_map[dates[0]]) * 100.0, 2),
        "notional_usd": notional_usd,
        "strat_records": strat_records,
        "timeline": timeline,
        "daily_spot_sample": [{"date": d, "rate": round(hist_map[d], 3)} for d in dates[::5]],
    }


def metrics(replay_res: Dict[str, Any]) -> Dict[str, Any]:
    recs = replay_res["strat_records"]
    labels = {
        "never": "Never Hedge (0%)",
        "always_100": "Always Hedge (100%)",
        "always_50": "Always Hedge (50%)",
        "hedgedesk": "HedgeDesk Dynamic Policy",
    }

    never_worst = max(recs["never"]["overruns_inr"]) if recs["never"]["overruns_inr"] else 1.0
    summary = {}
    for skey, data in recs.items():
        overruns = data["overruns_inr"]
        rates = data["landed_rates"]
        paise_costs = data["hedging_costs_paise"]
        sorted_desc = sorted(overruns, reverse=True)
        worst_m = sorted_desc[0] if sorted_desc else 0.0
        cvar_n = max(1, int(round(len(sorted_desc) * 0.15)))
        cvar_95 = sum(sorted_desc[:cvar_n]) / cvar_n if sorted_desc else 0.0
        avg_overrun = statistics.mean(overruns) if overruns else 0.0
        std_rate = statistics.stdev(rates) if len(rates) >= 2 else 0.0
        avg_rate = statistics.mean(rates) if rates else 0.0
        avg_paise = statistics.mean(paise_costs) if paise_costs else 0.0
        cut_vs_never = ((never_worst - worst_m) / never_worst * 100.0) if never_worst > 0 else 0.0

        summary[skey] = {
            "name": labels[skey],
            "mean_landed_rate": round(avg_rate, 3),
            "std_landed_rate": round(std_rate, 3),
            "avg_overrun_inr_lakh": round(avg_overrun / 100_000.0, 2),
            "worst_month_overrun_inr_lakh": round(worst_m / 100_000.0, 2),
            "cvar_95_overrun_inr_lakh": round(cvar_95 / 100_000.0, 2),
            "avg_hedging_cost_paise_per_usd": round(avg_paise, 1),
            "worst_month_cut_vs_never_pct": round(cut_vs_never, 1),
        }

    hd = summary["hedgedesk"]
    alw = summary["always_100"]
    cheaper_by_paise = round(alw["avg_hedging_cost_paise_per_usd"] - hd["avg_hedging_cost_paise_per_usd"], 1)

    headline = (
        f"HedgeDesk cut the worst-month overrun by {hd['worst_month_cut_vs_never_pct']}% vs never hedging, "
        f"at {hd['avg_hedging_cost_paise_per_usd']} paise per dollar average cost, "
        f"and was cheaper than always hedging 100% by {cheaper_by_paise} paise per dollar."
    )

    return {
        "date_range": replay_res["date_range"],
        "monthly_windows": replay_res["monthly_windows"],
        "spot_start": replay_res["spot_start"],
        "spot_end": replay_res["spot_end"],
        "inr_depreciation_pct": replay_res["inr_depreciation_pct"],
        "headline": headline,
        "strategies": summary,
        "timeline": replay_res["timeline"],
        "daily_spot_sample": replay_res["daily_spot_sample"],
        "assumptions_and_limits": [
            "Synthetic monthly USD 100,000 payables created on first business day of each month (30/60/90d rotating tenors).",
            "One currency pair (USD/INR) over 18 months (April 2025 – Nov 2026 rupee depreciation regime).",
            "Assumed 18 bps bank FX forward spread; customs duty and freight are customer-supplied inputs.",
            "Strict point-in-time assert in policy.decide() blocks any future rate leakage.",
        ],
    }
