import pytest

import seed_demo
from claim_logger import ClaimLogger
from claims_store import ClaimsStore


@pytest.fixture
def store(tmp_path):
    return ClaimsStore(base_dir=tmp_path / "data", logger=ClaimLogger(base_dir=tmp_path / "logs"))


def test_seed_creates_customers_licenses_and_claims(store):
    seed_demo.seed(store=store)
    assert len(store.list_customers()) == len(seed_demo.CUSTOMERS)
    assert len(store.list_claims()) == 30


def test_seed_is_idempotent_without_reset(store):
    seed_demo.seed(store=store)
    seed_demo.seed(store=store)
    assert len(store.list_customers()) == len(seed_demo.CUSTOMERS)
    assert len(store.list_claims()) == 30


def test_seed_reset_recreates_customers(store):
    seed_demo.seed(store=store)
    first_ids = {c["id"] for c in store.list_customers()}
    seed_demo.seed(reset=True, store=store)
    second_ids = {c["id"] for c in store.list_customers()}
    assert len(second_ids) == len(seed_demo.CUSTOMERS) and first_ids.isdisjoint(second_ids)
