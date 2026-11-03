"""고객사/제품/라이선스 마스터 데이터와 만료 상태 계산, 알림 발송 로직 검증."""

from datetime import date, timedelta

import pytest

import email_notify
from claims_store import ClaimsStore, LICENSE_EXPIRED, LICENSE_EXPIRING, LICENSE_SUBSCRIBED


@pytest.fixture
def store(tmp_path):
    from claim_logger import ClaimLogger

    return ClaimsStore(base_dir=tmp_path / "data", logger=ClaimLogger(base_dir=tmp_path / "logs"))


def _iso(days_from_today: int) -> str:
    return (date.today() + timedelta(days=days_from_today)).isoformat()


def test_create_customer_product_license(store):
    customer = store.create_customer("가나전자", "02-000-0000")
    product = store.create_product(customer["id"], "냉장고 K100")
    lic = store.create_license(product["id"], "Standard", _iso(-10), _iso(300), "ops@company.com")

    assert lic["customer_name"] == "가나전자"
    assert lic["product_name"] == "냉장고 K100"
    assert lic["status"] == LICENSE_SUBSCRIBED


def test_license_status_expiring_soon(store):
    customer = store.create_customer("다라산업")
    product = store.create_product(customer["id"], "보일러 B200")
    lic = store.create_license(product["id"], "", _iso(-300), _iso(10), "")
    assert lic["status"] == LICENSE_EXPIRING
    assert lic["days_left"] == 10


def test_license_status_expired(store):
    customer = store.create_customer("사아전자")
    product = store.create_product(customer["id"], "인버터 X1")
    lic = store.create_license(product["id"], "", _iso(-400), _iso(-5), "")
    assert lic["status"] == LICENSE_EXPIRED
    assert lic["days_left"] == -5


def test_create_product_rejects_unknown_customer(store):
    with pytest.raises(ValueError):
        store.create_product(9999, "존재안함")


def test_create_license_rejects_unknown_product(store):
    with pytest.raises(ValueError):
        store.create_license(9999, "", _iso(0), _iso(30), "")


def test_list_expiring_licenses_sorted_soonest_first_excludes_healthy(store):
    customer = store.create_customer("정상건")
    product = store.create_product(customer["id"], "모델A")
    store.create_license(product["id"], "", _iso(-10), _iso(300), "")  # 구독중 - 제외되어야 함
    far = store.create_license(product["id"], "", _iso(-10), _iso(25), "")  # 만료임박
    near = store.create_license(product["id"], "", _iso(-10), _iso(3), "")  # 만료임박, 더 급함
    expired = store.create_license(product["id"], "", _iso(-400), _iso(-1), "")  # 만료

    expiring = store.list_expiring_licenses()
    ids_in_order = [lic["id"] for lic in expiring]
    assert ids_in_order == [expired["id"], near["id"], far["id"]]


def test_mark_license_notified_updates_timestamp(store):
    customer = store.create_customer("고객")
    product = store.create_product(customer["id"], "제품")
    lic = store.create_license(product["id"], "", _iso(-10), _iso(5), "a@b.com")
    assert lic["last_notified_at"] == ""

    store.mark_license_notified(lic["id"])
    refreshed = store.list_licenses()[0]
    assert refreshed["last_notified_at"] != ""


def test_send_email_without_smtp_host_returns_false(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    assert email_notify.send_email(["a@b.com"], "제목", "본문") is False


def test_send_email_with_no_recipients_returns_false():
    assert email_notify.send_email([], "제목", "본문") is False


def test_check_and_notify_skips_license_without_recipients(store, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    customer = store.create_customer("고객")
    product = store.create_product(customer["id"], "제품")
    store.create_license(product["id"], "", _iso(-10), _iso(5), notify_emails="")

    result = email_notify.check_and_notify(store)
    assert result["checked"] == 1
    assert result["notified"] == 0
    assert result["skipped_no_recipient"] == 1


def test_check_and_notify_sends_once_per_day(store, monkeypatch):
    sent = []
    monkeypatch.setattr(email_notify, "send_email", lambda to, subject, body: sent.append((to, subject)) or True)

    customer = store.create_customer("고객")
    product = store.create_product(customer["id"], "제품")
    store.create_license(product["id"], "", _iso(-10), _iso(5), notify_emails="ops@company.com")

    result1 = email_notify.check_and_notify(store)
    assert result1["notified"] == 1
    assert len(sent) == 1

    # 같은 날 다시 점검해도 중복 발송하지 않는다
    result2 = email_notify.check_and_notify(store)
    assert result2["notified"] == 0
    assert len(sent) == 1
