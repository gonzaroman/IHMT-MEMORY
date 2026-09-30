/* IHMT interface logic.
 *
 * Two rules worth not breaking when touching this file:
 *
 * 1. Content coming from the memory is ALWAYS inserted with textContent,
 *    never with innerHTML. Anyone may have written those texts and they must
 *    not be able to run anything on this page.
 * 2. Every call to /api/* carries the token; without it the server answers 403.
 */

import { t, setLang, applyStaticStrings, getLang } from "./i18n.js";

const TOKEN = new URLSearchParams(location.search).get("t") || "";

const state = {
  status: null,
  scope: "user",
  preview: null,
  selectedLeaf: null,
  loadedTabs: new Set(),
  // Explore: first-level rows, so they can be filtered without rebuilding the tree
  // (and therefore without losing whatever the user has expanded).
  rootRows: [],
  // Timeline: the data arrives complete and is paginated here.
  timeline: null,
  timelineShown: 0,
};

/** How many domains it takes for the filter to be worth offering. */
const DOMAIN_FILTER_FROM = 8;

/** Timeline groups per page. */
const TIMELINE_PAGE = 50;

/** From how many values an attribute's history gets folded. */
const HISTORY_COLLAPSE_FROM = 4;

/** Normalize for comparison: no case, no accents. */
function fold(text) {
  return (text || "")
    .toLowerCase()
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "");
}

/* ------------------------------------------------------------------- helpers */

/** Create an element with properties and children, without using innerHTML. */
function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") throw new Error("innerHTML is not allowed here");
    else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child == null) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

function badge(text, kind = "") {
  return el("span", { class: `badge ${kind}`.trim(), text });
}

async function api(path, params = {}) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
  }
  const res = await fetch(url, { headers: { "X-IHMT-Token": TOKEN } });
  const data = await res.json().catch(() => ({ error: "bad_response" }));
  if (!res.ok) throw Object.assign(new Error(data.error || "error"), { data });
  return data;
}

async function apiPost(path, body = {}) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "X-IHMT-Token": TOKEN, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({ error: "bad_response" }));
  if (!res.ok) throw Object.assign(new Error(data.error || "error"), { data });
  return data;
}

function errorBox(err) {
  const detail = err?.data?.detail || err?.data?.error || err?.message || "";
  return el("div", { class: "notice" }, [`${t("common.error")}: ${detail}`]);
}

function spinner() {
  return el("div", { class: "spinner", text: t("common.loading") });
}

/* ---------------------------------------------------------------- navigation */

function showTab(name) {
  document.querySelectorAll("nav button").forEach((b) => {
    b.classList.toggle("is-active", b.dataset.tab === name);
  });
  for (const tab of ["setup", "explore", "diagnose", "timeline"]) {
    document.getElementById(`tab-${tab}`).hidden = tab !== name;
  }
  if (!state.loadedTabs.has(name)) {
    state.loadedTabs.add(name);
    if (name === "explore") loadTree();
    if (name === "timeline") loadTimeline();
  }
}

/* ------------------------------------------------------------------ screen 1 */

async function refreshStatus() {
  try {
    state.status = await api("/api/status");
  } catch (err) {
    document.getElementById("home-state").replaceChildren(errorBox(err));
    return;
  }
  const s = state.status;

  const chip = document.getElementById("home-chip");
  chip.textContent = s.memory_home;
  chip.title = s.memory_home;
  document.getElementById("home-input").value = s.memory_home;
  const proyecto = document.getElementById("project-input");
  if (!proyecto.value) proyecto.value = s.project_dir;

  const info = clear(document.getElementById("home-state"));
  const actions = clear(document.getElementById("home-actions"));

  if (s.memory_exists) {
    info.append(badge(t("setup.exists"), "ok"));
    info.append(badge(`${s.stats.leaves} ${t("common.leaves")}`));
    info.append(badge(`${s.stats.nodes} ${t("common.nodes")}`));
    info.append(badge(`${t("common.depth")} ${s.stats.depth}`));
    info.append(badge(`${s.stats.facts} ${t("common.facts")}`));
    if (s.stats.conflicts > 0) {
      info.append(badge(`${s.stats.conflicts} ${t("common.conflicts")}`, "warn"));
    }
  } else {
    info.append(badge(t("setup.missing"), "warn"));
    actions.append(
      el("button", { class: "btn primary", onClick: createMemory, text: t("setup.create") })
    );
  }
  refreshPreview();
}

async function createMemory() {
  const path = document.getElementById("home-input").value.trim();
  try {
    state.status = await apiPost("/api/memory/create", { path });
    state.loadedTabs.delete("explore");
    state.loadedTabs.delete("timeline");
    await refreshStatus();
    document.getElementById("home-actions").replaceChildren(
      el("div", { class: "notice ok" }, [t("setup.created")])
    );
  } catch (err) {
    document.getElementById("home-actions").replaceChildren(errorBox(err));
  }
}

async function useHome() {
  const path = document.getElementById("home-input").value.trim();
  try {
    await apiPost("/api/home", { path });
    state.loadedTabs.delete("explore");
    state.loadedTabs.delete("timeline");
    document.getElementById("folder-browser").hidden = true;
    await refreshStatus();
    await loadSetupState();
  } catch (err) {
    document.getElementById("home-state").replaceChildren(errorBox(err));
  }
}

async function browseNative() {
  if (!state.status?.native_picker) return openFolderBrowser();
  try {
    const res = await apiPost("/api/folders/pick", {
      initial: document.getElementById("home-input").value.trim(),
    });
    if (res.path) {
      document.getElementById("home-input").value = res.path;
      await useHome();
    }
  } catch {
    openFolderBrowser();
  }
}

/** Pick the PROJECT folder (where the .mcp.json goes), not the memory's. */
async function browseProject() {
  const campo = document.getElementById("project-input");
  if (!state.status?.native_picker) return openFolderBrowser(campo.value, campo);
  try {
    const res = await apiPost("/api/folders/pick", { initial: campo.value.trim() });
    if (res.path) {
      campo.value = res.path;
      refreshPreview();
    }
  } catch {
    openFolderBrowser(campo.value, campo);
  }
}

async function openFolderBrowser(path, target) {
  const wrap = document.getElementById("folder-browser");
  wrap.hidden = false;
  const list = clear(document.getElementById("folder-list"));
  list.append(spinner());
  const campo = target || document.getElementById("home-input");
  let data;
  try {
    data = await api("/api/folders", { path: path ?? campo.value });
  } catch (err) {
    list.replaceChildren(errorBox(err));
    return;
  }
  document.getElementById("folder-current").textContent = data.path;
  campo.value = data.path;
  if (campo.id === "project-input") refreshPreview();
  clear(list);
  if (data.parent) {
    list.append(
      el("button", { type: "button", onClick: () => openFolderBrowser(data.parent, campo) }, ["⤴", "…"])
    );
  }
  for (const dir of data.directories) {
    list.append(
      el("button", { type: "button", onClick: () => openFolderBrowser(dir.path, campo) }, [
        dir.is_memory ? "🌳" : "📁",
        el("span", { text: dir.name }),
      ])
    );
  }
  if (!data.directories.length && !data.parent) {
    list.append(el("div", { class: "spinner", text: t("common.empty") }));
  }
}

function selectScope(scope) {
  state.scope = scope;
  document.querySelectorAll(".scope").forEach((b) => {
    b.classList.toggle("is-active", b.dataset.scope === scope);
  });
  // The project only matters in the "this project only" scope: it is the folder
  // the .mcp.json is written to. In the user scope there is no project to
  // choose, so the field would just get in the way.
  document.getElementById("project-picker").hidden = scope !== "project";
  refreshPreview();
}

/** Target project folder, or undefined if the scope does not use one. */
function currentProject() {
  if (state.scope !== "project") return undefined;
  return document.getElementById("project-input").value.trim() || undefined;
}

const WARNING_KEYS = {
  missing_claude_cli: "setup.problemNoCli",
  missing_mcp_sdk: "setup.problemNoSdk",
  keeps_other_servers: "setup.problemKeepsOthers",
  replaces_existing: "setup.problemReplaces",
};

/** Counter used to discard preview responses that arrive late. */
let previewSeq = 0;
let previewTimer = null;

/** Reschedule the preview; avoids one request per keystroke. */
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(refreshPreview, 300);
}

async function refreshPreview() {
  const code = document.getElementById("preview-code");
  const label = document.getElementById("preview-label");
  const warns = clear(document.getElementById("preview-warnings"));
  code.textContent = "";
  const seq = ++previewSeq;
  try {
    const resultado = await apiPost("/api/setup/preview", {
      scope: state.scope,
      memory_home: document.getElementById("home-input").value.trim(),
      project_dir: currentProject(),
    });
    // Typing a path fires several requests in a row and they may come back
    // out of order: without this guard, the screen ends up showing a
    // half-typed folder as if it were the final one.
    if (seq !== previewSeq) return;
    state.preview = resultado;
  } catch (err) {
    if (seq === previewSeq) warns.append(errorBox(err));
    return;
  }
  const p = state.preview;
  label.textContent = p.kind === "file" ? `${t("setup.willWrite")}: ${p.file_path}` : t("setup.willRun");
  code.textContent = p.preview;
  for (const w of p.warnings) {
    const key = WARNING_KEYS[w];
    if (key) warns.append(el("div", { class: "notice" }, [t(key)]));
  }
}

async function applySetup() {
  const box = clear(document.getElementById("apply-result"));
  box.append(spinner());
  try {
    const res = await apiPost("/api/setup/apply", {
      scope: state.scope,
      memory_home: document.getElementById("home-input").value.trim(),
      project_dir: currentProject(),
    });
    clear(box);
    if (res.ok) {
      box.append(
        el("div", { class: "notice ok" }, [
          el("div", {}, [
            el("strong", { text: t("setup.applied") }),
            el("small", { text: t("setup.restart") }),
            res.output ? el("small", { text: res.output }) : null,
          ]),
        ])
      );
    } else {
      box.append(el("div", { class: "notice" }, [res.error || t("common.error")]));
    }
    renderSetupState(res.state);
  } catch (err) {
    box.replaceChildren(errorBox(err));
  }
}

async function copyPreview() {
  if (!state.preview) return;
  try {
    await navigator.clipboard.writeText(state.preview.preview);
    const btn = document.getElementById("btn-copy");
    btn.textContent = t("common.copied");
    setTimeout(() => (btn.textContent = t("common.copy")), 1500);
  } catch {
    /* the browser may block the clipboard; the text stays selectable */
  }
}

/** Look of each verdict: panel colour and symbol. */
const VERDICT_LOOK = {
  WORKING: { tone: "ok", icon: "●" },
  PENDING_APPROVAL: { tone: "warn", icon: "⏸" },
  NOT_REGISTERED: { tone: "bad", icon: "○" },
  SDK_MISSING: { tone: "bad", icon: "○" },
  CLI_MISSING: { tone: "bad", icon: "○" },
  FAILED: { tone: "bad", icon: "✕" },
  UNKNOWN: { tone: "warn", icon: "?" },
};

const VERDICT_TEXT = {
  WORKING: ["verdict.working", "verdict.workingHelp"],
  PENDING_APPROVAL: ["verdict.pending", "verdict.pendingHelp"],
  NOT_REGISTERED: ["verdict.notRegistered", "verdict.notRegisteredHelp"],
  SDK_MISSING: ["verdict.sdkMissing", "verdict.sdkMissingHelp"],
  CLI_MISSING: ["verdict.cliMissing", "verdict.cliMissingHelp"],
  FAILED: ["verdict.failed", "verdict.failedHelp"],
  UNKNOWN: ["verdict.unknown", "verdict.unknownHelp"],
};

const NOTE_KEYS = {
  duplicate_registration: "verdict.noteDuplicate",
  memory_not_created_yet: "verdict.noteNoMemory",
  user_scope_needs_no_approval: "verdict.noteUserScope",
};

const SCOPE_TEXT = {
  project: "verdict.scopeProject",
  user: "verdict.scopeUser",
  local: "verdict.scopeLocal",
};

/**
 * Query the MCP server status.
 *
 * @param {boolean} refrescar Forces the backend to ask the CLI again instead
 *   of reusing its 15 s cache. Essential for the button: approving the
 *   server and pressing it right away used to show the previous state.
 */
async function loadSetupState(refrescar = false) {
  const box = clear(document.getElementById("verdict"));
  box.append(el("div", { class: "spinner", text: t("verdict.checking") }));
  try {
    renderSetupState(await api("/api/setup/state", refrescar ? { refresh: "1" } : {}));
  } catch (err) {
    box.replaceChildren(errorBox(err));
  }
}

/** One step line, with its copyable command if it has one.
 *
 * The content goes inside a div and not directly in the <li>: applying
 * flex to the <li> itself makes the browser stop drawing the step number.
 */
function step(number, label, command) {
  const cuerpo = el("div", { class: "step-body" }, [el("span", { text: label })]);
  const fila = el("li", {}, [cuerpo]);
  if (command) {
    cuerpo.append(el("code", { class: "step-cmd", text: command }));
    cuerpo.append(
      el("button", {
        class: "link-btn",
        type: "button",
        text: t("common.copy"),
        onClick: async (event) => {
          try {
            await navigator.clipboard.writeText(command);
            const boton = event.currentTarget;
            boton.textContent = t("common.copied");
            setTimeout(() => (boton.textContent = t("common.copy")), 1500);
          } catch {
            /* the browser may block the clipboard; the text still shows */
          }
        },
      })
    );
  }
  return fila;
}

/** Concrete steps depending on what is missing. Empty when there is nothing to do. */
function verdictSteps(st) {
  const carpeta = st.registered_home || st.project_dir;
  switch (st.verdict) {
    case "PENDING_APPROVAL":
      return [
        step(1, t("verdict.stepTerminal")),
        step(2, t("verdict.stepCd"), `cd ${carpeta}`),
        step(3, t("verdict.stepRun"), "claude"),
        step(4, t("verdict.stepApprove")),
      ];
    case "SDK_MISSING":
      return [
        step(1, t("verdict.stepTerminal")),
        step(2, t("verdict.stepCd"), `cd ${st.project_dir}`),
        step(
          3,
          t("verdict.stepInstallSdk"),
          "python3 -m venv .venv && .venv/bin/pip install -r requirements-mcp.txt"
        ),
      ];
    case "CLI_MISSING":
      return [step(1, t("verdict.stepInstallCli"))];
    default:
      return [];
  }
}

/**
 * Status panel.
 *
 * Replaces the three green labels from before, which could say "registered"
 * while the server was not approved — that is, not working.
 */
function renderSetupState(st) {
  const box = clear(document.getElementById("verdict"));
  const look = VERDICT_LOOK[st.verdict] || VERDICT_LOOK.UNKNOWN;
  const [titleKey, helpKey] = VERDICT_TEXT[st.verdict] || VERDICT_TEXT.UNKNOWN;
  box.className = `panel verdict tone-${look.tone}`;

  box.append(
    el("div", { class: "verdict-head" }, [
      el("span", { class: "verdict-icon", text: look.icon }),
      el("div", {}, [
        el("h2", { text: t(titleKey) }),
        el("p", { class: "verdict-help", text: t(helpKey) }),
      ]),
    ])
  );

  const chips = el("div", { class: "metrics" });
  if (st.verdict === "WORKING" && st.effective_scope) {
    chips.append(badge(t(SCOPE_TEXT[st.effective_scope] || "common.domain"), "ok"));
  } else if (st.effective_scope) {
    chips.append(badge(t(SCOPE_TEXT[st.effective_scope] || "common.domain")));
  }
  if (st.registered_home) chips.append(badge(t("verdict.memoryHere", st.registered_home)));
  if (chips.childElementCount) box.append(chips);

  const pasos = verdictSteps(st);
  if (pasos.length) {
    box.append(el("p", { class: "note steps-title", text: t("verdict.stepsTitle") }));
    const lista = el("ol", { class: "steps" });
    for (const paso of pasos) lista.append(paso);
    box.append(lista);
  }

  for (const nota of st.notes || []) {
    const clave = NOTE_KEYS[nota];
    if (clave) box.append(el("p", { class: "note", text: t(clave) }));
  }

  const acciones = el("div", { class: "row verdict-actions" }, [
    el("button", {
      class: "btn",
      text: t("verdict.recheck"),
      onClick: () => loadSetupState(true),
    }),
  ]);
  if (st.detail) {
    const detalle = el("pre", { class: "code", hidden: "" }, [st.detail]);
    const alternar = el("button", {
      class: "link-btn",
      type: "button",
      text: t("verdict.detail"),
      onClick: () => {
        const oculto = detalle.hasAttribute("hidden");
        detalle.toggleAttribute("hidden", !oculto);
        alternar.textContent = oculto ? t("verdict.detailHide") : t("verdict.detail");
      },
    });
    acciones.append(alternar);
    box.append(acciones);
    box.append(detalle);
  } else {
    box.append(acciones);
  }
}

/* ------------------------------------------------------------------ screen 2 */

async function loadTree() {
  const box = clear(document.getElementById("tree"));
  const filterRow = document.getElementById("domain-filter-row");
  state.rootRows = [];
  if (!state.status?.memory_exists) {
    filterRow.hidden = true;
    box.append(el("p", { class: "note", text: t("explore.noMemory") }));
    return;
  }
  box.append(spinner());
  try {
    const data = await api("/api/tree");
    const ul = el("ul");
    for (const child of data.children) {
      const li = buildItem(child);
      state.rootRows.push({ li, key: fold(`${child.label} ${child.title}`) });
      ul.append(li);
    }
    clear(box).append(ul);
    // The filter only appears when the list is long: with six domains it
    // would get in the way more than it helps.
    filterRow.hidden = data.children.length < DOMAIN_FILTER_FROM;
    applyDomainFilter();
  } catch (err) {
    box.replaceChildren(errorBox(err));
  }
  document.getElementById("leaf-view").replaceChildren(
    el("p", { class: "note", text: t("explore.selectLeaf") })
  );
}

/** Hide the first-level rows that do not match, without touching the rest of the tree. */
function applyDomainFilter() {
  const term = fold(document.getElementById("domain-filter").value.trim());
  let visibles = 0;
  for (const row of state.rootRows) {
    const encaja = !term || row.key.includes(term);
    row.li.hidden = !encaja;
    if (encaja) visibles += 1;
  }
  const aviso = document.getElementById("domain-filter-empty");
  aviso.hidden = visibles > 0 || !term;
  if (!aviso.hidden) {
    aviso.textContent = t("explore.noDomains", document.getElementById("domain-filter").value.trim());
  }
}

function buildBranch(children) {
  const ul = el("ul");
  for (const child of children) ul.append(buildItem(child));
  return ul;
}

function buildItem(item) {
  const li = el("li");
  const twist = el("span", { class: "twist", text: item.has_children ? "▸" : "·" });
  const button = el(
    "button",
    {
      class: `node kind-${item.kind}`,
      type: "button",
      title: item.subtitle || item.title,
    },
    [
      twist,
      el("span", { class: "label", text: item.label || item.title || item.id }),
      item.kind === "leaf"
        ? el("span", { class: "count", text: item.date || "" })
        : el("span", { class: "count", text: `${item.leaf_count}` }),
    ]
  );

  let expanded = false;
  let childHolder = null;

  button.addEventListener("click", async () => {
    document.querySelectorAll(".tree .node").forEach((n) => n.classList.remove("is-selected"));
    button.classList.add("is-selected");

    if (item.kind === "leaf") {
      openLeaf(item.id);
      return;
    }
    if (expanded) {
      childHolder?.remove();
      childHolder = null;
      expanded = false;
      twist.textContent = "▸";
      return;
    }
    twist.textContent = "▾";
    expanded = true;
    childHolder = el("div", {}, [spinner()]);
    li.append(childHolder);
    try {
      const data = await api("/api/tree", { id: item.id });
      clear(childHolder).append(buildBranch(data.children));
    } catch (err) {
      clear(childHolder).append(errorBox(err));
    }
  });

  li.append(button);
  return li;
}

async function openLeaf(id) {
  const view = clear(document.getElementById("leaf-view"));
  view.append(spinner());
  let leaf;
  try {
    leaf = await api("/api/leaf", { id });
  } catch (err) {
    view.replaceChildren(errorBox(err));
    return;
  }
  state.selectedLeaf = leaf;
  clear(view);

  view.append(el("h3", { text: leaf.title }));

  const meta = el("div", { class: "leaf-meta" }, [
    badge(leaf.timestamp.slice(0, 10) || "—"),
    badge(leaf.domain, "accent"),
    badge(leaf.data_type),
    badge(`${leaf.tokens} ${t("explore.tokens")}`),
    badge(`${leaf.span.start_line}–${leaf.span.end_line} ${t("explore.lines")}`),
  ]);
  if (leaf.oversized) meta.append(badge("oversized", "warn"));
  view.append(meta);

  for (const notice of leaf.notices) {
    view.append(
      el("div", { class: "notice" }, [
        el("div", {}, [
          el("strong", { text: t("explore.superseded") }),
          el("small", { text: notice }),
          el("small", { text: `(${t("explore.noticeOrigin")})` }),
        ]),
      ])
    );
  }

  if (leaf.context) {
    view.append(el("p", { class: "note", text: `${t("explore.context")}: ${leaf.context}` }));
  }

  view.append(el("div", { class: "leaf-body", text: leaf.content }));
  if (leaf.truncated) view.append(el("p", { class: "note", text: t("explore.truncated") }));

  view.append(el("p", { class: "note", text: `${t("common.source")}: ${leaf.source}` }));
  view.append(el("p", { class: "note", text: `${t("common.tags")}: ${leaf.tags.join(" · ")}` }));
  view.append(el("p", { class: "note", text: t("common.path") }));
  view.append(el("p", { class: "breadcrumb", text: leaf.path.join("  →  ") }));
}

/* ------------------------------------------------------------------ screen 3 */

async function runSearch(clueOverride) {
  const box = clear(document.getElementById("search-results"));
  const query = document.getElementById("q").value.trim();
  if (!query) return;
  if (clueOverride !== undefined) document.getElementById("clue").value = clueOverride;
  const clue = document.getElementById("clue").value.trim();

  box.append(spinner());
  let data;
  try {
    data = await api("/api/search", { q: query, clue });
  } catch (err) {
    box.replaceChildren(errorBox(err));
    return;
  }
  clear(box);

  box.append(
    el("div", { class: "metrics" }, [
      badge(`${t("diagnose.confidence")} ${data.confidence.toFixed(2)}`, data.confidence >= 0.45 ? "ok" : "warn"),
      badge(`${data.node_reads + data.leaf_reads} ${t("diagnose.opened")} ${t("diagnose.of")} ${data.total_files}`),
      badge(`${t("common.depth")} ${data.depth}`),
    ])
  );

  if (data.clue_request) {
    box.append(
      el("div", { class: "notice" }, [
        el("div", {}, [
          el("strong", { text: t("diagnose.ambiguous") }),
          el("small", { text: t("diagnose.ambiguousHelp") }),
        ]),
      ])
    );
    for (const option of data.clue_request.options) {
      // A useful clue is words, not the date the excerpt starts with:
      // "vacaciones en Benidorm" tells things apart; "2024-07-22" just repeats the index.
      const trozos = (option.hint || "")
        .split(/\s+/)
        .filter((w) => !/^\d{4}-\d{2}-\d{2}$/.test(w) && !/^[\d.,;:—·-]+$/.test(w))
        .slice(0, 4);
      // A clue should not end in a preposition ("Vacaciones en Benidorm con").
      while (trozos.length > 1 && trozos[trozos.length - 1].replace(/[.,;:]$/, "").length <= 3) {
        trozos.pop();
      }
      const words = trozos.join(" ").replace(/[.,;:]$/, "");
      box.append(
        el("div", { class: "result" }, [
          el("h3", { text: option.title }),
          el("p", { class: "excerpt", text: option.hint }),
          el("button", {
            class: "btn",
            text: `${t("diagnose.tryClue")}: ${words}`,
            onClick: () => runSearch(words),
          }),
        ])
      );
    }
    return;
  }

  if (!data.results.length) {
    box.append(el("p", { class: "note", text: t("diagnose.noResults") }));
    return;
  }

  for (const r of data.results) {
    const item = el("div", { class: "result" }, [
      el("h3", { text: r.title }),
      el("div", { class: "leaf-meta" }, [
        badge(r.date || "—"),
        badge(r.domain, "accent"),
        badge(`score ${r.score.toFixed(2)}`),
      ]),
      el("p", { class: "excerpt", text: r.excerpt }),
      el("p", { class: "breadcrumb", text: r.path.join("  →  ") }),
    ]);
    for (const notice of r.notices) {
      item.append(el("div", { class: "notice" }, [notice]));
    }
    box.append(item);
  }
}

/* ------------------------------------------------------------------ screen 4 */

async function loadTimeline() {
  const conflicts = clear(document.getElementById("conflicts"));
  clear(document.getElementById("facts"));
  if (!state.status?.memory_exists) {
    conflicts.append(el("p", { class: "note", text: t("explore.noMemory") }));
    return;
  }
  conflicts.append(spinner());

  try {
    state.timeline = await api("/api/timeline");
  } catch (err) {
    conflicts.replaceChildren(errorBox(err));
    return;
  }

  clear(conflicts).append(el("h2", { text: t("timeline.conflictsHeading") }));
  if (!state.timeline.conflicts.length) {
    conflicts.append(el("p", { class: "note", text: t("timeline.noConflicts") }));
  }
  for (const c of state.timeline.conflicts) {
    conflicts.append(
      el("div", { class: "notice" }, [
        el("div", {}, [
          el("strong", { text: `${c.subject} · ${c.attribute}` }),
          el("small", { text: c.notice }),
          el("small", { text: `(${t("explore.noticeOrigin")})` }),
        ]),
      ])
    );
  }

  state.timelineShown = TIMELINE_PAGE;
  renderTimeline();
}

/** Groups that pass the current filter. */
function filteredFacts() {
  const term = fold(document.getElementById("timeline-filter").value.trim());
  const todos = state.timeline?.facts || [];
  if (!term) return todos;
  return todos.filter((f) => fold(`${f.subject} ${f.attribute}`).includes(term));
}

/**
 * Draw the timeline: only the groups visible so far.
 *
 * The data arrives complete from the server and is trimmed here. What hurts
 * with thousands of facts is drawing thousands of rows, not moving them over
 * the loopback; if the response itself ever became the problem, the backend
 * would have to paginate too.
 */
function renderTimeline() {
  const caja = clear(document.getElementById("facts"));
  const masCaja = clear(document.getElementById("timeline-more"));
  const contador = document.getElementById("timeline-count");
  const escrito = document.getElementById("timeline-filter").value.trim();

  const total = (state.timeline?.facts || []).length;
  if (!total) {
    contador.textContent = "";
    caja.append(el("p", { class: "note", text: t("timeline.noFacts") }));
    return;
  }

  const coincidencias = filteredFacts();
  if (!coincidencias.length) {
    contador.textContent = "";
    caja.append(el("p", { class: "note", text: t("timeline.noMatches", escrito) }));
    return;
  }

  const visibles = coincidencias.slice(0, state.timelineShown);
  contador.textContent = t("timeline.showing", visibles.length, coincidencias.length);
  for (const fact of visibles) caja.append(buildFact(fact));

  const restantes = coincidencias.length - visibles.length;
  if (restantes > 0) {
    masCaja.append(
      el("button", {
        class: "btn",
        text: t("timeline.loadMore", restantes),
        onClick: () => {
          state.timelineShown += TIMELINE_PAGE;
          renderTimeline();
        },
      })
    );
  }
}

/** A value row inside an attribute's history. */
function buildValue(v, { origin = false } = {}) {
  const historical = v.status === "HISTORICAL";
  const fila = el("div", { class: `fact-value ${historical ? "historical" : ""}` }, [
    el("span", { class: "when", text: v.date || "—" }),
    el("span", { class: "what", text: v.value }),
    badge(historical ? t("timeline.historical") : t("timeline.active"), historical ? "" : "ok"),
  ]);
  if (origin) fila.append(badge(t("timeline.origin")));
  return fila;
}

/**
 * An attribute with its history.
 *
 * With many revisions, what matters is the current value and where it comes
 * from: the first (the origin) and the last (the active one) stay visible,
 * and the ones in between are folded behind a button.
 */
function buildFact(fact) {
  const values = el("div", { class: "fact-values" });
  const total = fact.values.length;

  if (total < HISTORY_COLLAPSE_FROM) {
    fact.values.forEach((v, i) => values.append(buildValue(v, { origin: total > 1 && i === 0 })));
  } else {
    const intermedios = fact.values.slice(1, -1);
    const contenedor = el("div", { class: "fact-middle", hidden: "" });
    for (const v of intermedios) contenedor.append(buildValue(v));

    const alternar = el("button", {
      class: "link-btn",
      type: "button",
      text: t("timeline.showMiddle", intermedios.length),
      onClick: () => {
        const oculto = contenedor.hasAttribute("hidden");
        contenedor.toggleAttribute("hidden", !oculto);
        alternar.textContent = oculto
          ? t("timeline.hideMiddle")
          : t("timeline.showMiddle", intermedios.length);
      },
    });

    values.append(buildValue(fact.values[0], { origin: true }));
    values.append(alternar);
    values.append(contenedor);
    values.append(buildValue(fact.values[total - 1]));
  }

  return el("div", { class: "fact" }, [
    el("div", { class: "fact-key", text: `${fact.subject} · ${fact.attribute}` }),
    values,
  ]);
}

/* ------------------------------------------------------------------- startup */

function wire() {
  document.querySelectorAll("nav button").forEach((b) => {
    b.addEventListener("click", () => showTab(b.dataset.tab));
  });
  document.querySelectorAll("[data-lang-btn]").forEach((b) => {
    b.addEventListener("click", () => setLang(b.dataset.langBtn));
  });
  document.querySelectorAll(".scope").forEach((b) => {
    b.addEventListener("click", () => selectScope(b.dataset.scope));
  });

  document.getElementById("btn-browse").addEventListener("click", browseNative);
  document.getElementById("btn-use-home").addEventListener("click", useHome);
  document.getElementById("btn-browse-project").addEventListener("click", browseProject);
  document.getElementById("project-input").addEventListener("input", schedulePreview);
  document.getElementById("btn-apply").addEventListener("click", applySetup);
  document.getElementById("btn-copy").addEventListener("click", copyPreview);
  document.getElementById("btn-search").addEventListener("click", () => runSearch());
  document.getElementById("q").addEventListener("keydown", (e) => {
    if (e.key === "Enter") runSearch();
  });
  document.getElementById("clue").addEventListener("keydown", (e) => {
    if (e.key === "Enter") runSearch();
  });

  document.getElementById("domain-filter").addEventListener("input", applyDomainFilter);
  document.getElementById("timeline-filter").addEventListener("input", () => {
    // Changing the filter goes back to the first page: otherwise a filter that
    // leaves few results would inherit a meaningless "showing 150 of 3".
    state.timelineShown = TIMELINE_PAGE;
    renderTimeline();
  });

  document.addEventListener("ihmt:lang", () => {
    state.loadedTabs.clear();
    refreshStatus();
    loadSetupState();
    const active = document.querySelector("nav button.is-active")?.dataset.tab || "setup";
    state.loadedTabs.add("setup");
    showTab(active);
  });
}

async function boot() {
  document.documentElement.lang = getLang();
  applyStaticStrings();
  wire();
  state.loadedTabs.add("setup");
  await refreshStatus();
  await loadSetupState();
}

boot();
