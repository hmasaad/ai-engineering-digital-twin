const TITLES = {
  overview: ["Overview", "What the twin believes right now"],
  map: ["System map", "Modules, imports, and the shape of a change"],
  time: ["Time", "The system, and the relations that held, as of a chosen day"],
  sync: ["Sync", "Keep the virtual twin consistent with the repository"],
  risks: ["Risk radar", "Bug, incident, delay, and performance indexes"],
  change: ["Change lab", "Evaluate a proposal before it lands"],
  scenarios: ["Scenarios", "Run a decision against the model"],
  whatif: ["What if", "Counterfactual simulation of decisions that were not taken"],
  council: ["Council", "Specialized agents, sequenced into one plan"],
  learn: ["Learning", "Record outcomes and update beliefs"],
};

const SCENARIOS = [
  {
    name: "Characterize payment retries",
    blurb: "Assume tests land on the retry worker.",
    interventions: [{ action: "add_tests", module: "services/payments/retry.py" }],
  },
  {
    name: "Break the checkout cycle",
    blurb: "Pull the shared rule out of the inventory and checkout cycle.",
    interventions: [{ action: "extract_module", module: "services/inventory/stock.py" }],
  },
  {
    name: "Add a payments owner",
    blurb: "Put a second owner on the retry worker.",
    interventions: [{ action: "add_owner", module: "services/payments/retry.py" }],
  },
  {
    name: "Circuit breaker on retries",
    blurb: "Assume a breaker and a retry budget across payments.",
    interventions: [{ action: "add_circuit_breaker", module: "services/payments/retry.py" }],
  },
  {
    name: "Freeze retry changes",
    blurb: "Stop the file moving. Defect pressure falls and delay rises.",
    interventions: [{ action: "freeze", module: "services/payments/retry.py" }],
  },
];

const COMPARE = [
  ["expected_bug_modules", "Expected bug-bearing modules"],
  ["expected_incident_services", "Expected incident-bearing services"],
  ["delay", "Mean delay index"],
  ["perf", "Mean performance index"],
  ["debt", "Mean debt index"],
  ["cycles", "Import cycles"],
];

const state = {
  twin: null,
  view: "overview",
  selected: "services/payments/retry.py",
  asOf: 0,
  drift: null,
  kind: "bug",
  modelKind: "bug",
  picked: new Set(),
  change: null,
  scenario: null,
  counterfactual: null,
  cfModule: "",
  error: "",
  toast: "",
  busy: false,
};

const viewEl = () => document.getElementById("view");

document.getElementById("app").addEventListener("click", onClick);
document.getElementById("app").addEventListener("change", onChange);
document.getElementById("scan-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const path = new FormData(event.target).get("path");
  if (!path) return;
  run(async () => {
    state.twin = await api("/api/scan", { method: "POST", body: JSON.stringify({ path }) });
    state.view = "overview";
    state.toast = `Scanned ${state.twin.project.name}.`;
    state.change = null;
    state.scenario = null;
    state.drift = null;
  });
});
document.getElementById("rescan").addEventListener("click", () => {
  run(async () => {
    const path = state.twin?.project?.root || null;
    state.twin = await api("/api/scan", { method: "POST", body: JSON.stringify({ path }) });
    state.toast = "Rescanned the current repository.";
    state.drift = null;
  });
});

boot();

async function boot() {
  try {
    state.twin = await api("/api/twin");
  } catch (error) {
    state.error = error.message;
  }
  render();
}

async function run(work) {
  if (state.busy) return;
  state.busy = true;
  state.error = "";
  render();
  try {
    await work();
  } catch (error) {
    state.error = error.message || "Request failed";
  } finally {
    state.busy = false;
    render();
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    const text = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((item) => item.msg).join("; ") : response.statusText;
    throw new Error(text || "Request failed");
  }
  return data;
}

function onClick(event) {
  const button = event.target.closest("[data-view], [data-action]");
  if (!button || state.busy && button.dataset.action) return;
  if (button.dataset.view) {
    state.view = button.dataset.view;
    render();
    return;
  }
  const action = button.dataset.action;
  if (action === "asof") {
    state.asOf = Number(button.dataset.age);
    state.view = "time";
    render();
  } else if (action === "open") {
    state.selected = button.dataset.path;
    state.view = "map";
    render();
  } else if (action === "kind") {
    state.kind = button.dataset.kind;
    render();
  } else if (action === "model") {
    state.modelKind = button.dataset.kind;
    render();
  } else if (action === "preset-change") {
    state.picked = new Set(["services/payments/retry.py"]);
    state.view = "change";
    evaluate("Change retry backoff");
  } else if (action === "evaluate") {
    evaluate("Proposed change");
  } else if (action === "scenario") {
    const preset = SCENARIOS[Number(button.dataset.index)];
    runScenario(preset);
  } else if (action === "sync-check") {
    run(async () => {
      state.drift = await api("/api/sync");
      state.view = "sync";
      state.toast = state.drift.summary;
    });
  } else if (action === "sync-apply") {
    run(async () => {
      const body = await api("/api/sync", { method: "POST", body: "{}" });
      state.twin = body.twin;
      state.drift = null;
      state.view = "sync";
      state.toast = body.sync ? body.sync.summary : "Virtual state updated.";
    });
  } else if (action === "cf-run") {
    const module = state.cfModule;
    run(async () => {
      if (!module) throw new Error("Choose a module.");
      state.counterfactual = await api("/api/counterfactuals", {
        method: "POST",
        body: JSON.stringify({ module }),
      });
      state.view = "whatif";
    });
  } else if (action === "outcome") {
    recordOutcome(button.dataset.path, button.dataset.happened === "1");
  }
}

function onChange(event) {
  const input = event.target;
  if (input.dataset.action === "cf-module") {
    state.cfModule = input.value;
    return;
  }
  if (input.dataset.action === "pick") {
    if (input.checked) state.picked.add(input.value);
    else state.picked.delete(input.value);
  }
}

function evaluate(title) {
  const modules = [...state.picked];
  run(async () => {
    if (!modules.length) throw new Error("Select at least one module.");
    state.change = await api("/api/changes/evaluate", {
      method: "POST",
      body: JSON.stringify({ title, modules }),
    });
    state.view = "change";
  });
}

function runScenario(preset) {
  run(async () => {
    state.scenario = await api("/api/scenarios/run", {
      method: "POST",
      body: JSON.stringify({ name: preset.name, interventions: preset.interventions }),
    });
    state.view = "scenarios";
  });
}

function recordOutcome(module, happened) {
  run(async () => {
    const data = await api("/api/outcomes", {
      method: "POST",
      body: JSON.stringify({ module, kind: "bug", happened }),
    });
    state.twin = data.twin;
    state.change = null;
    state.scenario = null;
    const top = data.learning.deltas[0];
    state.toast = top
      ? `${data.learning.kind} on ${short(module)}: ${top.label} ${top.before.toFixed(2)} → ${top.after.toFixed(2)}.`
      : "Recorded. The weights barely moved.";
    state.view = "learn";
  });
}

function render() {
  const scroller = viewEl();
  const y = scroller.scrollTop;
  document.querySelectorAll("nav button").forEach((button) => {
    if (button.dataset.view === state.view) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  const [title, subtitle] = TITLES[state.view];
  document.getElementById("title").textContent = title;
  document.getElementById("subtitle").textContent = subtitle;
  const twin = state.twin;
  document.getElementById("crumb").textContent = twin ? twin.project.name : "Twin";
  const pill = document.getElementById("health-pill");
  if (twin) {
    const tone = twin.health >= 70 ? "good" : twin.health >= 45 ? "warn" : "bad";
    pill.className = `health-pill ${tone}`;
    pill.innerHTML = `<strong>${twin.health}</strong> health`;
  } else {
    pill.className = "health-pill";
    pill.textContent = "—";
  }
  const badge = document.getElementById("badge-council");
  badge.textContent = twin ? String(twin.kpis.critical) : "";
  badge.hidden = !twin || !twin.kpis.critical;
  const error = document.getElementById("error");
  error.hidden = !state.error;
  error.textContent = state.error;
  const toast = document.getElementById("toast");
  toast.hidden = !state.toast;
  toast.textContent = state.toast;
  document.querySelectorAll("button").forEach((button) => {
    if (button.id === "rescan" || button.type === "submit" || button.dataset.action || button.dataset.view) {
      button.disabled = state.busy;
    }
  });
  scroller.innerHTML = twin ? VIEWS[state.view](twin) : empty();
  scroller.scrollTop = y;
  const form = document.getElementById("event-form");
  if (form) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const data = new FormData(form);
      run(async () => {
        state.twin = await api("/api/events", {
          method: "POST",
          body: JSON.stringify({
            type: "incident",
            service: data.get("service"),
            severity: data.get("severity"),
            summary: data.get("summary") || "Recorded from the twin",
            age_days: 0,
          }),
        });
        state.toast = "Incident folded into the model.";
        state.view = "learn";
      });
    });
  }
}

const VIEWS = {
  overview: renderOverview,
  map: renderMap,
  time: renderTime,
  sync: renderSync,
  risks: renderRisks,
  change: renderChange,
  scenarios: renderScenarios,
  whatif: renderWhatIf,
  council: renderCouncil,
  learn: renderLearn,
};

function renderSync(twin) {
  const pending = state.drift && !state.drift.applied ? state.drift : null;
  const last = twin.sync || null;
  const rows = (pending || last || { events: [] }).events || [];
  const virtual = (pending || last || { virtual_only: [] }).virtual_only || [];
  const status = pending ? pending.status : last ? last.status : "unknown";
  const summary = pending ? pending.summary : last ? last.summary : "No synchronization yet.";
  return `
    <p class="narrative">The virtual twin is the last state Twin accepted. A check reads the repository and does not change that belief. Apply folds the difference in as events, so the virtual structure matches the real one and the temporal graph records the reconciliation.</p>
    <div class="actions" style="display:flex; gap:8px; margin-bottom:16px">
      <button type="button" class="primary" data-action="sync-check">Check repository</button>
      ${pending && pending.status === "drifted" ? `<button type="button" data-action="sync-apply">Apply ${pending.event_count} ${pending.event_count === 1 ? "event" : "events"}</button>` : ""}
    </div>
    <div class="cols">
      <section class="panel">
        <p class="kicker">${pending ? "Pending drift" : "Last reconciliation"} · ${esc(status)}</p>
        <p>${esc(summary)}</p>
        ${last && !pending ? `<p class="muted">Accepted ${esc(last.checked_at)}.</p>` : ""}
        <div class="list">${rows.slice(0, 12).map((event) => `<p class="path">${esc(event.kind)} · ${esc(event.summary)}</p>`).join("") || "<p class='muted'>Nothing to apply.</p>"}</div>
      </section>
      <section class="panel">
        <p class="kicker">Recorded only on the twin</p>
        <p class="muted">These events are part of the virtual state. They are not a repository change, and a sync leaves them in place.</p>
        <div class="list">${virtual.slice(0, 8).map((event) => `<p class="path">${esc(event.summary)}</p>`).join("") || "<p class='muted'>No virtual-only events.</p>"}</div>
      </section>
    </div>`;
}

function renderTime(twin) {
  const dynamics = twin.dynamics;
  if (!dynamics || !dynamics.slices.length) {
    return "<p class='muted'>No history to replay. Add commits or a .twin/history.jsonl file.</p>";
  }
  const slice = dynamics.slices.reduce((best, item) =>
    Math.abs(item.as_of - state.asOf) < Math.abs(best.as_of - state.asOf) ? item : best
  );
  const facts = dynamics.facts.filter((fact) => factHolds(fact, slice.as_of));
  const imports = facts.filter((fact) => fact.predicate === "imports");
  const events = facts.filter((fact) => fact.kind === "event").slice(0, 8);
  const modules = slice.graph.nodes.map((node) => ({
    path: node.path,
    service: node.service,
    role: "code",
    risks: { bug: node.bug },
  }));
  const edges = slice.graph.imports.map((edge) => ({ ...edge, cross: false }));
  return `
    <p class="narrative">${esc(dynamics.note)}</p>
    <div class="time-chart">${dynamics.slices.map((item) => `<button type="button" data-action="asof" data-age="${item.as_of}" aria-pressed="${item.as_of === slice.as_of}"><span class="bar" style="height:${Math.max(8, Math.round(item.bug * 100))}%"></span><span>${esc(item.label)}</span></button>`).join("")}</div>
    <div class="time-layout">
      <section>
        <p class="kicker">${esc(slice.label)} · ${slice.modules} modules · ${slice.edges} imports · state ${slice.state_index}</p>
        <p>${esc(slice.change)}</p>
        <p class="muted">Top bug index ${fmt(slice.bug)} on ${esc(slice.top_module || "none")}. Incident index ${fmt(slice.incident)}. Authors: ${esc(slice.authors.join(", ") || "none recorded")}.</p>
        ${modules.length ? drawMap(modules, edges, slice.top_module) : "<p class='muted'>No code modules had been committed yet.</p>"}
      </section>
      <section class="panel">
        <p class="kicker">Relations true at this day</p>
        <p class="muted">${imports.length} import intervals · ${facts.filter((fact) => fact.predicate === "exists").length} modules in existence.</p>
        <div class="list">${imports.slice(0, 8).map((fact) => `<p class="path">imports · ${esc(short(fact.subject))} → ${esc(short(fact.object))}<br/><span class="faint">since ${fact.from_age}d ago</span></p>`).join("")}</div>
        <p class="kicker" style="margin-top:14px">Events in the 20 days leading here</p>
        <div class="list">${events.map((fact) => `<p class="path">${esc(fact.predicate)} · ${esc(fact.summary)}<br/><span class="faint">${fact.at_age}d ago</span></p>`).join("") || "<p class='muted'>None in this 20-day window.</p>"}</div>
      </section>
    </div>`;
}

function factHolds(fact, asOf) {
  if (fact.kind === "event") return fact.at_age >= asOf && fact.at_age < asOf + 20;
  if (asOf > fact.from_age + 0.001) return false;
  if (fact.to_age != null && asOf < fact.to_age - 0.001) return false;
  return true;
}

function renderOverview(twin) {
  const bugs = twin.risks.filter((risk) => risk.kind === "bug").slice(0, 5);
  const steps = [
    ["Observe", `${twin.kpis.modules} modules · ${twin.timeline.length} recent events`],
    ["Understand", `${twin.kpis.services} services · ${twin.kpis.edges} imports`],
    ["Predict", `bug ${fmt(twin.kpis.bug)} · incident ${fmt(twin.kpis.incident)}`],
    ["Simulate", "change lab and scenarios"],
    ["Coordinate", `${twin.plan.length} sequenced steps`],
    ["Learn", `${twin.calibration.n} recorded outcomes`],
  ];
  return `
    <section class="hero">
      <article class="stat hero-health"><span class="kicker">Health</span><b>${twin.health}</b></article>
      ${stat("Bug", twin.kpis.bug)}
      ${stat("Incident", twin.kpis.incident)}
      ${stat("Delay", twin.kpis.delay)}
      ${stat("Perf", twin.kpis.perf)}
      ${stat("Debt", twin.kpis.debt)}
    </section>
    <p class="narrative">${esc(twin.narrative)}</p>
    <div class="loop">${steps.map(([name, fact]) => `<div><strong>${name}</strong><span>${esc(fact)}</span></div>`).join("")}</div>
    <div class="chips">${twin.services.map((service) => `<button type="button" class="chip" data-action="open" data-path="${esc(topModule(twin, service.name))}">${esc(service.name)} · bug ${fmt(service.bug)} · bus ${service.bus_factor || "—"}</button>`).join("")}</div>
    <div class="split">
      <section class="panel">
        <p class="kicker">Sharpest bug indexes</p>
        <div class="list">${bugs.map(riskRow).join("") || "<p class='muted'>No code modules.</p>"}</div>
      </section>
      <section class="panel">
        <p class="kicker">Recent engineering</p>
        <div class="list">${twin.timeline.map((event) => `<div class="row" style="grid-template-columns: 1fr"><span class="path">${esc(age(event.age_days))} · ${esc(event.type)}</span><span>${esc(event.summary)}</span></div>`).join("")}</div>
      </section>
    </div>
    <section class="panel" style="margin-top:16px">
      <p class="kicker">Council, first moves</p>
      ${twin.plan.slice(0, 3).map(stepCard).join("") || "<p class='muted'>No open steps.</p>"}
      <p class="footnote">Draws from the current indexes, ${twin.kpis.spread.trials} trials: bug-bearing modules p50 ${twin.kpis.spread.bug_p50}, p90 ${twin.kpis.spread.bug_p90}. These are decision-support indexes, not a guaranteed failure rate.</p>
    </section>`;
}

function renderMap(twin) {
  const code = twin.modules.filter((module) => module.role === "code");
  const selected = code.find((module) => module.path === state.selected) || code[0];
  return `<div class="map-layout"><div>${drawMap(code, twin.edges, selected && selected.path)}<div class="list" style="margin-top:12px">${code
    .slice()
    .sort((a, b) => b.risks.bug - a.risks.bug)
    .map((module) => `<button type="button" class="module-btn" data-action="open" data-path="${esc(module.path)}"><span><strong>${esc(short(module.path))}</strong><span class="path"> ${esc(module.path)}</span></span><span class="mono">${fmt(module.risks.bug)}</span></button>`)
    .join("")}</div></div><aside class="panel detail">${selected ? detail(selected, twin) : "<p class='muted'>No modules.</p>"}</aside></div>`;
}

function renderRisks(twin) {
  const kinds = ["bug", "incident", "delay", "perf"];
  const rows = twin.risks.filter((risk) => state.kind === "all" || risk.kind === state.kind);
  return `
    <div class="chips">
      ${["all", ...kinds].map((kind) => `<button type="button" class="chip" data-action="kind" data-kind="${kind}" aria-pressed="${state.kind === kind}">${kind}</button>`).join("")}
    </div>
    <div class="list">${rows.map(riskRow).join("")}</div>`;
}

function renderChange(twin) {
  const code = twin.modules.filter((module) => module.role === "code").slice().sort((a, b) => b.risks.bug - a.risks.bug);
  const result = state.change;
  return `
    <div class="cols">
      <section class="panel">
        <p class="kicker">Modules in the proposal</p>
        <div class="checks">${code.map((module) => `<label><input type="checkbox" data-action="pick" value="${esc(module.path)}" ${state.picked.has(module.path) ? "checked" : ""}/><span><span class="path">${esc(module.path)}</span><br/><span class="faint">bug ${fmt(module.risks.bug)}</span></span></label>`).join("")}</div>
        <div class="actions" style="margin-top:12px">
          <button type="button" class="primary" data-action="evaluate">Evaluate selected</button>
          <button type="button" data-action="preset-change">Evaluate the retry worker</button>
        </div>
      </section>
      <section class="panel">${result ? changeResult(result) : "<p class='kicker'>No evaluation yet</p><p class='muted'>Pick the files a change would touch. The lab walks importers out to depth 3 and reads the council findings on that set.</p>"}</section>
    </div>`;
}

function renderScenarios() {
  const result = state.scenario;
  return `
    <div class="list" style="margin-bottom:16px">${SCENARIOS.map((preset, index) => `<button type="button" class="preset ${result && result.name === preset.name ? "active" : ""}" data-action="scenario" data-index="${index}"><strong>${esc(preset.name)}</strong><br/><span class="muted">${esc(preset.blurb)}</span></button>`).join("")}</div>
    ${result ? scenarioResult(result) : "<p class='muted'>Choose a hypothetical. The repository is not edited.</p>"}`;
}

function renderWhatIf(twin) {
  const code = twin.modules.filter((module) => module.role === "code").slice().sort((a, b) => b.risks.bug - a.risks.bug);
  if (!code.some((module) => module.path === state.cfModule)) state.cfModule = code[0] ? code[0].path : "";
  const result = state.counterfactual && state.counterfactual.module === state.cfModule ? state.counterfactual : null;
  const options = code.map((module) => `<option value="${esc(module.path)}" ${module.path === state.cfModule ? "selected" : ""}>${esc(short(module.path))} · bug ${fmt(module.risks.bug)}</option>`).join("");
  return `
    <p class="narrative">The factual world is the system as it is. Each alternative is applied on its own, not stacked, and the repository is not edited. Preferred means the lowest combined movement in bug, incident, delay, and cycles.</p>
    <div class="actions" style="display:flex; gap:8px; margin-bottom:16px; flex-wrap:wrap">
      <select data-action="cf-module">${options}</select>
      <button type="button" class="primary" data-action="cf-run">Compare decisions</button>
    </div>
    ${result ? counterfactualResult(result) : "<p class='muted'>Choose a module and compare the decision that happened with the ones that did not.</p>"}`;
}

function counterfactualResult(result) {
  const factual = result.factual;
  const cards = result.alternatives.map((item) => `
    <article class="preset cf-card ${item.preferred ? "preferred" : ""}">
      <strong>${esc(item.title)}</strong> ${item.preferred ? '<span class="tag">preferred</span>' : ""}
      <p>${esc(item.contrary_to)}</p>
      <p class="mono">bug ${fmt(item.local.bug)} <span class="${deltaTone(item.local.bug - factual.bug)}">${fmtDelta(item.local.bug - factual.bug)}</span> · incident ${fmt(item.local.incident)} <span class="${deltaTone(item.local.incident - factual.incident)}">${fmtDelta(item.local.incident - factual.incident)}</span> · delay ${fmt(item.local.delay)} <span class="${deltaTone(item.local.delay - factual.delay)}">${fmtDelta(item.local.delay - factual.delay)}</span></p>
      <p class="muted">${esc(item.tradeoff)}</p>
    </article>`).join("");
  return `
    <section class="panel" style="margin-bottom:16px">
      <p class="kicker">What happened · ${esc(short(result.module))}</p>
      <p>${esc(factual.summary)}</p>
      <p class="mono">bug ${fmt(factual.bug)} · incident ${fmt(factual.incident)} · delay ${fmt(factual.delay)} · cycles ${factual.metrics.cycles}</p>
      <p>${esc(result.why)}</p>
    </section>
    <div class="list">${cards}</div>`;
}

function renderCouncil(twin) {
  const groups = twin.roster.map((agent) => {
    const items = twin.findings.filter((finding) => finding.agent === agent.name);
    return `<section class="agent-block"><p class="kicker">${esc(agent.name)} · ${esc(agent.mandate)}</p>${items.length ? items.map(findingCard).join("") : "<p class='muted'>No findings in this pass.</p>"}</section>`;
  }).join("");
  return `<div class="cols"><div>${groups}</div><section><p class="kicker">Sequenced plan</p><div class="steps">${twin.plan.map(stepCard).join("")}</div></section></div>`;
}

function renderLearn(twin) {
  const bugs = twin.risks.filter((risk) => risk.kind === "bug").slice(0, 6);
  const model = twin.models[state.modelKind];
  const services = twin.services.map((service) => `<option value="${esc(service.name)}">${esc(service.name)}</option>`).join("");
  return `
    <p class="narrative">When you confirm or reject a prediction, Twin nudges that model: weight += ${twin.model.learning_rate} × (outcome − prediction) × feature. Features that were present when the model was wrong move the most. Debt is a fixed index and does not learn.</p>
    <div class="cols">
      <section class="panel">
        <p class="kicker">Record a bug outcome</p>
        <div class="list">${bugs.map((risk) => `<div class="row" style="grid-template-columns: minmax(0,1fr) auto"><div><span class="path">${esc(risk.module)}</span><div class="meter ${tone(risk.score)}"><i style="width:${Math.round(risk.score * 100)}%"></i></div></div><div class="choice-row"><button type="button" class="choice yes" data-action="outcome" data-path="${esc(risk.module)}" data-happened="1">Bug happened</button><button type="button" class="choice no" data-action="outcome" data-path="${esc(risk.module)}" data-happened="0">It did not</button></div></div>`).join("")}</div>
        <form id="event-form" style="margin-top:16px">
          <p class="kicker">Record an incident</p>
          <div class="actions" style="display:flex; gap:8px; flex-wrap:wrap">
            <select name="service">${services}</select>
            <select name="severity"><option>sev1</option><option>sev2</option><option selected>sev3</option></select>
            <input name="summary" placeholder="What broke" />
            <button class="primary" type="submit">Fold into the model</button>
          </div>
        </form>
      </section>
      <section class="panel">
        <p class="kicker">Calibration</p>
        <p>${twin.calibration.n ? `${twin.calibration.n} outcomes · Brier ${fmt(twin.calibration.brier)} · ${twin.calibration.confirmed} confirmed` : "No outcomes yet. Confirm or reject a prediction and the weights will move."}</p>
        <div class="chips">${Object.keys(twin.models).map((kind) => `<button type="button" class="chip" data-action="model" data-kind="${kind}" aria-pressed="${state.modelKind === kind}">${kind}</button>`).join("")}</div>
        <p class="mono">bias ${fmt(model.bias)}</p>
        <table class="weights">${Object.entries(model.weights).map(([key, weight]) => `<tr><td>${esc(label(twin, key))}</td><td>${fmt(weight)}</td></tr>`).join("")}</table>
        <p class="kicker" style="margin-top:16px">Weight updates</p>
        ${twin.weight_history.length ? twin.weight_history.map((item) => `<p><span class="path">${esc(item.note)}</span><br/>${item.deltas.slice(0, 3).map((delta) => `${esc(delta.label)} ${fmt(delta.before)} → ${fmt(delta.after)}`).join(" · ")}</p>`).join("") : "<p class='muted'>No updates yet.</p>"}
        <p class="footnote">Churn uses a ${twin.model.windows.churn_days}-day window and a ${twin.model.windows.half_life_days}-day half-life. A knee is the raw value that maps to about 0.63: churn ${twin.model.knees.churn} lines, complexity ${twin.model.knees.complexity}, coupling ${twin.model.knees.coupling}.</p>
      </section>
    </div>`;
}

function stat(name, value) {
  return `<article class="stat"><span class="kicker">${name}</span><b>${fmt(value)}</b></article>`;
}

function riskRow(risk) {
  return `<button type="button" class="row" data-action="open" data-path="${esc(risk.module)}"><span><span class="path">${esc(risk.module)}</span><div class="meter ${tone(risk.score)}"><i style="width:${Math.round(risk.score * 100)}%"></i></div><span class="tags">${risk.drivers.map((driver) => `<i class="tag">${esc(driver)}</i>`).join("")}</span></span><span class="sev ${risk.kind}">${esc(risk.kind)}</span><span class="mono">${fmt(risk.score)}</span></button>`;
}

function findingCard(finding) {
  return `<article class="finding"><span class="sev ${finding.severity}">${esc(finding.severity)} · ${esc(finding.effort)}</span><h3>${esc(finding.title)}</h3><p>${esc(finding.detail)}</p><p><strong>Do this.</strong> ${esc(finding.remediation)}</p><p class="muted">${esc(finding.expected_effect)}</p></article>`;
}

function stepCard(step) {
  return `<article class="step"><div class="n">${step.step}</div><div><span class="sev ${step.severity}">${esc(step.agent)} · ${esc(step.effort)}</span><h3>${esc(step.title)}</h3><p>${esc(step.remediation)}</p><p class="faint">${esc(step.handoff)}</p></div></article>`;
}

function changeResult(result) {
  return `
    <p class="kicker">${esc(result.title)}</p>
    <p class="stat" style="padding:0"><span class="kicker">Change risk index</span><b>${fmt(result.risk)}</b></p>
    <p>${esc(result.recommendation)}</p>
    <p class="muted">Delay pressure on the edited files: ${fmt(result.delay_pressure)}. Reviewers: ${esc(result.reviewers.join(", ") || "no recent authors")}.</p>
    <div class="list">${result.blast.map((item) => `<div class="row" style="grid-template-columns: 1fr 70px 70px"><span class="path">${esc(item.module)}<br/><span class="faint">${item.relation} · depth ${item.depth}</span></span><span class="mono">${fmt(item.bug)}</span><span class="faint">${item.test_gap ? "untested" : "tested"}</span></div>`).join("")}</div>
    ${result.objections.length ? `<p class="kicker">Objections</p>${result.objections.map((item) => `<p><span class="sev ${item.severity}">${esc(item.agent)}</span> ${esc(item.title)}</p>`).join("")}` : ""}`;
}

function scenarioResult(result) {
  const rows = COMPARE.map(([key, labelText]) => {
    const before = result.baseline[key];
    const after = result.simulated[key];
    const delta = result.deltas[key];
    return `<tr><td>${labelText}</td><td>${fmt(before)}</td><td>${fmt(after)}</td><td class="${deltaTone(delta)}">${fmtDelta(delta)}</td></tr>`;
  }).join("");
  return `
    <section class="panel">
      <p class="kicker">${esc(result.name)}</p>
      ${result.notes.map((note) => `<p>${esc(note)}</p>`).join("")}
      <table class="compare"><thead><tr><th>Signal</th><th>Baseline</th><th>Simulated</th><th>Delta</th></tr></thead><tbody>${rows}</tbody></table>
      <p class="kicker">Largest module moves</p>
      <div class="list">${result.movers.map((row) => `<div class="row" style="grid-template-columns: 1.4fr 80px 90px"><span class="path">${esc(short(row.module))} · ${esc(row.metric)}</span><span class="mono">${fmt(row.before)} → ${fmt(row.after)}</span><span class="${deltaTone(row.delta)}">${fmtDelta(row.delta)}</span></div>`).join("")}</div>
      <p class="footnote">Bug-bearing modules per draw, p90 ${result.baseline.spread.bug_p90} → ${result.simulated.spread.bug_p90}. Same seed, ${result.baseline.spread.trials} draws.</p>
    </section>`;
}

function detail(module, twin) {
  const contributions = (module.contributions.bug || []).slice(0, 6);
  const evidence = module.evidence.slice(0, 4);
  return `
    <p class="kicker">${esc(module.service)} · ${esc(module.language)}</p>
    <h3>${esc(module.path)}</h3>
    <p class="mono">${module.loc} lines · complexity ${module.complexity} · churn ${Math.round(module.churn)} · tests ${module.test_gap ? "none linked" : "linked"}</p>
    <p class="muted">Authors: ${esc(module.authors.join(", ") || "no history")}. Review lag ${module.review_hours || 0}h.</p>
    <div class="chips">${["bug", "incident", "delay", "perf"].map((kind) => `<span class="tag">${kind} ${fmt(module.risks[kind])}</span>`).join("")}<span class="tag">debt ${fmt(module.debt)}</span></div>
    <p class="kicker">Why the bug index is ${fmt(module.risks.bug)}</p>
    ${contributions.map((row) => `<div class="contrib"><span>${esc(row.label)}</span><span class="mono">${fmt(row.contribution)}</span><div class="meter"><i style="width:${Math.min(100, Math.abs(row.contribution) * 40)}%"></i></div></div>`).join("")}
    <p class="faint">Baseline ${fmt(twin.models.bug.bias)}. The index is a logistic curve over these contributions.</p>
    ${evidence.length ? `<p class="kicker">Evidence</p>${evidence.map((item) => `<p class="path">${item.line ? `L${item.line} · ` : ""}${esc(item.kind)} — ${esc(item.text)}</p>`).join("")}` : ""}
    <p class="kicker">Imports</p>
    <p class="path">${esc(module.imports.join(", ") || "none")}</p>
    <p class="kicker">Imported by</p>
    <p class="path">${esc(module.imported_by.join(", ") || "none")}</p>`;
}

function drawMap(modules, edges, selected) {
  const groups = new Map();
  modules.forEach((module) => {
    if (!groups.has(module.service)) groups.set(module.service, []);
    groups.get(module.service).push(module);
  });
  const ordered = [...groups.entries()].sort((a, b) => Math.max(...b[1].map((item) => item.risks.bug)) - Math.max(...a[1].map((item) => item.risks.bug)));
  const pos = new Map();
  const colW = 220;
  const rowH = 56;
  let x = 24;
  let height = 120;
  const headers = [];
  ordered.forEach(([service, list]) => {
    list.sort((a, b) => b.risks.bug - a.risks.bug);
    let y = 48;
    headers.push(`<text x="${x}" y="28" fill="#727b6c" font-size="11" font-family="IBM Plex Mono, monospace">${esc(service)}</text>`);
    list.forEach((module) => {
      pos.set(module.path, { x, y, w: 190, h: 42, module });
      y += rowH;
    });
    height = Math.max(height, y + 20);
    x += colW;
  });
  const hot = new Set();
  if (selected) {
    edges.forEach((edge) => {
      if (edge.source === selected || edge.target === selected) hot.add(`${edge.source}->${edge.target}`);
    });
  }
  const paths = edges.map((edge) => {
    const a = pos.get(edge.source);
    const b = pos.get(edge.target);
    if (!a || !b) return "";
    const x1 = a.x + a.w;
    const y1 = a.y + a.h / 2;
    const x2 = b.x;
    const y2 = b.y + b.h / 2;
    const c = Math.max(24, Math.abs(x2 - x1) * 0.4);
    const klass = hot.has(`${edge.source}->${edge.target}`) ? "edge hot" : "edge";
    return `<path class="${klass}" d="M ${x1} ${y1} C ${x1 + c} ${y1}, ${x2 - c} ${y2}, ${x2} ${y2}"/>`;
  }).join("");
  const nodes = [...pos.values()].map(({ x: nx, y: ny, w, h, module }) => {
    const stroke = module.path === selected ? "#d6ff4a" : tone(module.risks.bug) === "bad" ? "#ff6a3d" : tone(module.risks.bug) === "warn" ? "#f0b429" : "#3d4838";
    return `<g class="node" data-action="open" data-path="${esc(module.path)}"><rect x="${nx}" y="${ny}" width="${w}" height="${h}" rx="6" fill="#181d16" stroke="${stroke}" stroke-width="${module.path === selected ? 2 : 1}"/><text x="${nx + 10}" y="${ny + 18}" fill="#e8eedf" font-size="12" font-family="Public Sans, sans-serif">${esc(short(module.path))}</text><text x="${nx + 10}" y="${ny + 34}" fill="#a3ac99" font-size="11" font-family="IBM Plex Mono, monospace">${fmt(module.risks.bug)}</text></g>`;
  }).join("");
  return `<div class="map-scroll"><svg width="${x}" height="${height}" viewBox="0 0 ${x} ${height}" role="img" aria-label="Service map">${paths}${headers.join("")}${nodes}</svg></div>`;
}

function empty() {
  return `<section class="panel"><h2>No twin yet</h2><p>Scan a repository from the field above. On first launch, Twin reads <span class="path">examples/northwind</span>.</p></section>`;
}

function topModule(twin, service) {
  const modules = twin.modules.filter((module) => module.service === service && module.role === "code");
  modules.sort((a, b) => b.risks.bug - a.risks.bug);
  return modules[0] ? modules[0].path : "";
}

function label(twin, key) {
  const found = twin.model.features.find((feature) => feature.key === key);
  return found ? found.label : key;
}

function short(path) {
  return String(path || "").split("/").pop();
}

function age(days) {
  if (days < 1) return "today";
  if (days < 2) return "1 day ago";
  return `${Math.round(days)} days ago`;
}

function tone(score) {
  if (score >= 0.66) return "bad";
  if (score >= 0.4) return "warn";
  return "good";
}

function fmt(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  if (Number.isInteger(value)) return String(value);
  return Number(value).toFixed(2);
}

function fmtDelta(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const sign = value > 0 ? "+" : "";
  return sign + (Number.isInteger(value) ? String(value) : Number(value).toFixed(2));
}

function deltaTone(value) {
  const epsilon = Number.isInteger(value) ? 0.5 : 0.005;
  if (Math.abs(value) < epsilon) return "flat";
  return value < 0 ? "good" : "bad";
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
}
