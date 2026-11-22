import hashlib
import json
import time
from typing import Any, Dict, List, Optional
import httpx

from .config import CRUSTDATA_API_KEY, CRUSTDATA_API_VERSION, CRUSTDATA_BASE_URL, DEFAULT_POLICY
from .db import StateDB, utc_now_iso


class BudgetExceededError(RuntimeError):
    """Raised when a run hits its credit ceiling."""


class CrustdataClient:
    """Rate-limit-safe, SQLite-cached Crustdata API v2025-11-01 client with live cost ledger."""

    def __init__(self, db: StateDB, api_key: str = CRUSTDATA_API_KEY, usd_to_inr: float = 92.68):
        self.db = db
        self.api_key = api_key or CRUSTDATA_API_KEY
        self.usd_to_inr = usd_to_inr
        self._last_call_ts: Dict[str, float] = {}

    def _headers(self) -> Dict[str, str]:
        return {
            "authorization": f"Bearer {self.api_key}",
            "x-api-version": CRUSTDATA_API_VERSION,
            "content-type": "application/json",
        }

    def _cache_key(self, endpoint: str, body: dict) -> str:
        raw = f"{endpoint}:{json.dumps(body, sort_keys=True)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _check_budget(self, run_id: str, budget_limit_credits: float, est_cost: float):
        if not run_id:
            return
        costs = self.db.get_run_costs(run_id)
        if costs["total_credits_billed"] + est_cost > budget_limit_credits:
            raise BudgetExceededError(
                f"Run {run_id} credit ceiling ({budget_limit_credits:.2f} credits) reached "
                f"(used {costs['total_credits_billed']:.2f}, next call est {est_cost:.2f})."
            )

    def _pace_endpoint(self, endpoint: str, min_interval: float = 0.35):
        last = self._last_call_ts.get(endpoint, 0.0)
        delta = time.time() - last
        if delta < min_interval:
            time.sleep(min_interval - delta)
        self._last_call_ts[endpoint] = time.time()

    def post(
        self,
        endpoint: str,
        body: dict,
        run_id: str = "",
        step_name: str = "crustdata_call",
        budget_limit_credits: float = 25.0,
        est_credits: float = 1.0,
        use_cache: bool = True,
    ) -> Dict[str, Any]:
        ck = self._cache_key(endpoint, body)
        if use_cache:
            cached = self.db.get_cached_api(ck)
            if cached:
                self.db.log_cost(
                    run_id=run_id or "standalone",
                    step_name=step_name,
                    provider="Crustdata (Cache)",
                    endpoint=endpoint,
                    request_id=cached["request_id"] or "cached",
                    credits_used=0.0,
                    cost_inr=0.0,
                    cache_hit=True,
                    note=f"Cache hit (orig credits={cached['credits_used']})",
                )
                return {
                    "data": cached["response"],
                    "request_id": cached["request_id"],
                    "credits_used": 0.0,
                    "orig_credits": cached["credits_used"],
                    "fetched_at": cached["fetched_at"],
                    "cache_hit": True,
                    "source_url": f"{CRUSTDATA_BASE_URL}{endpoint}",
                }

        self._check_budget(run_id, budget_limit_credits, est_credits)

        url = f"{CRUSTDATA_BASE_URL}{endpoint}"
        backoff = 3.0
        for attempt in range(4):
            self._pace_endpoint(endpoint)
            try:
                with httpx.Client(timeout=30.0) as client:
                    resp = client.post(url, headers=self._headers(), json=body)
                if resp.status_code == 429:
                    reset_s = float(resp.headers.get("x-ratelimit-reset", backoff))
                    time.sleep(min(max(reset_s, backoff), 15.0))
                    backoff = min(backoff * 2, 16.0)
                    continue
                if resp.status_code >= 400:
                    return {
                        "error": f"HTTP {resp.status_code}: {resp.text[:350]}",
                        "status_code": resp.status_code,
                        "data": None,
                        "request_id": resp.headers.get("x-request-id", ""),
                        "credits_used": 0.0,
                        "fetched_at": utc_now_iso(),
                        "cache_hit": False,
                        "source_url": url,
                    }
                data = resp.json()
                credits = float(resp.headers.get("x-credits-used", est_credits) or 0.0)
                req_id = resp.headers.get("x-request-id", "")
                fetched_at = utc_now_iso()
                cost_inr = credits * DEFAULT_POLICY.usd_per_crustdata_credit * self.usd_to_inr

                self.db.set_cached_api(
                    cache_key=ck,
                    endpoint=endpoint,
                    request_data=body,
                    response_data=data,
                    headers_data=dict(resp.headers),
                    credits_used=credits,
                    request_id=req_id,
                )
                self.db.log_cost(
                    run_id=run_id or "standalone",
                    step_name=step_name,
                    provider="Crustdata API",
                    endpoint=endpoint,
                    request_id=req_id,
                    credits_used=credits,
                    cost_inr=cost_inr,
                    cache_hit=False,
                    note=f"Live API call ({credits:.2f} cr)",
                )
                return {
                    "data": data,
                    "request_id": req_id,
                    "credits_used": credits,
                    "orig_credits": credits,
                    "fetched_at": fetched_at,
                    "cache_hit": False,
                    "source_url": url,
                }
            except httpx.HTTPError as exc:
                if attempt == 3:
                    return {
                        "error": f"Network error: {exc}",
                        "data": None,
                        "request_id": "",
                        "credits_used": 0.0,
                        "fetched_at": utc_now_iso(),
                        "cache_hit": False,
                        "source_url": url,
                    }
                time.sleep(backoff)
        return {"error": "Rate limit retries exhausted", "data": None, "credits_used": 0.0, "fetched_at": utc_now_iso()}

    def get_credits_balance(self) -> Dict[str, Any]:
        url = f"{CRUSTDATA_BASE_URL}/account/credits"
        try:
            with httpx.Client(timeout=12.0) as client:
                r = client.get(url, headers=self._headers())
            if r.status_code == 200:
                return r.json()
        except Exception as exc:
            return {"error": str(exc)}
        return {"error": "Failed to fetch credits"}

    def verify_supplier(
        self,
        supplier_name: str,
        supplier_domain: Optional[str],
        supplier_country: str,
        run_id: str,
        budget_limit_credits: float,
    ) -> Dict[str, Any]:
        """
        Verify a foreign supplier via Crustdata /company/identify + /company/enrich.
        Detects exact matches, lookalikes/mismatches, low-headcount shells, and unverifiable entities.
        """
        ident_body: Dict[str, Any] = {"count": 3}
        if supplier_domain:
            ident_body["domains"] = [supplier_domain]
        else:
            ident_body["names"] = [supplier_name]

        ident_res = self.post(
            "/company/identify",
            ident_body,
            run_id=run_id,
            step_name="verify_supplier_identify",
            budget_limit_credits=budget_limit_credits,
            est_credits=0.2,
        )
        raw_list = ident_res.get("data")
        if not isinstance(raw_list, list) or not raw_list:
            return {
                "status": "CANNOT_VERIFY",
                "reason": "Crustdata /company/identify returned no records matching domain or legal name.",
                "missing_field": "crustdata_company_id / corporate_registry_record",
                "confidence": 0.0,
                "reliability_score": 0.0,
                "request_id": ident_res.get("request_id", ""),
                "fetched_at": ident_res.get("fetched_at", utc_now_iso()),
                "source_url": ident_res.get("source_url", ""),
            }

        matches = raw_list[0].get("matches") or []
        if not matches:
            return {
                "status": "CANNOT_VERIFY",
                "reason": f"No company entity matched '{supplier_domain or supplier_name}' in Crustdata graph.",
                "missing_field": "verified_company_domain_or_registration",
                "confidence": 0.0,
                "reliability_score": 0.0,
                "request_id": ident_res.get("request_id", ""),
                "fetched_at": ident_res.get("fetched_at", utc_now_iso()),
                "source_url": ident_res.get("source_url", ""),
            }

        top = matches[0]
        conf = float(top.get("confidence_score") or 0.0)
        cdata = top.get("company_data") or {}
        binfo = cdata.get("basic_info") or {}
        cid = cdata.get("crustdata_company_id") or binfo.get("crustdata_company_id")
        matched_name = binfo.get("name") or supplier_name
        matched_domain = binfo.get("primary_domain")

        # Check lookalike trap: name-only match with low confidence or mismatched name tokens
        input_tokens = {t for t in supplier_name.lower().replace(",", "").replace(".", "").split() if len(t) > 2 and t not in {"ltd", "inc", "corp", "co", "pvt", "gmbh", "llc"}}
        matched_tokens = {t for t in matched_name.lower().replace(",", "").replace(".", "").split() if len(t) > 2}
        overlap = len(input_tokens & matched_tokens) / max(1, len(input_tokens))

        if not supplier_domain and (conf < 0.70 or overlap < 0.30):
            return {
                "status": "CANNOT_VERIFY",
                "reason": (
                    f"Cannot verify entity '{supplier_name}': closest fuzzy candidate '{matched_name}' "
                    f"failed verification (confidence={conf:.2f}, token_overlap={overlap:.2f})."
                ),
                "missing_field": "verified_company_domain_or_registration",
                "candidate_name": matched_name,
                "candidate_id": cid,
                "confidence": conf,
                "reliability_score": 0.0,
                "request_id": ident_res.get("request_id", ""),
                "fetched_at": ident_res.get("fetched_at", utc_now_iso()),
                "source_url": ident_res.get("source_url", ""),
            }

        if not supplier_domain and (conf < DEFAULT_POLICY.min_identify_confidence or overlap < 0.65):
            return {
                "status": "LOOKALIKE_SUSPECT",
                "reason": (
                    f"Lookalike alert: input '{supplier_name}' only weakly matched '{matched_name}' "
                    f"(confidence={conf:.2f}, token_overlap={overlap:.2f}, domain={matched_domain or 'missing'})."
                ),
                "missing_field": "verified_official_domain / exact_legal_entity_match",
                "candidate_name": matched_name,
                "candidate_id": cid,
                "confidence": conf,
                "reliability_score": 0.2,
                "request_id": ident_res.get("request_id", ""),
                "fetched_at": ident_res.get("fetched_at", utc_now_iso()),
                "source_url": ident_res.get("source_url", ""),
            }

        # Enrich verified candidate to get real headcount, country, operating status, history
        enrich_body: Dict[str, Any] = {
            "fields": ["basic_info", "headcount", "locations", "web_traffic"]
        }
        if matched_domain:
            enrich_body["domains"] = [matched_domain]
        elif cid:
            enrich_body["crustdata_company_ids"] = [int(cid)]
        else:
            enrich_body["names"] = [matched_name]

        enrich_res = self.post(
            "/company/enrich",
            enrich_body,
            run_id=run_id,
            step_name="verify_supplier_enrich",
            budget_limit_credits=budget_limit_credits,
            est_credits=2.0,
        )
        edata_list = enrich_res.get("data")
        enriched_cd = cdata
        if isinstance(edata_list, list) and edata_list:
            ematches = edata_list[0].get("matches") or []
            if ematches:
                enriched_cd = ematches[0].get("company_data") or cdata

        ebasic = enriched_cd.get("basic_info") or binfo
        ehc = enriched_cd.get("headcount") or {}
        eloc = enriched_cd.get("locations") or {}
        hc_total = ehc.get("total")
        timeseries = ehc.get("timeseries") or []
        hc_growth_6m = None
        if len(timeseries) >= 26:
            try:
                now_c = float(timeseries[-1].get("employee_count", 0))
                prev_c = float(timeseries[-26].get("employee_count", 0))
                if prev_c > 0:
                    hc_growth_6m = round(((now_c - prev_c) / prev_c) * 100.0, 1)
            except Exception:
                hc_growth_6m = None

        status_flag = (ebasic.get("status") or "active").lower()
        year_founded = ebasic.get("year_founded")
        country_found = eloc.get("country") or ehc.get("largest_headcount_country") or "Unknown"

        # Shell / unverified scale gate
        if hc_total is not None and hc_total < DEFAULT_POLICY.min_verified_headcount:
            return {
                "status": "LOOKALIKE_SUSPECT",
                "reason": (
                    f"Entity '{matched_name}' ({matched_domain}) has only {hc_total} tracked employees "
                    f"(< policy min {DEFAULT_POLICY.min_verified_headcount}); possible shell/broker."
                ),
                "missing_field": "verified_manufacturing_headcount_and_plant_registry",
                "crustdata_company_id": cid,
                "legal_name": matched_name,
                "primary_domain": matched_domain,
                "headcount": hc_total,
                "country": country_found,
                "year_founded": year_founded,
                "confidence": conf,
                "reliability_score": 0.35,
                "request_id": enrich_res.get("request_id") or ident_res.get("request_id", ""),
                "fetched_at": enrich_res.get("fetched_at") or ident_res.get("fetched_at", utc_now_iso()),
                "source_url": enrich_res.get("source_url") or ident_res.get("source_url", ""),
            }

        # Reliability score (0.0 to 1.0) combining confidence, scale, age, active status
        rel = 0.65
        if conf >= 0.95:
            rel += 0.15
        if hc_total and hc_total >= 100:
            rel += 0.10
        if year_founded and int(year_founded) <= 2020:
            rel += 0.05
        if hc_growth_6m is not None and hc_growth_6m >= -5.0:
            rel += 0.05
        rel = min(1.0, round(rel, 2))

        return {
            "status": "VERIFIED" if status_flag == "active" else "CANNOT_VERIFY",
            "crustdata_company_id": cid,
            "legal_name": ebasic.get("name") or matched_name,
            "primary_domain": matched_domain,
            "website": ebasic.get("website"),
            "company_type": ebasic.get("company_type"),
            "operating_status": status_flag,
            "year_founded": year_founded,
            "headcount": hc_total,
            "headcount_growth_6m_pct": hc_growth_6m,
            "country": country_found,
            "hq_location": eloc.get("headquarters") or eloc.get("city") or country_found,
            "linkedin_url": ebasic.get("professional_network_url"),
            "description": (ebasic.get("description") or "")[:280],
            "confidence": conf,
            "reliability_score": rel,
            "request_id": enrich_res.get("request_id") or ident_res.get("request_id", ""),
            "fetched_at": enrich_res.get("fetched_at") or ident_res.get("fetched_at", utc_now_iso()),
            "source_url": enrich_res.get("source_url") or ident_res.get("source_url", ""),
        }

    def find_indian_manufacturers(
        self,
        part_query: str,
        run_id: str,
        budget_limit_credits: float,
        limit: int = 5,
    ) -> Dict[str, Any]:
        """Search verified Indian manufacturers (>= 50 employees) using Crustdata /company/search."""
        body = {
            "search": {"query": f"{part_query} manufacturer India", "mode": "hybrid"},
            "filters": {
                "op": "and",
                "conditions": [
                    {"field": "locations.country", "type": "in", "value": ["India"]},
                    {"field": "headcount.total", "type": "=>", "value": 50},
                ],
            },
            "limit": limit,
            "fields": ["crustdata_company_id", "basic_info", "headcount", "locations"],
        }
        res = self.post(
            "/company/search",
            body,
            run_id=run_id,
            step_name="domestic_manufacturer_search",
            budget_limit_credits=budget_limit_credits,
            est_credits=0.03 * limit,
        )
        raw_companies = (res.get("data") or {}).get("companies") or []
        makers = []
        for comp in raw_companies:
            bi = comp.get("basic_info") or {}
            hc = comp.get("headcount") or {}
            loc = comp.get("locations") or {}
            desc = (bi.get("description") or "").lower()
            # Filter out expo/event websites or pure agencies
            is_event_site = any(w in desc for w in ["trade show", "exhibition", "expo ", "conference"])
            if is_event_site:
                continue
            makers.append({
                "crustdata_company_id": comp.get("crustdata_company_id") or bi.get("crustdata_company_id"),
                "name": bi.get("name"),
                "primary_domain": bi.get("primary_domain"),
                "website": bi.get("website"),
                "linkedin_url": bi.get("professional_network_url"),
                "headcount": hc.get("total"),
                "company_type": bi.get("company_type"),
                "hq": loc.get("headquarters") or loc.get("city") or "India",
                "description": (bi.get("description") or "")[:240],
                "price_status": "CANNOT_VERIFY (Private B2B catalogue price — RFQ required)",
                "request_id": res.get("request_id", ""),
                "fetched_at": res.get("fetched_at", utc_now_iso()),
                "source_url": res.get("source_url", ""),
            })
        return {
            "makers": makers,
            "request_id": res.get("request_id", ""),
            "fetched_at": res.get("fetched_at", utc_now_iso()),
            "error": res.get("error"),
        }

    def search_live_web(
        self,
        query: str,
        run_id: str,
        budget_limit_credits: float,
        num_results: int = 3,
    ) -> Dict[str, Any]:
        return self.post(
            "/web/search/live",
            {"query": query, "num_results": num_results},
            run_id=run_id,
            step_name="web_search_live",
            budget_limit_credits=budget_limit_credits,
            est_credits=1.0,
        )
