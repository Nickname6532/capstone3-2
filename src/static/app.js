const claimsBody = document.getElementById("claims-body");
const form = document.getElementById("claim-form");
const filterStatus = document.getElementById("filter-status");
const filterChannel = document.getElementById("filter-channel");
const filterDifficulty = document.getElementById("filter-difficulty");
const filterAssignee = document.getElementById("filter-assignee");
const filterContact = document.getElementById("filter-contact");
const filterQ = document.getElementById("filter-q");
const filterDateFrom = document.getElementById("filter-date-from");
const filterDateTo = document.getElementById("filter-date-to");

// <input type="datetime-local">은 "YYYY-MM-DDTHH:MM"만 받는다. 예전에 "YYYY-MM-DD HH:MM"으로 저장된 값도 보이게 변환.
function toLocalInput(value) {
  return (value || "").replace(" ", "T").slice(0, 16);
}

// 목록용 짧은 표기: 2026-11-01T09:00 -> 11-01 09:00
function shortWhen(value) {
  const v = (value || "").replace("T", " ");
  return v ? v.slice(5, 16) : "";
}

function issuePeriodText(c) {
  if (!c.issue_started_at && !c.issue_ended_at) return "";
  return `장애 ${shortWhen(c.issue_started_at) || "?"} ~ ${shortWhen(c.issue_ended_at) || "진행중"}`;
}

attachPhoneAutoFormat(document.getElementById("internal-contact-input"));

function statusOptions(current) {
  const next = { "접수": ["처리중", "완료"], "처리중": ["완료", "접수"], "완료": [] }[current] || [];
  const opts = [`<option value="${current}" selected>${current}</option>`];
  for (const s of next) opts.push(`<option value="${s}">${s}</option>`);
  return opts.join("");
}

const DIFFICULTY_LABELS = { low: "경미(Low)", medium: "보통(Medium)", high: "심각(High)" };

function difficultyBadge(difficulty) {
  if (!difficulty) return `<span class="muted">-</span>`;
  return `<span class="status-word diff-${difficulty}">${DIFFICULTY_LABELS[difficulty] || escapeHtml(difficulty)}</span>`;
}

async function loadClaims() {
  const params = new URLSearchParams();
  if (filterStatus.value) params.set("status", filterStatus.value);
  if (filterChannel.value) params.set("channel", filterChannel.value);
  if (filterDifficulty.value) params.set("difficulty", filterDifficulty.value);
  if (filterAssignee.value) params.set("assignee", filterAssignee.value);
  if (filterContact.value) params.set("contact", filterContact.value);
  if (filterQ.value) params.set("q", filterQ.value);
  if (filterDateFrom.value) params.set("date_from", filterDateFrom.value);
  if (filterDateTo.value) params.set("date_to", filterDateTo.value);
  if (filterDateFrom.value && filterDateTo.value && filterDateTo.value < filterDateFrom.value) {
    document.getElementById("claims-count").textContent = "기간의 종료일이 시작일보다 빠릅니다.";
    return;
  }
  const res = await apiFetch("/api/claims?" + params.toString());
  const claims = await res.json();

  claimsBody.innerHTML = claims.map(c => `
    <tr data-id="${c.claim_id}" class="s-${c.status}">
      <td class="num">${c.claim_id}</td>
      <td class="num">${escapeHtml((c.created_at || "").slice(0, 16))}${
        issuePeriodText(c) ? `<br><span class="muted period-line">${escapeHtml(issuePeriodText(c))}</span>` : ""}</td>
      <td>${escapeHtml(c.channel || "내부입력")}</td>
      <td>${escapeHtml(c.customer)}</td>
      <td class="num">${escapeHtml(c.contact || "-")}</td>
      <td>${escapeHtml(c.product)}</td>
      <td class="type-cell">${escapeHtml(c.category || "-")}</td>
      <td class="desc-cell"><div class="desc-clamp" title="${escapeHtml(c.description)}">${escapeHtml(c.description)}</div></td>
      <td><input class="assignee-input" value="${escapeHtml(c.assignee || "")}" placeholder="미배정" ${c.status === "완료" ? "disabled" : ""}/></td>
      <td>${difficultyBadge(c.difficulty)}</td>
      <td><span class="status-word s-${c.status}">${c.status}</span></td>
      <td class="actions">
        <select class="status-select" ${c.status === "완료" ? "disabled" : ""}>${statusOptions(c.status)}</select>
        <button class="save-btn" ${c.status === "완료" ? "disabled" : ""}>저장</button>
        <button type="button" class="detail-btn">상세</button>
      </td>
    </tr>
  `).join("");

  const filtered = Boolean(filterStatus.value || filterChannel.value || filterDifficulty.value || filterAssignee.value ||
    filterContact.value || filterQ.value || filterDateFrom.value || filterDateTo.value);
  document.getElementById("claims-count").textContent =
    `${filtered ? "검색 결과" : "전체"} ${claims.length}건` + (filtered ? "" : " (최신순)");

  claimsBody.querySelectorAll("tr").forEach(tr => {
    const id = tr.dataset.id;
    tr.querySelector(".save-btn")?.addEventListener("click", async () => {
      const status = tr.querySelector(".status-select").value;
      const assignee = tr.querySelector(".assignee-input").value;
      await updateClaim(id, { status, assignee });
    });
    tr.querySelector(".detail-btn")?.addEventListener("click", () => openDetail(id));
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

function showFormError(message) {
  const el = document.getElementById("claim-form-error");
  el.textContent = message || "";
  el.hidden = !message;
}

// 필수 항목이 비었거나 공백뿐이면 등록하지 않고 첫 빈 칸으로 이동한다.
function validateClaimForm() {
  const labels = { customer: "고객사", product: "서비스/플랜", assignee: "담당자", description: "장애 내용" };
  const missing = [];
  let firstEmpty = null;
  for (const [name, label] of Object.entries(labels)) {
    const input = form.elements[name];
    input.classList.remove("invalid");
    if (!input.value.trim()) {
      missing.push(label);
      input.classList.add("invalid");
      firstEmpty = firstEmpty || input;
    }
  }
  if (missing.length) {
    showFormError(`필수 항목을 입력해주세요: ${missing.join(", ")}`);
    firstEmpty.focus();
    return false;
  }
  const started = form.elements["issue_started_at"].value;
  const ended = form.elements["issue_ended_at"].value;
  if (started && ended && ended < started) {
    showFormError("장애 종료시점이 발생시점보다 빠릅니다.");
    form.elements["issue_ended_at"].focus();
    return false;
  }
  showFormError("");
  return true;
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!validateClaimForm()) return;
  const fd = new FormData(form);
  const payload = Object.fromEntries(fd.entries());
  delete payload["claim-form-files"];
  payload.amount_krw = Number(payload.amount_krw || 0);
  if (form.dataset.customerId) payload.customer_id = Number(form.dataset.customerId);
  if (form.dataset.productId) payload.product_id = Number(form.dataset.productId);
  if (form.dataset.licenseId) payload.license_id = Number(form.dataset.licenseId);

  let res;
  try {
    res = await apiFetch("/api/claims", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    alert("접수 실패: 서버에 연결하지 못했습니다. 잠시 후 다시 시도해주세요.");
    return;
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert("접수 실패: " + (typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail || err || res.status)));
    return;
  }
  const row = await res.json();

  const fileInput = document.getElementById("claim-form-files");
  const files = Array.from(fileInput.files || []).slice(0, 3);
  for (const file of files) {
    const attFd = new FormData();
    attFd.append("file", file);
    const attRes = await apiFetch(`/api/claims/${row.claim_id}/attachments`, { method: "POST", body: attFd });
    if (!attRes.ok) {
      const err = await attRes.json().catch(() => ({}));
      alert(`"${file.name}" 첨부 실패: ` + (err.detail || attRes.status));
    }
  }

  form.reset();
  showFormError("");
  fileInput.value = "";
  delete form.dataset.customerId;
  delete form.dataset.productId;
  delete form.dataset.licenseId;
  document.getElementById("license-badge").innerHTML = "";
  await Promise.all([loadClaims(), loadStats()]);
});

const importKind = document.getElementById("import-kind");
importKind.addEventListener("change", () => {
  document.getElementById("import-product-label").hidden = importKind.value !== "licenses";
});

function importList(title, color, items) {
  return `<p style='font-size:13px; color: ${color}; margin-bottom:2px;'>${title}</p>` +
    `<ul style='font-size:13px; color: var(--muted); margin-top:0;'>${items.join("")}</ul>`;
}

document.getElementById("import-btn").addEventListener("click", async () => {
  const fileInput = document.getElementById("import-file");
  const resultEl = document.getElementById("import-result");
  const file = fileInput.files[0];
  if (!file) {
    alert("파일을 선택해주세요.");
    return;
  }
  const isLicense = importKind.value === "licenses";
  const btn = document.getElementById("import-btn");
  btn.disabled = true;
  btn.textContent = "가져오는 중...";
  resultEl.innerHTML = "";

  const fd = new FormData();
  fd.append("file", file);
  let url = "/api/claims/import";
  if (isLicense) {
    const product = document.getElementById("import-product").value.trim();
    url = "/api/licenses/import" + (product ? `?default_product_name=${encodeURIComponent(product)}` : "");
  }

  try {
    const res = await apiFetch(url, { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) {
      resultEl.innerHTML = `<p style="color: var(--danger);">가져오기 실패: ${escapeHtml(data.detail || res.status)}</p>`;
      return;
    }
    let html = `<p><strong>${data.imported_count}건</strong> 등록됨 / 총 ${data.total_rows}건 중 ` +
      `중복 ${data.duplicate_count}건 제외, 실패 ${data.failed_count}건</p>`;
    if (data.failed_rows && data.failed_rows.length) {
      html += importList("실패", "var(--danger)",
        data.failed_rows.map(f => `<li>${f.row ? f.row + "행: " : ""}${escapeHtml(f.reason)}</li>`));
    }
    if (data.duplicate_rows && data.duplicate_rows.length) {
      html += importList("중복 (건너뜀)", "var(--warn-ink)",
        data.duplicate_rows.map(d => `<li>${d.row ? d.row + "행: " : ""}${d.license_key ? escapeHtml(d.license_key) + " — " : ""}${escapeHtml(d.reason)}</li>`));
    }
    if (data.warnings && data.warnings.length) {
      html += importList("주의(자동 보정됨)", "var(--muted)",
        data.warnings.map(w => `<li>${escapeHtml(String(w.claim_id || w.license_id))}: ${w.warnings.map(escapeHtml).join(", ")}</li>`));
    }
    resultEl.innerHTML = html;
    fileInput.value = "";
    await Promise.all([loadClaims(), loadStats(), loadFieldSuggestions()]);
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
filterDifficulty.addEventListener("change", loadClaims);
filterAssignee.addEventListener("input", debounce(loadClaims, 300));
filterContact.addEventListener("input", debounce(loadClaims, 300));
filterQ.addEventListener("input", debounce(loadClaims, 300));
filterDateFrom.addEventListener("change", loadClaims);
filterDateTo.addEventListener("change", loadClaims);
document.getElementById("reset-filter-btn").addEventListener("click", () => {
  for (const el of [filterStatus, filterChannel, filterDifficulty, filterAssignee, filterContact, filterQ, filterDateFrom, filterDateTo]) {
    el.value = "";
  }
  loadClaims();
});

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

let customersCache = [];
let allProductsCache = [];

async function loadFieldSuggestions() {
  // 고객사/서비스는 라이선스 모듈에 등록된 고객사·제품을 자동완성 후보로 보여준다.
  // 고객사를 정확히 입력하면 그 고객사가 보유한 제품으로 좁혀주고 라이선스 상태 배지도 보여준다.
  try {
    const [customers, products] = await Promise.all([
      apiFetch("/api/customers").then(r => r.json()),
      apiFetch("/api/products").then(r => r.json()),
    ]);
    customersCache = customers;
    allProductsCache = products;
    document.getElementById("customer-suggestions").innerHTML =
      customers.map(c => `<option value="${escapeHtml(c.name)}">`).join("");
    document.getElementById("product-suggestions").innerHTML =
      products.map(p => `<option value="${escapeHtml(p.name)}">`).join("");
  } catch (err) {
    // 라이선스 모듈을 아직 안 쓰는 경우에도 클레임 접수 자체는 막히면 안 된다.
  }
}

async function onCustomerFieldChange() {
  const customerInput = form.elements["customer"];
  const badge = document.getElementById("license-badge");
  const customer = customersCache.find(c => c.name === customerInput.value.trim());
  delete form.dataset.customerId;
  delete form.dataset.productId;
  delete form.dataset.licenseId;
  badge.innerHTML = "";

  if (!customer) {
    document.getElementById("product-suggestions").innerHTML =
      allProductsCache.map(p => `<option value="${escapeHtml(p.name)}">`).join("");
    return;
  }
  form.dataset.customerId = customer.id;
  const products = await apiFetch(`/api/products?customer_id=${customer.id}`).then(r => r.json());
  document.getElementById("product-suggestions").innerHTML =
    products.map(p => `<option value="${escapeHtml(p.name)}">`).join("");
  await onProductFieldChange();
}

async function onProductFieldChange() {
  const badge = document.getElementById("license-badge");
  delete form.dataset.productId;
  delete form.dataset.licenseId;
  badge.innerHTML = "";
  const customerId = form.dataset.customerId;
  const productName = form.elements["product"].value.trim();
  if (!customerId || !productName) return;

  const licenses = await apiFetch(`/api/licenses?customer_id=${customerId}`).then(r => r.json());
  const match = licenses.find(l => l.product_name === productName);
  if (!match) return;
  form.dataset.productId = match.product_id;
  form.dataset.licenseId = match.id;
  const dday = match.status === "만료" ? `D+${Math.abs(match.days_left)}` : `D-${match.days_left}`;
  badge.innerHTML = ` <span class="status-word lic-${match.status}">${match.status} (${dday})</span>`;
}

form.elements["customer"].addEventListener("change", onCustomerFieldChange);
form.elements["product"].addEventListener("change", onProductFieldChange);

// ---------------------------------------------------------------- 클레임 상세(처리내역/첨부파일/이력/추천)

const detailModal = document.getElementById("detail-modal");
let detailClaimId = null;

function attachmentIcon(contentType) {
  return (contentType || "").startsWith("video/") ? "🎞" : "🖼";
}

async function loadDetailAttachments(claimId) {
  const listEl = document.getElementById("detail-attachments");
  const res = await apiFetch(`/api/claims/${claimId}/attachments`);
  const attachments = await res.json();
  listEl.innerHTML = attachments.length
    ? attachments.map(a => `
        <li data-id="${a.id}">
          <a href="/api/attachments/${a.id}/file" target="_blank" rel="noopener">
            ${attachmentIcon(a.content_type)} ${escapeHtml(a.filename)}
          </a>
          <span>
            <span class="muted num">${(a.size_bytes / 1024).toFixed(0)}KB</span>
            <button type="button" class="attach-delete-btn" title="삭제">✕</button>
          </span>
        </li>`).join("")
    : `<li class="muted">첨부된 파일이 없습니다.</li>`;

  listEl.querySelectorAll(".attach-delete-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      const id = btn.closest("li").dataset.id;
      if (!confirm("이 첨부파일을 삭제할까요?")) return;
      await apiFetch(`/api/attachments/${id}`, { method: "DELETE" });
      await loadDetailAttachments(claimId);
    });
  });
}

function historyActionLabel(h) {
  return { create: "접수", import: "가져오기", reopen: "재오픈", status_change: "변경" }[h.action_type] || h.action_type;
}

async function loadDetailHistory(claimId) {
  const listEl = document.getElementById("detail-history");
  const history = await apiFetch(`/api/claims/${claimId}/history`).then(r => r.json());
  listEl.innerHTML = history.length
    ? history.map(h => `
        <li>
          <span class="when num">${escapeHtml(h.created_at)}</span>
          <span class="status-word">[${historyActionLabel(h)}]</span>
          ${h.prev_status ? `${escapeHtml(h.prev_status)} → ` : ""}${escapeHtml(h.new_status)}
          ${h.changed_by ? ` · ${escapeHtml(h.changed_by)}` : ""}
          ${h.note ? `<br><span class="muted">${escapeHtml(h.note)}</span>` : ""}
        </li>`).join("")
    : `<li class="muted">이력이 없습니다.</li>`;
}

async function loadDetailRecommendations(claimId) {
  const el = document.getElementById("detail-recommendations");
  const data = await apiFetch(`/api/claims/${claimId}/recommendations`).then(r => r.json());
  const similarHtml = data.similar_claims.length
    ? data.similar_claims.map(c => `
        <li>
          <span class="who">${escapeHtml(c.claim_id)} · ${escapeHtml(c.customer)} · ${escapeHtml(c.product)}</span>
          ${c.difficulty ? ` <span class="muted">(${DIFFICULTY_LABELS[c.difficulty] || escapeHtml(c.difficulty)}${c.processing_minutes ? `, ${c.processing_minutes}분` : ""})</span>` : ""}
          <br><span class="muted">${escapeHtml(c.resolution)}</span>
        </li>`).join("")
    : `<li class="muted">유사한 과거 사례가 없습니다.</li>`;
  const manualsHtml = data.manuals.length
    ? data.manuals.map(m => `<li><span class="who">${escapeHtml(m.title)}</span><br><span class="muted">${escapeHtml(m.content)}</span></li>`).join("")
    : `<li class="muted">관련 매뉴얼이 없습니다.</li>`;
  el.innerHTML = `
    <p class="hint">유사 사례</p>
    <ul class="history-list">${similarHtml}</ul>
    <p class="hint">매뉴얼</p>
    <ul class="history-list">${manualsHtml}</ul>
  `;
}

async function openDetail(claimId) {
  detailClaimId = claimId;
  const res = await apiFetch(`/api/claims/${claimId}`);
  const claim = await res.json();

  document.getElementById("detail-modal-title").textContent = `클레임 상세: ${claim.claim_id}`;
  document.getElementById("detail-processing-type").value = claim.processing_type || "";
  document.getElementById("detail-sub-status").value = claim.sub_status || "";
  document.getElementById("detail-difficulty").value = claim.difficulty || "";
  document.getElementById("detail-processing-minutes").value = claim.processing_minutes || 0;
  document.getElementById("detail-visit-time").value = claim.visit_time || "";
  document.getElementById("detail-issue-started").value = toLocalInput(claim.issue_started_at);
  document.getElementById("detail-issue-ended").value = toLocalInput(claim.issue_ended_at);
  document.getElementById("detail-scheduled-visit").value = toLocalInput(claim.scheduled_visit_at);
  document.getElementById("detail-completed-visit").value = toLocalInput(claim.completed_visit_at);
  document.getElementById("detail-partner-name").value = claim.partner_name || "";
  document.getElementById("detail-partner-contact").value = claim.partner_contact || "";
  document.getElementById("detail-resolution").value = claim.resolution || "";

  document.getElementById("detail-reopen-btn").hidden = claim.status !== "완료";

  await Promise.all([loadDetailAttachments(claimId), loadDetailHistory(claimId), loadDetailRecommendations(claimId)]);
  detailModal.hidden = false;
}

function closeDetail() {
  detailModal.hidden = true;
  detailClaimId = null;
}

document.getElementById("detail-modal-close").addEventListener("click", closeDetail);
detailModal.addEventListener("click", (e) => {
  if (e.target === detailModal) closeDetail(); // 바깥(배경) 클릭 시 닫기
});

document.getElementById("detail-save-btn").addEventListener("click", async () => {
  if (!detailClaimId) return;
  await updateClaim(detailClaimId, {
    processing_type: document.getElementById("detail-processing-type").value,
    sub_status: document.getElementById("detail-sub-status").value,
    difficulty: document.getElementById("detail-difficulty").value,
    processing_minutes: Number(document.getElementById("detail-processing-minutes").value || 0),
    visit_time: document.getElementById("detail-visit-time").value,
    issue_started_at: document.getElementById("detail-issue-started").value,
    issue_ended_at: document.getElementById("detail-issue-ended").value,
    scheduled_visit_at: document.getElementById("detail-scheduled-visit").value,
    completed_visit_at: document.getElementById("detail-completed-visit").value,
    partner_name: document.getElementById("detail-partner-name").value,
    partner_contact: document.getElementById("detail-partner-contact").value,
    resolution: document.getElementById("detail-resolution").value,
  });
  await loadDetailHistory(detailClaimId);
});

document.getElementById("detail-reopen-btn").addEventListener("click", async () => {
  if (!detailClaimId) return;
  const note = prompt("재오픈 사유를 입력해주세요 (재발 경위 등):", "");
  if (note === null) return;
  await updateClaim(detailClaimId, { status: "처리중", note });
  await openDetail(detailClaimId);
});

document.getElementById("detail-upload-btn").addEventListener("click", async () => {
  if (!detailClaimId) return;
  const fileInput = document.getElementById("detail-file-input");
  const file = fileInput.files[0];
  if (!file) {
    alert("파일을 선택해주세요.");
    return;
  }
  const fd = new FormData();
  fd.append("file", file);
  const res = await apiFetch(`/api/claims/${detailClaimId}/attachments`, { method: "POST", body: fd });
  if (!res.ok) {
    const err = await res.json();
    alert("업로드 실패: " + (err.detail || res.status));
    return;
  }
  fileInput.value = "";
  await loadDetailAttachments(detailClaimId);
});

loadUser();
loadFieldSuggestions();
loadStats();
loadClaims();
