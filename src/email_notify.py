"""
라이선스 만료 임박 알림 메일. SMTP_HOST가 설정되어 있지 않으면 실제로 보내지
않고 콘솔에만 남긴다 — 메일 서버 없는 로컬/데모 환경에서도 로직(누구에게,
언제, 몇 건)을 그대로 확인할 수 있게 하기 위함이다.

환경변수:
    SMTP_HOST, SMTP_PORT(기본 587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM
"""

from __future__ import annotations

import os
import smtplib
import ssl
from datetime import datetime
from email.mime.text import MIMEText
from typing import Any


def send_email(to_addrs: list[str], subject: str, body: str) -> bool:
    to_addrs = [a.strip() for a in to_addrs if a and a.strip()]
    if not to_addrs:
        return False

    host = os.environ.get("SMTP_HOST")
    if not host:
        print(f"[메일 발송 생략: SMTP_HOST 미설정] to={to_addrs} subject={subject}")
        return False

    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    from_addr = os.environ.get("SMTP_FROM") or user or "no-reply@example.com"

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)

    try:
        with smtplib.SMTP(host, port, timeout=10) as server:
            server.starttls(context=ssl.create_default_context())
            if user and password:
                server.login(user, password)
            server.sendmail(from_addr, to_addrs, msg.as_string())
        return True
    except (smtplib.SMTPException, OSError) as e:
        print(f"[메일 발송 실패] to={to_addrs}: {e}")
        return False


def _license_email_body(lic: dict[str, Any]) -> str:
    if lic["status"] == "만료":
        due = f"{abs(lic['days_left'])}일 전에 이미 만료되었습니다."
    else:
        due = f"{lic['days_left']}일 후 만료됩니다."
    return (
        f"{lic['customer_name']}의 '{lic['product_name']}' 라이선스"
        f"({lic.get('plan_name') or '기본 플랜'})가 {due}\n\n"
        f"시작일: {lic['start_date']}\n"
        f"만료일: {lic['end_date']}\n"
    )


def check_and_notify(store) -> dict[str, Any]:
    """만료임박/만료 라이선스를 찾아 아직 오늘 알림을 못 받은 건에만 메일을 보낸다.

    같은 날 중복 발송을 막기 위해 last_notified_at이 오늘 날짜로 시작하면 건너뛴다.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    checked = 0
    notified = 0
    skipped_no_recipient = 0

    for lic in store.list_expiring_licenses():
        checked += 1
        if (lic.get("last_notified_at") or "").startswith(today):
            continue

        recipients = [e for e in (lic.get("notify_emails") or "").split(",") if e.strip()]
        if not recipients:
            skipped_no_recipient += 1
            continue

        subject = f"[라이선스 {lic['status']}] {lic['customer_name']} - {lic['product_name']}"
        if send_email(recipients, subject, _license_email_body(lic)):
            store.mark_license_notified(lic["id"])
            notified += 1

    return {"checked": checked, "notified": notified, "skipped_no_recipient": skipped_no_recipient}
