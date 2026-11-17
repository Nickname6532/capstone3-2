"""
라이선스 만료 임박 알림 메일. SMTP_HOST가 설정되어 있지 않으면 실제로 보내지
않고 콘솔에만 남긴다 — 메일 서버 없는 로컬/데모 환경에서도 로직(누구에게,
언제, 몇 건)을 그대로 확인할 수 있게 하기 위함이다.

환경변수:
    SMTP_HOST, SMTP_PORT(기본 587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM
"""

from __future__ import annotations

import html
import os
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Optional

# 상태별 강조색 — 대시보드(style.css)의 신호색(amber/coral/moss)과 맞춘다.
_STATUS_COLORS = {"만료임박": "#c9893a", "만료": "#b8554a", "구독중": "#4f8f6f"}


def send_email(to_addrs: list[str], subject: str, body: str, html_body: Optional[str] = None) -> bool:
    """text(body)는 항상 보내고, html_body가 있으면 text/html 두 파트를 함께 담는다.

    SMTP_HOST가 비어 있으면 실제로 보내지 않고 콘솔에만 남긴다.
    """
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

    if html_body:
        msg: MIMEMultipart | MIMEText = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")
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


def _license_due_text(lic: dict[str, Any]) -> str:
    if lic["status"] == "만료":
        return f"{abs(lic['days_left'])}일 전에 이미 만료되었습니다"
    return f"{lic['days_left']}일 후 만료됩니다"


def _license_email_body(lic: dict[str, Any]) -> str:
    """텍스트 전용 메일 클라이언트를 위한 폴백 본문."""
    return (
        f"{lic['customer_name']}의 '{lic['product_name']}' 라이선스"
        f"({lic.get('plan_name') or '기본 플랜'})가 {_license_due_text(lic)}.\n\n"
        f"시작일: {lic['start_date']}\n"
        f"만료일: {lic['end_date']}\n"
    )


def _license_email_html(lic: dict[str, Any]) -> str:
    """실제 발송되는 HTML 양식. 고객사/제품명은 사용자가 입력한 값이라 메일 클라이언트에서
    그대로 렌더링되는 HTML에 꽂히지 않도록 반드시 escape한다."""
    customer = html.escape(str(lic["customer_name"]))
    product = html.escape(str(lic["product_name"]))
    plan = html.escape(str(lic.get("plan_name") or "기본 플랜"))
    color = _STATUS_COLORS.get(lic["status"], "#888888")
    due = _license_due_text(lic)
    return f"""\
<div style="font-family:-apple-system,'Malgun Gothic',sans-serif;max-width:480px;margin:0 auto;border:1px solid #e3e3e6;border-radius:8px;overflow:hidden;">
  <div style="background:#12161f;color:#f2f2f2;padding:18px 22px;">
    <p style="margin:0;font-size:12.5px;color:#9aa3b2;">클레임 통합관리 시스템</p>
    <h2 style="margin:6px 0 0;font-size:18px;">라이선스 {lic['status']} 안내</h2>
  </div>
  <div style="padding:22px;">
    <p style="margin:0 0 16px;font-size:15px;line-height:1.6;color:#222;">
      <strong>{customer}</strong>의 <strong>{product}</strong> 라이선스({plan})가
      <span style="color:{color};font-weight:700;">{due}</span>.
    </p>
    <table style="width:100%;border-collapse:collapse;font-size:13px;color:#333;">
      <tr><td style="padding:6px 0;color:#888;">시작일</td><td style="padding:6px 0;text-align:right;">{lic['start_date']}</td></tr>
      <tr><td style="padding:6px 0;color:#888;border-top:1px solid #eee;">만료일</td><td style="padding:6px 0;text-align:right;font-weight:700;border-top:1px solid #eee;">{lic['end_date']}</td></tr>
    </table>
  </div>
  <div style="background:#f7f7f8;padding:12px 22px;font-size:12px;color:#999;">
    이 메일은 라이선스 만료 자동 점검에 의해 발송되었습니다. 갱신·확인이 필요하면 담당자에게 문의하세요.
  </div>
</div>
"""


def check_and_notify(store) -> dict[str, Any]:
    """만료임박/만료 라이선스를 찾아 아직 오늘 알림을 못 받은 건에만 메일을 보낸다.

    같은 날 중복 발송을 막기 위해 last_notified_at이 오늘 날짜로 시작하면 건너뛴다.
    수신자는 notify_target_option(담당자/고객/모두/끔)에 따라 store가 계산하고,
    발송 결과는 license_notification_logs에 남긴다.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    checked = 0
    notified = 0
    skipped_no_recipient = 0

    for lic in store.list_expiring_licenses():
        checked += 1
        if (lic.get("last_notified_at") or "").startswith(today):
            continue

        recipients = store.notification_recipients(lic)
        if not recipients:
            skipped_no_recipient += 1
            continue

        subject = f"[라이선스 {lic['status']}] {lic['customer_name']} - {lic['product_name']}"
        ok = send_email(recipients, subject, _license_email_body(lic), html_body=_license_email_html(lic))
        store.log_license_notification(lic["id"], recipients, lic.get("days_left"), ok)
        if ok:
            store.mark_license_notified(lic["id"])
            notified += 1

    return {"checked": checked, "notified": notified, "skipped_no_recipient": skipped_no_recipient}
