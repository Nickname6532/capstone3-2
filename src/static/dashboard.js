// 대시보드 전용: 만기 타임라인, 만료 임박 목록, 상태 요약, 담당자별 실적.

const TL_DAYS = 120;      // 타임라인이 보여주는 범위(오늘부터 +120일)
const TL_LANE_H = 18;     // 라벨 한 줄(레인) 높이
const TL_AXIS_H = 48;     // 축 + 눈금 라벨 영역 높이
const TL_TOP_PAD = 34;
const tl = { host: null, items: [], lastWidth: 0 };

// 항목마다 서로 다른 색(황금각 간격)을 주어 타임라인 점과 목록 행을 색으로 짝짓는다.
function hueOf(i) {
  return Math.round((i * 137.5 + 8) % 360);
}

function isExpired(lic) {
  return lic.status === "만료";
}

function ddayText(lic) {
  if (lic.days_left < 0) return `D+${Math.abs(lic.days_left)}`;
  if (lic.days_left === 0) return "D-day";
  return `D-${lic.days_left}`;
}

function whenText(lic) {
  if (isExpired(lic)) return `${Math.abs(lic.days_left)}일 전 만료됨`;
  return lic.days_left === 0 ? "오늘 만료" : `${lic.days_left}일 후 만료`;
}

function reducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

// ---------------------------------------------------------------- 선택(마커 <-> 목록 행)

function selectLicense(key, scrollToRow) {
  document.querySelectorAll(".tl-marker.is-active, .alert-row.is-active").forEach(el => {
    el.classList.remove("is-active");
    el.removeAttribute("aria-current");
  });
  const marker = document.querySelector(`.tl-marker[data-key="${key}"]`);
  const row = document.querySelector(`.alert-row[data-key="${key}"]`);
  if (marker) { marker.classList.add("is-active"); marker.setAttribute("aria-current", "true"); }
  if (row) {
    row.classList.add("is-active");
    row.setAttribute("aria-current", "true");
    if (scrollToRow) row.scrollIntoView({ block: "nearest", behavior: reducedMotion() ? "auto" : "smooth" });
  }
}

// ---------------------------------------------------------------- 타임라인

function buildTimeline(host, items) {
  host.innerHTML = `<div class="tl-plot" role="group" aria-label="만기 타임라인: 오늘부터 ${TL_DAYS}일 후까지"></div>`;
  const plot = host.querySelector(".tl-plot");
  items.forEach((lic, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "tl-marker hued";
    b.style.setProperty("--h", String(hueOf(i)));
    b.dataset.key = String(i);
    b.style.setProperty("--i", String(Math.min(i, 14)));
    const full = `${lic.customer_name} / ${lic.product_name} / 만료일 ${lic.end_date} (${lic.status})`;
    b.title = full;
    b.setAttribute("aria-label", `${full}, ${ddayText(lic)}. 누르면 아래 목록에서 해당 항목을 표시합니다`);
    b.innerHTML =
      `<span class="tl-stem"></span>` +
      `<span class="tl-label" aria-hidden="true"></span>` +
      `<span class="tl-tip"><span class="tl-name">${escapeHtml(lic.customer_name)}</span>` +
      `<span class="tl-dday num">${ddayText(lic)}</span></span>`;
    plot.appendChild(b);
  });
}

function addStatic(plot, className, styles, html) {
  const el = document.createElement("div");
  el.className = `tl-static ${className}`;
  Object.assign(el.style, styles);
  if (html) el.innerHTML = html;
  plot.insertBefore(el, plot.querySelector(".tl-marker"));   // 마커(포커스 유지)는 항상 눈금 위
  return el;
}

function layoutTimeline() {
  const host = tl.host;
  if (!host) return;
  const plot = host.querySelector(".tl-plot");
  if (!plot) return;
  const items = tl.items;
  const W = plot.clientWidth;
  if (W <= 0) return;
  tl.lastWidth = W;

  plot.querySelectorAll(".tl-static").forEach(n => n.remove());
  const markers = Array.from(plot.querySelectorAll(".tl-marker"));

  const hasPast = items.some(l => l.days_left < 0);
  const padL = 12, padR = 20;
  const zoneW = hasPast ? (W < 520 ? 60 : 92) : 0;
  const x0 = padL + zoneW;                   // 오늘 선
  const x1 = W - padR;                       // +120일
  const pastMax = Math.max(30, ...items.filter(l => l.days_left < 0).map(l => Math.abs(l.days_left)));
  const xOf = (d) => {
    if (d >= 0) return x0 + (Math.min(d, TL_DAYS) / TL_DAYS) * (x1 - x0);
    const t = Math.min(Math.abs(d), pastMax) / pastMax;
    return padL + 8 + (1 - t) * (zoneW - 24);
  };

  // 1) 라벨 폭을 실제로 재서 좌/우 배치와 레인을 계산한다. (줄기는 라벨 뒤로 그려지므로 라벨끼리만 겹치지 않게 한다)
  const order = markers
    .map(m => ({ m, lic: items[Number(m.dataset.key)] }))
    .sort((a, b) => xOf(a.lic.days_left) - xOf(b.lic.days_left) || a.lic.days_left - b.lic.days_left);
  const GAP = 2;
  const lanesLo = [];   // lanesLo[i] = i번 레인에 이미 놓인 라벨 구간 [lo, hi] 목록
  let lanes = 1;

  for (const { m, lic } of order) {
    const x = xOf(lic.days_left);
    const flip = x > W - 150;   // 툴팁이 오른쪽 밖으로 나가지 않게
    const lo = x - 8, hi = x + 8;
    let lane = 0;
    while ((lanesLo[lane] || []).some(r => lo < r[1] + GAP && r[0] < hi + GAP)) lane++;
    (lanesLo[lane] = lanesLo[lane] || []).push([lo, hi]);
    lanes = Math.max(lanes, lane + 1);
    m.classList.toggle("flip", flip);
    m.style.left = `${x}px`;
    m.style.bottom = `${TL_AXIS_H}px`;
    m.style.height = `${(lane + 1) * TL_LANE_H}px`;
  }

  const plotH = TL_TOP_PAD + lanes * TL_LANE_H + TL_AXIS_H;
  plot.style.height = `${plotH}px`;
  const axisY = plotH - TL_AXIS_H;

  // 2) 눈금: 오늘, 월 경계(긴 눈금 + 라벨), 주 경계(짧은 눈금)
  if (hasPast) {
    addStatic(plot, "tl-zone", { left: "0px", top: "0px", width: `${x0}px`, height: `${axisY}px` });
    addStatic(plot, "tl-zone-label", { left: "0px", width: `${x0}px`, top: `${axisY + 10}px` }, "지남");
  }
  addStatic(plot, "tl-axis", { left: "0px", width: `${W}px`, top: `${axisY}px` });
  addStatic(plot, "tl-today", { left: `${x0}px`, top: "0px", height: `${axisY + 6}px` });
  addStatic(plot, "tl-today-label", { left: `${x0 + 5}px`, top: `${axisY + 10}px` }, "오늘");

  const today = new Date();
  today.setHours(0, 0, 0, 0);
  for (let off = 1; off <= TL_DAYS; off++) {
    const d = new Date(today);
    d.setDate(d.getDate() + off);
    const x = xOf(off);
    if (d.getDate() === 1) {
      addStatic(plot, "tl-grid", { left: `${x}px`, top: "0px", height: `${axisY + 8}px` });
      const text = d.getMonth() === 0 ? `${d.getFullYear()}년 1월` : `${d.getMonth() + 1}월`;
      if (x - x0 > 38 && x + 4 + (d.getMonth() === 0 ? 60 : 34) < W) {
        addStatic(plot, "tl-month", { left: `${x + 4}px`, top: `${axisY + 10}px` }, text);
      }
    } else if (off % 7 === 0) {
      addStatic(plot, "tl-week", { left: `${x}px`, top: `${axisY}px` });
    }
  }
}

function renderTimeline(host, items) {
  tl.host = host;
  tl.items = items;
  buildTimeline(host, items);
  layoutTimeline();
  if (!reducedMotion()) {
    host.classList.add("tl-reveal");
    setTimeout(() => host.classList.remove("tl-reveal"), 1400);
  }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(layoutTimeline);

  host.addEventListener("click", (e) => {
    const m = e.target.closest(".tl-marker");
    if (m) selectLicense(m.dataset.key, true);
  });
  host.addEventListener("focusin", (e) => {
    const m = e.target.closest(".tl-marker");
    if (m) selectLicense(m.dataset.key, false);
  });
}

let tlResizeFrame = 0;
function scheduleTimelineLayout() {
  if (!tl.host || !tl.items.length) return;
  cancelAnimationFrame(tlResizeFrame);
  tlResizeFrame = requestAnimationFrame(() => {
    const plot = tl.host.querySelector(".tl-plot");
    if (plot && plot.clientWidth !== tl.lastWidth) layoutTimeline();
  });
}
window.addEventListener("resize", scheduleTimelineLayout);
if (window.ResizeObserver) {
  new ResizeObserver(scheduleTimelineLayout).observe(document.getElementById("license-alert"));
}

// ---------------------------------------------------------------- 목록

async function loadLicenseAlerts() {
  const el = document.getElementById("license-alert");
  const body = document.getElementById("license-alert-body");
  const host = document.getElementById("license-timeline");
  let licenses;
  try {
    const res = await apiFetch("/api/licenses/expiring");
    if (!res.ok) throw new Error(String(res.status));
    licenses = await res.json();
  } catch (err) {
    host.innerHTML = "";
    body.innerHTML = `<p class="alert-empty">만기 정보를 불러오지 못했습니다. 잠시 후 새로고침하거나, 계속되면 라이선스관리 화면에서 직접 확인해 주세요.</p>`;
    return;
  }

  if (!licenses.length) {
    el.classList.remove("alert-board");
    tl.items = [];
    host.innerHTML = "";
    body.innerHTML = `<p class="alert-empty">지금 확인할 만기 알림이 없습니다. 만료 기준일에 가까워진 라이선스가 생기면 여기에 표시됩니다.</p>`;
    return;
  }

  el.classList.add("alert-board");
  const items = licenses.slice().sort((a, b) => a.days_left - b.days_left);
  body.innerHTML = items.map((lic, i) => `
      <div class="alert-row hued" style="--h:${hueOf(i)}" data-key="${i}">
        <span class="dday num">${ddayText(lic)}</span>
        <span class="who"><strong class="cust">${escapeHtml(lic.customer_name)}</strong>
          <span class="prod">${escapeHtml(lic.product_name)}</span>
          <span class="status-word lic-${lic.status}">${lic.status}</span>
        </span>
        <span class="when num">${escapeHtml(lic.end_date)} (${whenText(lic)})</span>
      </div>
    `).join("");
  body.querySelectorAll(".alert-row").forEach(row => {
    row.addEventListener("click", () => selectLicense(row.dataset.key, false));
  });
  renderTimeline(host, items);
}

// ---------------------------------------------------------------- 담당자별 실적

async function loadAnalytics() {
  const el = document.getElementById("analytics-body");
  const data = await apiFetch("/api/stats/analytics").then(r => r.json());
  if (!data.by_assignee.length) {
    el.innerHTML = `<p class="hint">아직 집계할 클레임이 없습니다. 장애이력관리에서 접수하면 담당자별 실적이 쌓입니다.</p>`;
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
            <span>완료 <b>${a.done}</b></span>
            <span>완료율 <b>${a.completion_rate}%</b></span>
            <span>평균 <b>${a.avg_processing_minutes}분</b></span>
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
