// 모든 로그인 후 페이지가 공유하는 스크립트: 메뉴 바, API 호출, 사용자 표시, 상태 요약 보드.

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

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
  const statsEl = document.getElementById("stats");
  if (!statsEl) return;
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

// ---- 상단 메뉴: 대시보드 / 장애이력관리 / 라이선스관리 (+ 보조 메뉴)
(function injectNav() {
  const main = [["/", "대시보드"], ["/claims", "장애이력관리"], ["/licenses", "라이선스관리"]];
  const sub = [["/manuals", "매뉴얼"], ["/submit", "고객 접수 화면"]];
  const path = location.pathname.replace(/\/+$/, "") || "/";
  const link = ([href, label], cls) =>
    `<a href="${href}" class="${cls}${path === href ? " active" : ""}"${path === href ? ' aria-current="page"' : ""}>${label}</a>`;
  const nav = document.createElement("nav");
  nav.className = "site-nav";
  nav.setAttribute("aria-label", "주 메뉴");
  nav.innerHTML =
    `<span class="site-nav-brand">클레임 통합관리</span>` +
    main.map(m => link(m, "site-nav-link")).join("") +
    `<span class="site-nav-spacer"></span>` +
    sub.map(m => link(m, "site-nav-sub")).join("");
  document.body.insertBefore(nav, document.body.firstChild);
})();
