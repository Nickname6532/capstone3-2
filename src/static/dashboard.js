// 대시보드 전용: 라이선스 만료 임박, 상태 요약 보드, 담당자별 실적.

async function loadLicenseAlerts() {
  const el = document.getElementById("license-alert");
  const body = document.getElementById("license-alert-body");
  const res = await apiFetch("/api/licenses/expiring");
  const licenses = await res.json();

  if (!licenses.length) {
    el.classList.remove("alert-board");
    body.innerHTML = `<p class="alert-empty">만료 임박하거나 만료된 라이선스가 없습니다.</p>`;
    return;
  }

  el.classList.add("alert-board");
  body.innerHTML = licenses.map(lic => {
    const when = lic.status === "만료"
      ? `${Math.abs(lic.days_left)}일 전 만료됨`
      : `${lic.days_left}일 후 만료`;
    return `
      <div class="alert-row">
        <span class="who">${escapeHtml(lic.customer_name)} · ${escapeHtml(lic.product_name)}
          <span class="status-word lic-${lic.status}">${lic.status}</span>
        </span>
        <span class="when num">${escapeHtml(lic.end_date)} (${when})</span>
      </div>
    `;
  }).join("");
}

async function loadAnalytics() {
  const el = document.getElementById("analytics-body");
  const data = await apiFetch("/api/stats/analytics").then(r => r.json());
  if (!data.by_assignee.length) {
    el.innerHTML = `<p class="hint">아직 집계할 클레임이 없습니다.</p>`;
    return;
  }
  const maxDone = Math.max(1, ...data.by_assignee.map(a => a.done));
  el.innerHTML = `
    <div class="analytics-rows">
      ${data.by_assignee.map(a => `
        <div class="analytics-row">
          <span class="analytics-name">${escapeHtml(a.assignee)}</span>
          <span class="analytics-bar-track">
            <span class="analytics-bar" style="width:${(a.done / maxDone) * 100}%"></span>
          </span>
          <span class="analytics-figures num">
            완료 ${a.done} · 완료율 ${a.completion_rate}% · 평균 ${a.avg_processing_minutes}분
          </span>
        </div>
      `).join("")}
    </div>
  `;
}

loadUser();
loadLicenseAlerts();
loadStats();
loadAnalytics();
