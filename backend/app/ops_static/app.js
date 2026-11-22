"use strict";

// 콜록 운영 지표 화면. 서버가 계산한 숫자를 그리기만 한다.
// 사용자 입력(닉네임 등)은 항상 textContent 로만 넣는다.

const CODE_KEY = "collog-ops-code";
const state = { code: null, tab: "core", summary: null, accounts: null };

const $ = (id) => document.getElementById(id);

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    // CSP 가 style 속성을 막으므로 CSSOM 으로 넣는다
    else if (key === "style") node.style.cssText = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function storage(action, value) {
  try {
    if (action === "get") return localStorage.getItem(CODE_KEY);
    if (action === "set") localStorage.setItem(CODE_KEY, value);
    if (action === "clear") localStorage.removeItem(CODE_KEY);
  } catch (_) {
    return null;
  }
  return null;
}

// ------------------------------------------------------------ 표기

const fmt = (n) => (n === null || n === undefined ? "–" : Number(n).toLocaleString("ko-KR"));
const pct = (r) => (r && r.pct !== null ? `${r.pct}%` : "–");
const frac = (r) => (r ? `${fmt(r.n)} / ${fmt(r.d)}` : "–");

const ROLE = { PARENT: "부모", CHILD: "자녀" };
const PLATFORM = { IOS: "iPhone", ANDROID: "Android" };
const GENDER = { MALE: "남성", FEMALE: "여성" };
const CONDITION = {
  FORGETFULNESS: "건망증",
  RESPIRATORY: "호흡기 질환",
  RHINITIS: "비염",
  DIABETES: "당뇨",
  HYPERTENSION: "고혈압",
  DYSLIPIDEMIA: "이상지질혈증",
  ASTHMA: "천식",
  OBESITY: "비만",
};

function kst(iso, withTime) {
  if (!iso) return "–";
  const d = new Date(iso);
  const opts = { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit" };
  if (withTime) Object.assign(opts, { hour: "2-digit", minute: "2-digit", hour12: false });
  return d.toLocaleString("ko-KR", opts);
}

function minutes(sec) {
  if (sec === null || sec === undefined) return "–";
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return m ? `${m}분 ${s}초` : `${s}초`;
}

// ------------------------------------------------------------ 서버

async function api(path, options) {
  const response = await fetch(path, {
    ...options,
    headers: { "X-Ops-Code": state.code, "Content-Type": "application/json" },
    cache: "no-store",
  });
  if (response.status === 401) {
    logout("접속 코드가 맞지 않거나 바뀌었습니다");
    throw new Error("unauthorized");
  }
  if (!response.ok) throw new Error(`서버 응답 ${response.status}`);
  return response.json();
}

function summaryPath() {
  const params = new URLSearchParams();
  if ($("include-internal").checked) params.set("internal", "true");
  if ($("since").value) params.set("since", $("since").value);
  const query = params.toString();
  return `/ops/api/summary${query ? `?${query}` : ""}`;
}

async function loadSummary() {
  showStatus("불러오는 중…");
  try {
    state.summary = await api(summaryPath());
    showStatus(null);
    render();
  } catch (error) {
    if (error.message !== "unauthorized") showStatus(`불러오지 못했습니다 (${error.message})`);
  }
}

async function loadAccounts() {
  showStatus("계정 목록을 불러오는 중…");
  try {
    state.accounts = (await api("/ops/api/accounts")).accounts;
    showStatus(null);
    render();
  } catch (error) {
    if (error.message !== "unauthorized") showStatus(`불러오지 못했습니다 (${error.message})`);
  }
}

function showStatus(text) {
  $("status").hidden = !text;
  $("status").textContent = text || "";
}

// ------------------------------------------------------------ 조각

function card(label, value, sub, def, hero) {
  return el(
    "article",
    { class: hero ? "card hero" : "card" },
    el("span", { class: "label" }, label),
    el("span", { class: "value" }, value),
    sub ? el("span", { class: "sub" }, sub) : null,
    def ? el("span", { class: "def" }, def) : null,
  );
}

function bars(title, counts, note, labels) {
  const entries = Object.entries(counts || {});
  const max = Math.max(1, ...entries.map(([, v]) => v));
  const total = entries.reduce((sum, [, v]) => sum + v, 0);
  return el(
    "div",
    { class: "block" },
    el("h2", {}, title),
    entries.length
      ? el(
          "div",
          { class: "bars" },
          entries.map(([key, value]) =>
            el(
              "div",
              { class: "bar-row" },
              el("span", {}, (labels && labels[key]) || key),
              el(
                "span",
                { class: "bar-track" },
                el("span", { class: "bar-fill", style: `width:${(100 * value) / max}%` }),
              ),
              el("span", { class: "bar-num" }, `${fmt(value)}`),
            ),
          ),
        )
      : el("p", { class: "muted" }, "아직 없음"),
    el("p", { class: "muted" }, `합계 ${fmt(total)}${note ? ` · ${note}` : ""}`),
  );
}

function table(head, rows) {
  return el(
    "div",
    { class: "table-wrap" },
    el(
      "table",
      {},
      el("thead", {}, el("tr", {}, head.map((h) => el("th", {}, h)))),
      el("tbody", {}, rows),
    ),
  );
}

// ------------------------------------------------------------ 탭

function renderCore(s) {
  const h = s.headline;
  const f = h.callDaysPerWeek;
  return [
    el(
      "div",
      { class: "grid" },
      card(
        "가입 완료 사용자",
        `${fmt(h.signedUp)}명`,
        `부모 ${fmt(h.signedUpParents)} · 자녀 ${fmt(h.signedUpChildren)} · 최근 7일 +${fmt(h.new7d)} · 30일 +${fmt(h.new30d)}`,
        `계정을 만든 사람. 안드로이드 가입 단계(닉네임·약관)에서 멈춘 ${fmt(h.loginOnly)}명은 뺌`,
        true,
      ),
      card(
        "최근 7일 앱 사용",
        pct(h.active7d),
        `${frac(h.active7d)}명`,
        "가입자 중 최근 7일 안에 앱을 열어 로그인이 갱신됐거나 통화·할 일·건강 동기화를 한 사람",
      ),
      card("최근 30일 앱 사용", pct(h.active30d), `${frac(h.active30d)}명`, "같은 기준, 30일"),
      card(
        "가입 7일 뒤에도 사용",
        pct(h.returnedAfter7d),
        `${frac(h.returnedAfter7d)}명`,
        "가입한 지 7일이 지난 사람 중, 가입 7일 뒤 이후에 앱을 쓴 기록이 있는 사람",
      ),
      card(
        "연결된 가족",
        `${fmt(h.linkedFamilies.n)}곳`,
        `만든 가족 ${fmt(h.families)}곳 중 ${pct(h.linkedFamilies)}`,
        "가입한 사람이 둘 이상 들어온 가족(초대만 하고 아무도 안 들어온 가족은 연결 안 됨)",
      ),
      card(
        "통화한 가족(부모·자녀 쌍)",
        `${fmt(h.callingPairs)}쌍`,
        `최근 7일 통화한 쌍 ${fmt(h.callingPairs7d)}`,
        "부모와 자녀 한 쌍이 한 번이라도 받은 통화가 있으면 셈",
      ),
      card(
        "받은 통화",
        `${fmt(h.connectedCalls)}건`,
        `최근 7일 ${fmt(h.connectedCalls7d)}건 · 분석 완료 ${fmt(h.analyzedCalls)}건`,
        "상대가 받아 연결된 통화(안 받은 전화는 뺌)",
      ),
      card(
        "첫 통화 뒤 14일 안 다시 통화",
        pct(h.repeatWithin14d),
        `${frac(h.repeatWithin14d)}쌍`,
        "첫 통화가 14일 이상 지난 쌍 중, 첫 통화 다음 날부터 14일 안에 다시 통화한 쌍",
      ),
      card(
        "한 주에 통화하는 날",
        f.median === null ? "–" : `${f.median}일`,
        `평균 ${f.mean ?? "–"}일 · 대상 ${fmt(f.pairs)}쌍`,
        "최근 28일 통화가 있던 쌍의 통화한 날 수 ÷ 4 (중앙값). 같은 날 여러 번은 하루로 셈",
      ),
    ),
    bars("최근 28일 쌍별 통화한 날 수", f.buckets, "28일 동안 며칠 통화했는지"),
  ];
}

function renderCalls(s) {
  const rows = [
    ["통화 시도", (x) => fmt(x.attempts)],
    ["상대가 받음", (x) => `${fmt(x.connected.n)} (${pct(x.connected)})`],
    ["녹음·분석 켬", (x) => `${fmt(x.recorded.n)} (${pct(x.recorded)})`],
    ["분석 완료", (x) => `${fmt(x.analyzed.n)} (${pct(x.analyzed)})`],
    ["분석 제외(말이 짧음 등)", (x) => fmt(x.excluded)],
    ["분석 실패", (x) => fmt(x.failed)],
    ["처리 중", (x) => fmt(x.inProgress)],
    ["리포트 알림 보냄", (x) => fmt(x.reportNotified)],
  ];
  const c = s.calls;
  return [
    el(
      "div",
      { class: "block" },
      el("h2", {}, "통화 흐름"),
      table(
        ["단계", "전체", "최근 30일"],
        rows.map(([label, pick]) =>
          el("tr", {}, el("td", {}, label), el("td", {}, pick(c.all)), el("td", {}, pick(c.last30d))),
        ),
      ),
      el(
        "p",
        { class: "muted" },
        "받음 % = 받음 / 시도, 녹음 % = 녹음 / 받음, 분석 완료 % = 완료 / (완료 + 제외 + 실패)",
      ),
    ),
    el(
      "div",
      { class: "grid", style: "margin-top:12px" },
      card(
        "통화 시간(중앙값)",
        minutes(c.durationSec.median),
        `평균 ${minutes(c.durationSec.mean)} · ${fmt(c.durationSec.count)}건`,
        "받은 통화 중 길이가 기록된 것",
      ),
      card(
        "부모님이 말한 시간(중앙값)",
        minutes(c.parentSpeechSec.median),
        `${fmt(c.parentSpeechSec.count)}건`,
        "분석에서 잰 부모 발화 시간",
      ),
    ),
  ];
}

function renderFeatures(s) {
  const x = s.features;
  return [
    el(
      "div",
      { class: "grid" },
      card(
        "리포트 발행",
        `${fmt(x.reportsIssued)}회`,
        `부모 ${fmt(x.reportParents)}명 · 최근 30일 ${fmt(x.reports30d)}회`,
        "누군가 리포트를 열어 새로 만들어진 횟수(같은 내용을 다시 열면 늘지 않음)",
      ),
      card("건강 앱 연동", pct(x.healthLinked), `${frac(x.healthLinked)}명`, "건강 앱 연동 동의가 지금 켜져 있는 가입자"),
      card("최근 7일 건강 동기화", `${fmt(x.healthSynced7d)}명`, null, "최근 7일 안에 건강 요약을 올린 사람"),
      card("오늘 할 일 쓰는 사람", pct(x.todoUsers), `${frac(x.todoUsers)}명`, "직접 할 일을 한 번이라도 쓴 가입자"),
      card("직접 쓴 할 일 완료", pct(x.todoDone), `${frac(x.todoDone)}개`, "지우지 않은 직접 쓴 할 일 중 완료"),
      card("추천 할 일 완료", pct(x.suggestedDone), `${frac(x.suggestedDone)}개`, "앱이 추천한 할 일 중 완료"),
      card(
        "변화 신호",
        `${fmt(x.changeSignals)}건`,
        `알림까지 간 것 ${fmt(x.promotedSignals)} · 급한 안내 ${fmt(x.acuteSignals)}`,
        "지난 통화와 비교해 달라진 점으로 잡힌 것",
      ),
      card("마케팅 수신 동의", pct(x.marketingOptIn), `${frac(x.marketingOptIn)}명`, null),
    ),
    el(
      "div",
      { class: "blocks", style: "margin-top:12px" },
      bars(
        "등록한 질환",
        x.conditions,
        `프로필이 있는 부모 ${fmt(x.parentsWithProfile)}명 · 걱정을 직접 쓴 부모 ${fmt(x.concernsWritten)}명`,
        CONDITION,
      ),
    ),
  ];
}

function renderPeople(s) {
  const p = s.people;
  return el(
    "div",
    { class: "blocks" },
    bars("마지막으로 쓴 기기", p.platform, "가입자 기준", PLATFORM),
    bars("로그인 방식", p.login, "가입자 기준"),
    bars("부모 연령대", p.parentAge, "가입 때 적은 생년월일"),
    bars("자녀 연령대", p.childAge, "가입 때 적은 생년월일"),
    bars("성별", p.gender, "가입자 기준", GENDER),
  );
}

function renderWeekly(s) {
  const weeks = s.weekly;
  const max = Math.max(1, ...weeks.map((w) => w.connectedCalls));
  return el(
    "div",
    { class: "block" },
    el("h2", {}, "주별 추이 (월요일 시작, 한국 시간)"),
    table(
      ["주", "새 가입", "받은 통화", "통화한 쌍", "분석 완료", ""],
      weeks.map((w) =>
        el(
          "tr",
          {},
          el("td", {}, w.week.slice(5).replace("-", ".") + " 주"),
          el("td", {}, fmt(w.signups)),
          el("td", {}, fmt(w.connectedCalls)),
          el("td", {}, fmt(w.callingPairs)),
          el("td", {}, fmt(w.analyzedCalls)),
          el(
            "td",
            { class: "left", style: "min-width:120px" },
            el(
              "span",
              { class: "bar-track", style: "display:block" },
              el("span", {
                class: "bar-fill",
                style: `display:block;width:${(100 * w.connectedCalls) / max}%`,
              }),
            ),
          ),
        ),
      ),
    ),
    el("p", { class: "muted" }, "막대 = 받은 통화. 이번 주는 아직 진행 중"),
  );
}

function renderAccounts() {
  if (!state.accounts) {
    loadAccounts();
    return el("p", { class: "muted" }, "불러오는 중…");
  }
  const filter = el("input", {
    class: "filter",
    type: "search",
    placeholder: "이름·닉네임으로 찾기",
    oninput: () => paint(),
  });
  const body = el("tbody");
  function paint() {
    const q = filter.value.trim();
    body.replaceChildren(
      ...state.accounts
        .filter((a) => !q || (a.label || "").includes(q))
        .map((a) => {
          const box = el("input", {
            type: "checkbox",
            checked: a.internal,
            "aria-label": "팀·시험 계정",
            onchange: async () => {
              box.disabled = true;
              try {
                await api(`/ops/api/accounts/${encodeURIComponent(a.id)}/internal`, {
                  method: "PUT",
                  body: JSON.stringify({ internal: box.checked }),
                });
                a.internal = box.checked;
                state.summary = null;
                paint();
              } catch (error) {
                box.checked = a.internal;
                showStatus(`바꾸지 못했습니다 (${error.message})`);
              } finally {
                box.disabled = false;
              }
            },
          });
          return el(
            "tr",
            { class: a.internal ? "internal" : null },
            el("td", { class: "left" }, a.label),
            el("td", { class: "left" }, ROLE[a.role] || a.role),
            el("td", { class: "left" }, PLATFORM[a.platform] || "–"),
            el("td", { class: "left" }, a.login),
            el("td", {}, kst(a.createdAt)),
            el("td", {}, kst(a.lastSeenAt, true)),
            el("td", {}, fmt(a.connectedCalls)),
            el("td", {}, box),
          );
        }),
    );
  }
  paint();
  return el(
    "div",
    { class: "block" },
    el("h2", {}, `계정 ${fmt(state.accounts.length)}개`),
    el(
      "p",
      { class: "muted" },
      "팀원·시험·심사 계정에 표시하면 모든 지표에서 빠집니다(위 「팀·시험 계정도 포함」을 켜면 다시 셈). 닉네임이 없는 계정(iOS)은 이름의 첫 글자와 끝 글자만 보입니다.",
    ),
    filter,
    el(
      "div",
      { class: "table-wrap" },
      el(
        "table",
        {},
        el(
          "thead",
          {},
          el(
            "tr",
            {},
            ["이름·닉네임", "역할", "기기", "로그인", "가입일", "마지막 사용", "받은 통화", "팀·시험"].map((h, i) =>
              el("th", { class: i < 4 ? "left" : null }, h),
            ),
          ),
        ),
        body,
      ),
    ),
  );
}

function render() {
  for (const button of document.querySelectorAll(".tabs button")) {
    button.setAttribute("aria-selected", String(button.dataset.tab === state.tab));
  }
  const panel = $("panel");
  if (state.tab === "accounts") {
    panel.replaceChildren(renderAccounts());
    return;
  }
  const s = state.summary;
  if (!s) {
    loadSummary();
    panel.replaceChildren();
    return;
  }
  $("as-of").textContent = `${kst(s.generatedAt, true)} 기준 · 한국 시간`;
  $("scope-note").textContent = s.scope.includeInternal
    ? `팀·시험 계정 ${fmt(s.scope.internalAccounts)}개 포함`
    : `팀·시험 계정 ${fmt(s.scope.internalAccounts)}개 뺌`;
  const views = {
    core: renderCore,
    calls: renderCalls,
    features: renderFeatures,
    people: renderPeople,
    weekly: renderWeekly,
  };
  panel.replaceChildren(...[views[state.tab](s)].flat());
}

// ------------------------------------------------------------ 복사

function summaryText(s) {
  const h = s.headline;
  const c = s.calls;
  const x = s.features;
  const scope = s.scope.includeInternal ? "팀·시험 계정 포함" : "팀·시험 계정 제외";
  const since = s.scope.since ? `, ${s.scope.since} 이후 가입자` : "";
  return [
    `콜록 서비스 지표 (${kst(s.generatedAt, true)} 한국 시간 기준, ${scope}${since})`,
    `- 가입 완료 사용자 ${fmt(h.signedUp)}명 (부모 ${fmt(h.signedUpParents)}, 자녀 ${fmt(h.signedUpChildren)}), 최근 30일 신규 ${fmt(h.new30d)}명`,
    `- 연결된 가족 ${fmt(h.linkedFamilies.n)}곳 (만든 가족 ${fmt(h.families)}곳 중 ${pct(h.linkedFamilies)})`,
    `- 통화한 부모·자녀 쌍 ${fmt(h.callingPairs)}쌍, 받은 통화 ${fmt(h.connectedCalls)}건 (분석 완료 ${fmt(h.analyzedCalls)}건)`,
    `- 최근 7일 앱 사용 ${pct(h.active7d)} (${frac(h.active7d)}명), 최근 30일 ${pct(h.active30d)} (${frac(h.active30d)}명)`,
    `- 가입 7일 뒤에도 사용 ${pct(h.returnedAfter7d)} (${frac(h.returnedAfter7d)}명)`,
    `- 첫 통화 뒤 14일 안 다시 통화 ${pct(h.repeatWithin14d)} (${frac(h.repeatWithin14d)}쌍)`,
    `- 한 주에 통화하는 날 중앙값 ${h.callDaysPerWeek.median ?? "–"}일 (평균 ${h.callDaysPerWeek.mean ?? "–"}일, 최근 28일 통화한 ${fmt(h.callDaysPerWeek.pairs)}쌍)`,
    `- 통화 연결률 ${pct(c.all.connected)} (${frac(c.all.connected)}), 분석 완료율 ${pct(c.all.analyzed)} (${frac(c.all.analyzed)})`,
    `- 통화 시간 중앙값 ${minutes(c.durationSec.median)}`,
    `- 리포트 발행 ${fmt(x.reportsIssued)}회, 건강 앱 연동 ${pct(x.healthLinked)} (${frac(x.healthLinked)}명)`,
  ].join("\n");
}

async function copySummary() {
  if (!state.summary) return;
  const text = summaryText(state.summary);
  try {
    await navigator.clipboard.writeText(text);
    showStatus("복사했습니다. 문서에 붙여 넣으세요.");
  } catch (_) {
    const area = el("textarea", { style: "width:100%;height:220px" }, text);
    $("panel").prepend(area);
    area.select();
    showStatus("자동 복사가 막혀 있어 아래 글을 직접 복사해 주세요.");
  }
}

// ------------------------------------------------------------ 시작

function logout(message) {
  storage("clear");
  state.code = null;
  state.summary = null;
  state.accounts = null;
  $("app").hidden = true;
  $("login").hidden = false;
  $("login-error").hidden = !message;
  $("login-error").textContent = message || "";
  $("code").value = "";
}

function enter(code) {
  state.code = code;
  $("login").hidden = true;
  $("app").hidden = false;
  render();
}

document.addEventListener("DOMContentLoaded", () => {
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const code = $("code").value.trim();
    if (!code) return;
    const response = await fetch("/ops/api/summary", {
      headers: { "X-Ops-Code": code },
      cache: "no-store",
    });
    if (response.status === 401) {
      $("login-error").hidden = false;
      $("login-error").textContent = "접속 코드가 맞지 않습니다";
      return;
    }
    storage("set", code);
    state.summary = response.ok ? await response.json() : null;
    enter(code);
  });
  for (const button of document.querySelectorAll(".tabs button")) {
    button.addEventListener("click", () => {
      state.tab = button.dataset.tab;
      render();
    });
  }
  $("include-internal").addEventListener("change", loadSummary);
  $("since").addEventListener("change", loadSummary);
  $("refresh").addEventListener("click", () => {
    state.accounts = null;
    if (state.tab === "accounts") render();
    else loadSummary();
  });
  $("copy").addEventListener("click", copySummary);
  $("logout").addEventListener("click", () => logout());

  const saved = storage("get");
  if (saved) enter(saved);
  else logout();
});
