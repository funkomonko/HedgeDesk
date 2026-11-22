import os
from dataclasses import dataclass, asdict
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


@dataclass
class TreasuryPolicyConfig:
    """Board-approved corporate FX hedging & procurement policy (editable for live fixes)."""
    policy_version: str = "FEMA-CORP-TREASURY-2026.2"
    client_name: str = "BharatFlow 3PL & Cold-Chain Logistics Pvt. Ltd."
    client_gstin_state: str = "Maharashtra / Haryana Hubs"
    base_hedge_ratio: float = 0.60
    min_hedge_ratio: float = 0.00
    max_hedge_ratio: float = 1.00
    # Risk / Cost weights
    high_exposure_usd_threshold: float = 50000.0
    max_annualized_forward_premium_pct: float = 4.25
    volatility_surge_threshold_pct: float = 4.50
    min_verified_headcount: int = 15
    min_identify_confidence: float = 0.82
    # Staleness & Safety gates
    max_rate_age_hours: int = 72
    duplicate_amount_tolerance_pct: float = 0.001
    bank_fx_spread_bps: float = 18.0  # 0.18% corporate forward spread
    # Import landed cost assumptions (explicitly disclosed & editable)
    default_customs_duty_pct: float = 7.5
    default_freight_insurance_pct: float = 3.2
    domestic_logistics_gst_advantage_pct: float = 1.5
    # Unit economics / pricing
    monthly_subscription_inr: float = 24999.0
    manual_advisor_cost_per_cycle_inr: float = 45000.0
    # Cost controls
    usd_per_crustdata_credit: float = float(os.getenv("HEDGEDESK_USD_PER_CREDIT", "0.05"))
    default_run_budget_credits: float = 25.0

    def to_dict(self) -> dict:
        return asdict(self)


CRUSTDATA_API_KEY: str = os.getenv("CRUSTDATA_API_KEY", "").strip()
CRUSTDATA_BASE_URL: str = "https://api.crustdata.com"
CRUSTDATA_API_VERSION: str = "2025-11-01"
DB_PATH: Path = BASE_DIR / os.getenv("HEDGEDESK_DB_PATH", "hedgedesk_state.db")

DEFAULT_POLICY = TreasuryPolicyConfig()
