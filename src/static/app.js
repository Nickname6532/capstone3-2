const claimsBody = document.getElementById("claims-body");
const statsEl = document.getElementById("stats");
const form = document.getElementById("claim-form");
const filterStatus = document.getElementById("filter-status");
const filterChannel = document.getElementById("filter-channel");
const filterAssignee = document.getElementById("filter-assignee");
const filterContact = document.getElementById("filter-contact");
const filterQ = document.getElementById("filter-q");

attachPhoneAutoFormat(document.getElementById("internal-contact-input"));

async function apiFetch(url, options) {
  const res = await fetch(url, options);
  if (res.status === 401) {
    location.href = "/login";
    throw new Error("로그인이 필요합니다");
  }
  return res;
}

async function loadUser() {
  const res = await apiFetch("/api/auth/me");
  const me = await res.json();
  const adminLink = me.role === "admin" ? ` · <a href="/admin">계정 관리</a>` : "";
  document.getElementById("user-box").innerHTML =
    `${escapeHtml(me.display_name)}님 (${escapeHtml(me.role)})${adminLink} · <a href="#" id="logout-link">로그아웃</a>`;
  document.getElementById("logout-link").addEventListener("click", async (e) => {
    e.preventDefault();
    await fetch("/api/auth/logout", { method: "POST" });
    location.href = "/login";
  });
}

async function loadStats() {
  const res = await apiFetch("/api/stats/summary");
  const s = await res.json();
  const channelLine = Object.entries(s.channel_breakdown || {})
    .map(([ch, n]) => `${ch} ${n}`)
    .join(" · ");
  statsEl.innerHTML = `
    <div class="board-seg"><span class="label"><span class="dot 접수"></span>접수</span><span class="value num">${s.received}</span></div>
    <div class="board-seg"><span class="label"><span class="dot 처리중"></span>처리중</span><span class="value num">${s.in_progress}</span></div>
    <div class="board-seg"><span class="label"><span class="dot 완료"></span>완료</span><span class="value num">${s.done}</span></div>
    <div class="board-seg"><span class="label">이번달 처리</span><span class="value num">${s.this_month_count}</span></div>
    <div class="board-seg"><span class="label">이번달 비용</span><span class="value num">${s.this_month_cost_krw}<span style="font-size:16px;">원</span></span></div>
    <div class="board-seg"><span class="label">채널별 누적</span><span class="value small">${channelLine || "-"}</span></div>
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
  if (filterAssignee.value) params.set("assignee", filterAssignee.value);
  if (filterContact.value) params.set("contact", filterContact.value);
  if (filterQ.value) params.set("q", filterQ.value);
  const res = await apiFetch("/api/claims?" + params.toString());
  const claims = await res.json();

  claimsBody.innerHTML = claims.map(c => `
    <tr data-id="${c.claim_id}" class="s-${c.status}">
      <td class="num">${c.claim_id}</td>
      <td class="num">${c.created_at}</td>
      <td>${escapeHtml(c.channel || "내부입력")}</td>
      <td>${escapeHtml(c.customer)}</td>
      <td class="num">${escapeHtml(c.contact || "-")}</td>
      <td>${escapeHtml(c.product)}</td>
      <td>${escapeHtml(c.category || "-")}</td>
      <td class="desc-cell">${escapeHtml(c.description)}</td>
      <td><input class="assignee-input" value="${escapeHtml(c.assignee || "")}" placeholder="미배정" ${c.status === "완료" ? "disabled" : ""}/></td>
      <td><span class="status-word s-${c.status}">${c.status}</span></td>
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
  const res = await apiFetch(`/api/claims/${id}`, {
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

  const res = await apiFetch("/api/claims", {
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

document.getElementById("import-btn").addEventListener("click", async () => {
  const fileInput = document.getElementById("import-file");
  const resultEl = document.getElementById("import-result");
  const file = fileInput.files[0];
  if (!file) {
    alert("파일을 선택해주세요.");
    return;
  }
  const btn = document.getElementById("import-btn");
  btn.disabled = true;
  btn.textContent = "가져오는 중...";
  resultEl.innerHTML = "";

  const fd = new FormData();
  fd.append("file", file);

  try {
    const res = await apiFetch("/api/claims/import", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) {
      resultEl.innerHTML = `<p style="color: var(--danger);">가져오기 실패: ${escapeHtml(data.detail || res.status)}</p>`;
      return;
    }
    let html = `<p><strong>${data.imported_count}건</strong> 등록됨 / 총 ${data.total_rows}건 중 ` +
      `중복 ${data.duplicate_count}건 제외, 실패 ${data.failed_count}건</p>`;
    if (data.failed_rows && data.failed_rows.length) {
      html += "<p style='font-size:13px; color: var(--danger); margin-bottom:2px;'>실패</p><ul style='font-size:13px; color: var(--muted); margin-top:0;'>" +
        data.failed_rows.map(f => `<li>${f.row ? f.row + "행: " : ""}${escapeHtml(f.reason)}</li>`).join("") +
        "</ul>";
    }
    if (data.duplicate_rows && data.duplicate_rows.length) {
      html += "<p style='font-size:13px; color: var(--warn); margin-bottom:2px;'>중복 (건너뜀)</p><ul style='font-size:13px; color: var(--muted); margin-top:0;'>" +
        data.duplicate_rows.map(d => `<li>${d.row ? d.row + "행: " : ""}${escapeHtml(d.reason)}</li>`).join("") +
        "</ul>";
    }
    if (data.warnings && data.warnings.length) {
      html += "<p style='font-size:13px; color: var(--muted); margin-bottom:2px;'>주의(자동 보정됨)</p><ul style='font-size:13px; color: var(--muted); margin-top:0;'>" +
        data.warnings.map(w => `<li>${w.claim_id}: ${w.warnings.map(escapeHtml).join(", ")}</li>`).join("") +
        "</ul>";
    }
    resultEl.innerHTML = html;
    fileInput.value = "";
    await Promise.all([loadClaims(), loadStats()]);
  } catch (err) {
    resultEl.innerHTML = `<p style="color: var(--danger);">네트워크 오류로 가져오기에 실패했습니다.</p>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "가져오기";
  }
});

document.getElementById("refresh-btn").addEventListener("click", loadClaims);
filterStatus.addEventListener("change", loadClaims);
filterChannel.addEventListener("change", loadClaims);
filterAssignee.addEventListener("input", debounce(loadClaims, 300));
filterContact.addEventListener("input", debounce(loadClaims, 300));
filterQ.addEventListener("input", debounce(loadClaims, 300));

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

loadUser();
loadStats();
loadClaims();
