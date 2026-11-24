from datetime import date
from pathlib import Path

import pytest

import bulk_import
import seed_claims
from claim_logger import ClaimLogger
from claims_store import ClaimsStore

SAMPLE_CSV = Path(__file__).resolve().parent.parent / "sample_data" / "test_claims.csv"


@pytest.fixture
def store(tmp_path):
    return ClaimsStore(base_dir=tmp_path / "data", logger=ClaimLogger(base_dir=tmp_path / "logs"))


def _make_licenses(store):
    customer = store.create_customer("테스트사", "02-000-0000", "t@example.com")
    product = store.create_product(customer["id"], "VMware")
    for i, (start, end) in enumerate([("2026-01-01", "2030-01-01"), ("2025-01-01", "2026-12-01"), ("2024-01-01", "2025-01-01")]):
        store.create_license(product["id"], "Standard", start, end, license_key=f"KEY-{i}")
    return customer, product


def test_specs_are_deterministic_and_status_aware():
    lic = [{"customer": "A", "product": "P", "license_key": "K", "end_date": "2020-01-01"}]
    a = seed_claims.build_claim_specs(lic * 20, seed=1, today=date(2026, 1, 1))
    b = seed_claims.build_claim_specs(lic * 20, seed=1, today=date(2026, 1, 1))
    assert a == b and a
    assert all(s["description"].startswith(seed_claims.TEST_MARK) and "K" in s["description"] for s in a)


def test_seed_into_store_links_claims_to_licenses(store):
    customer, product = _make_licenses(store)
    created = seed_claims.seed_into_store(store, seed=3)
    claims = store.list_claims()
    assert created == len(claims) > 0
    assert all(c["customer_id"] == customer["id"] and c["product_id"] == product["id"] and c["license_id"] for c in claims)
    assert {c["status"] for c in claims} <= {"접수", "처리중", "완료"}


def test_seed_into_store_is_idempotent(store):
    _make_licenses(store)
    seed_claims.seed_into_store(store, seed=3)
    n = len(store.list_claims())
    assert seed_claims.seed_into_store(store, seed=3) == 0
    assert len(store.list_claims()) == n


def test_sample_csv_imports_without_failures():
    ok, duplicates, failed = bulk_import.import_rows("test_claims.csv", SAMPLE_CSV.read_bytes())
    assert ok and not failed and not duplicates
    assert all(r["description"].startswith(seed_claims.TEST_MARK) for r in ok)
