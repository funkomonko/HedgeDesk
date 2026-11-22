from typing import Any, Dict

TEST_CASES: Dict[str, Dict[str, Any]] = {
    "standard_cold_chain": {
        "case_id": "standard_cold_chain",
        "case_name": "Standard + Domestic Break-Even: 3PL Cold-Chain & Fleet Expansion Book",
        "description": (
            "Open USD/EUR import payables for BharatFlow 3PL across Carrier (reefer compressors), "
            "Zebra Technologies (warehouse scanners), and Danfoss (cold-room valves). "
            "Verifies foreign suppliers, computes forward hedges, and finds Indian manufacturing alternatives with break-even ₹ unit prices."
        ),
        "client_name": "BharatFlow 3PL & Cold-Chain Logistics Pvt. Ltd.",
        "invoices": [
            {
                "invoice_id": "INV-2026-101",
                "supplier_name": "Carrier Global",
                "supplier_domain": "carrier.com",
                "supplier_country": "USA",
                "currency": "USD",
                "amount": 120000.0,
                "quantity_units": 40,
                "due_date": "2027-01-21",
                "part_description": "Commercial Transport Reefer Compressors (24V DC)",
                "domestic_search_query": "commercial refrigeration HVAC compressor manufacturer",
                "customs_duty_pct": 7.5,
                "freight_insurance_pct": 3.2,
            },
            {
                "invoice_id": "INV-2026-102",
                "supplier_name": "Zebra Technologies",
                "supplier_domain": "zebra.com",
                "supplier_country": "USA",
                "currency": "USD",
                "amount": 48000.0,
                "quantity_units": 120,
                "due_date": "2026-12-22",
                "part_description": "Industrial Rugged RFID Barcode Scanners",
                "domestic_search_query": "industrial automation electronics barcode hardware manufacturer",
                "customs_duty_pct": 10.0,
                "freight_insurance_pct": 2.5,
            },
            {
                "invoice_id": "INV-2026-103",
                "supplier_name": "Danfoss",
                "supplier_domain": "danfoss.com",
                "supplier_country": "Denmark",
                "currency": "EUR",
                "amount": 35000.0,
                "quantity_units": 250,
                "due_date": "2027-02-15",
                "part_description": "Cold-Storage Electronic Expansion Valves",
                "domestic_search_query": "industrial valves cold storage automation manufacturer",
                "customs_duty_pct": 7.5,
                "freight_insurance_pct": 3.0,
            },
        ],
    },
    "hard_duplicate_invoice": {
        "case_id": "hard_duplicate_invoice",
        "case_name": "Hard Case 1: Duplicate Invoice Detection (Prevent Over-Hedging & Double-Pay)",
        "description": (
            "Same supplier, amount ($85,000), and due date submitted twice under two invoice filenames "
            "(INV-2026-884 and INV-2026-884-REISSUE). HedgeDesk must catch the duplicate and hold the second line."
        ),
        "client_name": "BharatFlow 3PL & Cold-Chain Logistics Pvt. Ltd.",
        "invoices": [
            {
                "invoice_id": "INV-2026-884",
                "supplier_name": "Carrier Global",
                "supplier_domain": "carrier.com",
                "supplier_country": "USA",
                "currency": "USD",
                "amount": 85000.0,
                "quantity_units": 25,
                "due_date": "2027-01-15",
                "part_description": "Cold-Chain Telemetry & Compressor Units",
                "domestic_search_query": "commercial refrigeration manufacturer",
            },
            {
                "invoice_id": "INV-2026-884-REISSUE",
                "supplier_name": "Carrier Global Corp.",
                "supplier_domain": "carrier.com",
                "supplier_country": "USA",
                "currency": "USD",
                "amount": 85000.0,
                "quantity_units": 25,
                "due_date": "2027-01-15",
                "part_description": "Cold-Chain Telemetry & Compressor Units (Re-sent PDF)",
                "domestic_search_query": "commercial refrigeration manufacturer",
            },
        ],
    },
    "hard_lookalike_unverifiable": {
        "case_id": "hard_lookalike_unverifiable",
        "case_name": "Hard Case 2: Lookalike Shell Name + Cannot-Verify Ghost Supplier",
        "description": (
            "Tests lookalike entity ('Carrier Global' name-only without official domain resolves to a tiny lookalike courier) "
            "plus an unverifiable offshore shell ('PAYTM*SHARMA IMPORTS / Nova Phantom Holdings Ltd'). "
            "HedgeDesk must NEVER guess or book FEMA forwards on unverified suppliers."
        ),
        "client_name": "BharatFlow 3PL & Cold-Chain Logistics Pvt. Ltd.",
        "invoices": [
            {
                "invoice_id": "INV-2026-901",
                "supplier_name": "Carrier Global",
                "supplier_domain": "",  # Missing official domain triggers name-only lookalike check
                "supplier_country": "Unknown",
                "currency": "USD",
                "amount": 62000.0,
                "quantity_units": 20,
                "due_date": "2027-01-18",
                "part_description": "Reefer Condenser Assembly",
                "domestic_search_query": "commercial refrigeration manufacturer",
            },
            {
                "invoice_id": "INV-2026-902",
                "supplier_name": "Shenzhen Phantom Cold-Chain X99 Trading",
                "supplier_domain": "",
                "supplier_country": "Unknown",
                "currency": "USD",
                "amount": 41000.0,
                "quantity_units": 100,
                "due_date": "2027-01-20",
                "part_description": "Lithium Warehouse Pallet Cells",
                "domestic_search_query": "lithium ion battery manufacturer",
            },
        ],
    },
    "hard_roll_and_stale_fx": {
        "case_id": "hard_roll_and_stale_fx",
        "case_name": "Hard Case 3: Shifted Maturity Date (Forward Roll) + Stale/Down Rate Gate",
        "description": (
            "Shipment delayed after Sept 2026: due date slipped from 2026-12-10 to 2027-01-25 (+46d Forward Roll). "
            "Includes an optional Stale/Down FX toggle demonstrating the 'cannot verify' safety halt."
        ),
        "client_name": "BharatFlow 3PL & Cold-Chain Logistics Pvt. Ltd.",
        "simulate_rate_source_down": False,
        "simulate_stale_rate": False,
        "invoices": [
            {
                "invoice_id": "INV-2026-777",
                "supplier_name": "Honeywell",
                "supplier_domain": "honeywell.com",
                "supplier_country": "USA",
                "currency": "USD",
                "amount": 95000.0,
                "quantity_units": 200,
                "previous_due_date": "2026-12-10",
                "due_date": "2027-01-25",
                "part_description": "Warehouse Cold-Room IoT Temperature Sensors",
                "domestic_search_query": "industrial automation sensor electrical manufacturer",
                "customs_duty_pct": 7.5,
                "freight_insurance_pct": 3.0,
            }
        ],
    },
}
