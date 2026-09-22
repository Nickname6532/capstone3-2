const claimsBody = document.getElementById("claims-body");
const statsEl = document.getElementById("stats");
const form = document.getElementById("claim-form");
const filterStatus = document.getElementById("filter-status");
const filterChannel = document.getElementById("filter-channel");
const filterQ = document.getElementById("filter-q");

async function loadStats() {
  const res = await fetch("/api/stats/summary");
  const s = await res.json();
  const channelLine = Object.entries(s.channel_breakdown || {})
    .map(([ch, n]) => `${ch} ${n}`)
    .join(" · ");
  statsEl.innerHTML = `
    <div class="stat-card"><div class="label">접수</div><div class="value">${s.received}</div></div>
    <div class="stat-card"><div class="label">처리중</div><div class="value">${s.in_progress}</div></div>
    <div class="stat-card"><div class="label">완료</div><div class="value">${s.done}</div></div>
    <div class="stat-card"><div class="label">이번달 처리건수</div><div class="value">${s.this_month_count}</div></div>
    <div class="stat-card"><div class="label">이번달 LLM 비용</div><div class="value">${s.this_month_cost_krw}원</div></div>
    <div class="stat-card"><div class="label">채널별 누적</div><div class="value" style="font-size:13px; font-weight:500;">${channelLine || "-"}</div></div>
  `;
}

function statusOptions(current) {
  const next = { "접수": ["처리중", "완료"], "처리중": ["완료", "접수"], "완료": [] }[current] || [];
  const opts = [`<option value="${current}" selected>${current}</option>`];
  for (const s of next) opts.push(`<option value="${s}">${s}</option>`);
  return opts.join("");
}

async function loadClaims() {
  const params = new URLSearchParams();
  if (filterStatus.value) params.set("status", filterStatus.value);
  if (filterChannel.value) params.set("channel", filterChannel.value);
  if (filterQ.value) params.set("q", filterQ.value);
  const res = await fetch("/api/claims?" + params.toString());
  const claims = await res.json();

  claimsBody.innerHTML = claims.map(c => `
    <tr data-id="${c.claim_id}">
      <td>${c.claim_id}</td>
      <td>${c.created_at}</td>
      <td>${escapeHtml(c.channel || "내부입력")}</td>
      <td>${escapeHtml(c.customer)}</td>
      <td>${escapeHtml(c.contact || "-")}</td>
      <td>${escapeHtml(c.product)}</td>
      <td>${escapeHtml(c.category || "-")}</td>
      <td class="desc-cell">${escapeHtml(c.description)}</td>
      <td><input class="assignee-input" value="${escapeHtml(c.assignee || "")}" placeholder="미배정" ${c.status === "완료" ? "disabled" : ""}/></td>
      <td><span class="badge ${c.status}">${c.status}</span></td>
      <td class="actions">
        <select class="status-select" ${c.status === "완료" ? "disabled" : ""}>${statusOptions(c.status)}</select>
        <button class="save-btn" ${c.status === "완료" ? "disabled" : ""}>저장</button>
      </td>
    </tr>
  `).join("");

  claimsBody.querySelectorAll("tr").forEach(tr => {
    const id = tr.dataset.id;
    tr.querySelector(".save-btn")?.addEventListener("click", async () => {
      const status = tr.querySelector(".status-select").value;
      const assignee = tr.querySelector(".assignee-input").value;
      await updateClaim(id, { status, assignee });
    });
  });
}

async function updateClaim(id, updates) {
  const res = await fetch(`/api/claims/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updates),
  });
  if (!res.ok) {
    const err = await res.json();
    alert("업데이트 실패: " + (err.detail || res.status));
    return;
  }
  await Promise.all([loadClaims(), loadStats()]);
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(form);
  const payload = Object.fromEntries(fd.entries());
  payload.amount_krw = Number(payload.amount_krw || 0);

  const res = await fetch("/api/claims", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json();
    alert("접수 실패: " + JSON.stringify(err));
    return;
  }
  form.reset();
  await Promise.all([loadClaims(), loadStats()]);
});

document.getElementById("refresh-btn").addEventListener("click", loadClaims);
filterStatus.addEventListener("change", loadClaims);
filterChannel.addEventListener("change", loadClaims);
filterQ.addEventListener("input", debounce(loadClaims, 300));

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

loadStats();
loadClaims();
