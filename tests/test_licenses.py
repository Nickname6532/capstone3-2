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
    monkeypatch.setattr(email_notify, "send_email", lambda to, subject, body, html_body=None: sent.append((to, subject)) or True)

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


def test_check_and_notify_passes_html_body(store, monkeypatch):
    captured = {}

    def fake_send(to, subject, body, html_body=None):
        captured["body"], captured["html_body"] = body, html_body
        return True

    monkeypatch.setattr(email_notify, "send_email", fake_send)
    customer = store.create_customer("고객사A")
    product = store.create_product(customer["id"], "제품A")
    store.create_license(product["id"], "Pro", _iso(-10), _iso(5), notify_emails="ops@company.com")

    email_notify.check_and_notify(store)
    assert "고객사A" in captured["html_body"] and "제품A" in captured["html_body"]
    assert "고객사A" in captured["body"]


def test_license_email_html_escapes_customer_name():
    lic = {"customer_name": "<script>alert(1)</script>", "product_name": "Pro 플랜", "plan_name": "",
           "status": "만료임박", "days_left": 3, "start_date": "2026-01-01", "end_date": "2026-11-20"}
    out = email_notify._license_email_html(lic)
    assert "<script>" not in out and "&lt;script&gt;" in out


def test_license_email_html_shows_due_date_and_status():
    lic = {"customer_name": "넷스퀘어", "product_name": "Pro 플랜", "plan_name": "Pro",
           "status": "만료", "days_left": -3, "start_date": "2026-01-01", "end_date": "2026-11-07"}
    out = email_notify._license_email_html(lic)
    assert "넷스퀘어" in out and "3일 전에 이미 만료되었습니다" in out and "라이선스 만료 안내" in out


def test_send_email_builds_multipart_with_html(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.test.local")
    monkeypatch.setenv("SMTP_FROM", "alerts@company.com")
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=10): pass
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def starttls(self, context=None): pass
        def sendmail(self, from_addr, to_addrs, msg_str): sent.append((from_addr, to_addrs, msg_str))

    monkeypatch.setattr(email_notify.smtplib, "SMTP", FakeSMTP)
    assert email_notify.send_email(["a@b.com"], "제목", "텍스트 버전 본문", html_body="<b>HTML 버전 본문</b>") is True
    import email as email_pkg

    parsed = email_pkg.message_from_string(sent[0][2])
    parts = {p.get_content_type(): p.get_payload(decode=True).decode("utf-8") for p in parsed.walk() if not p.is_multipart()}
    assert parts["text/plain"] == "텍스트 버전 본문" and parts["text/html"] == "<b>HTML 버전 본문</b>"


def test_check_and_notify_logs_and_respects_target_option(store, monkeypatch):
    sent = []
    monkeypatch.setattr(email_notify, "send_email", lambda to, subject, body, html_body=None: sent.append(to) or True)
    customer = store.create_customer("고객")
    product = store.create_product(customer["id"], "제품")
    lic = store.create_license(
        product["id"], "", _iso(-10), _iso(5),
        notify_staff_emails="staff@a.com", notify_customer_emails="cust@a.com", notify_target_option="staff",
    )
    email_notify.check_and_notify(store)
    assert sent == [["staff@a.com"]]
    logs = store.list_license_notification_logs(lic["id"])
    assert len(logs) == 1 and logs[0]["success"] == 1 and logs[0]["recipients"] == "staff@a.com"


# ---------------------------------------------------------------- CRUD / 알림 설정

def test_update_and_delete_customer(store):
    customer = store.create_customer("가나전자", "02-000-0000")
    updated = store.update_customer(customer["id"], {"email": "ops@gana.com", "memo": "VIP"})
    assert updated["email"] == "ops@gana.com" and updated["memo"] == "VIP"
    store.delete_customer(customer["id"])
    assert store.get_customer(customer["id"]) is None


def test_delete_customer_with_products_rejected(store):
    customer = store.create_customer("가나전자")
    store.create_product(customer["id"], "냉장고")
    with pytest.raises(ValueError):
        store.delete_customer(customer["id"])


def test_update_and_delete_product(store):
    customer = store.create_customer("가나전자")
    product = store.create_product(customer["id"], "냉장고")
    assert store.update_product(product["id"], "냉장고 K200")["name"] == "냉장고 K200"
    store.delete_product(product["id"])
    assert store.get_product(product["id"]) is None


def test_delete_product_with_license_rejected(store):
    customer = store.create_customer("가나전자")
    product = store.create_product(customer["id"], "냉장고")
    store.create_license(product["id"], "", _iso(-10), _iso(10), "")
    with pytest.raises(ValueError):
        store.delete_product(product["id"])


def test_update_and_delete_license(store):
    customer = store.create_customer("가나전자")
    product = store.create_product(customer["id"], "냉장고")
    lic = store.create_license(product["id"], "Standard", _iso(-10), _iso(10), "ops@company.com")
    updated = store.update_license(lic["id"], {"warning_days": 7, "notify_target_option": "staff"})
    assert updated["warning_days"] == 7 and updated["notify_target_option"] == "staff"
    store.delete_license(lic["id"])
    assert store.list_licenses(customer_id=customer["id"]) == []


def test_license_update_rejects_unknown_notify_target(store):
    customer = store.create_customer("고객")
    product = store.create_product(customer["id"], "제품")
    lic = store.create_license(product["id"], "", _iso(-10), _iso(10), "")
    with pytest.raises(ValueError):
        store.update_license(lic["id"], {"notify_target_option": "everyone"})


def test_per_license_warning_days_overrides_global_default(store):
    customer = store.create_customer("고객")
    product = store.create_product(customer["id"], "제품")
    strict = store.create_license(product["id"], "", _iso(-10), _iso(20), "", warning_days=7)
    loose = store.create_license(product["id"], "", _iso(-10), _iso(20), "", warning_days=60)
    refreshed = {lic["id"]: lic for lic in store.list_licenses(customer_id=customer["id"])}
    assert refreshed[strict["id"]]["status"] == LICENSE_SUBSCRIBED
    assert refreshed[loose["id"]]["status"] == LICENSE_EXPIRING


def test_notification_recipients_by_target_option():
    base = {"notify_staff_emails": "staff@a.com", "notify_customer_emails": "cust@a.com", "notify_emails": ""}
    assert ClaimsStore.notification_recipients({**base, "notify_target_option": "staff"}) == ["staff@a.com"]
    assert ClaimsStore.notification_recipients({**base, "notify_target_option": "customer"}) == ["cust@a.com"]
    assert set(ClaimsStore.notification_recipients({**base, "notify_target_option": "both"})) == {"staff@a.com", "cust@a.com"}
    assert ClaimsStore.notification_recipients({**base, "notify_target_option": "none"}) == []


def test_notification_recipients_falls_back_to_legacy_notify_emails():
    row = {"notify_staff_emails": "", "notify_customer_emails": "", "notify_emails": "legacy@a.com", "notify_target_option": "both"}
    assert ClaimsStore.notification_recipients(row) == ["legacy@a.com"]


def test_get_customer_overview(store):
    customer = store.create_customer("가나전자", email="ops@gana.com")
    product = store.create_product(customer["id"], "냉장고")
    store.create_license(product["id"], "Standard", _iso(-10), _iso(10), "ops@company.com")
    store.create_claim({"customer": "가나전자", "product": "냉장고", "description": "문제", "customer_id": customer["id"]})
    overview = store.get_customer_overview(customer["id"])
    assert overview["customer"]["email"] == "ops@gana.com"
    assert len(overview["products"]) == 1 and len(overview["licenses"]) == 1 and overview["claim_count"] == 1


def test_find_license_by_key(store):
    customer = store.create_customer("고객")
    product = store.create_product(customer["id"], "VMware")
    store.create_license(product["id"], "1년", _iso(-10), _iso(300), license_key="ABC-123")
    assert store.find_license_by_key(product["id"], "ABC-123") is not None
    assert store.find_license_by_key(product["id"], "ZZZ") is None
    assert store.find_license_by_key(product["id"], "") is None
