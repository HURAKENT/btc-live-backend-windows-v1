(() => {
  "use strict";

  const NO_DATA = "NO_DATA";
  const state = {
    interface_version: null,
    current_market_identity: null,
    health: null,
    sources: [],
    strict_a_signal: null,
    execution_readiness: null,
    paper_account: null,
    paper_positions: [],
    paper_fills: [],
    incidents: [],
    last_event_id: 0,
    performanceStatus: null,
    performanceStrategies: [],
    performanceView: "HISTORICAL",
    selectedStrategyId: null,
    selectedStrategyDetail: null,
    performanceTimeseries: null,
    performanceObservations: [],
    performanceTransportStatus: "CONNECTING",
  };

  let socket = null;
  let reconnectTimer = null;
  let reconnectAttempt = 0;
  let performanceRequestSequence = 0;

  const element = (id) => document.getElementById(id);
  const value = (candidate) => candidate === null || candidate === undefined || candidate === "" ? NO_DATA : String(candidate);
  const firstDefined = (...candidates) => candidates.find((candidate) => candidate !== null && candidate !== undefined && candidate !== "");

  function displayValue(candidate) {
    if (candidate === null || candidate === undefined || candidate === "") return NO_DATA;
    if (typeof candidate === "object") return JSON.stringify(candidate);
    return String(candidate);
  }

  function money(candidate) {
    if (!Number.isFinite(Number(candidate))) return NO_DATA;
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: "USD",
      minimumFractionDigits: 2,
      maximumFractionDigits: 6,
    }).format(Number(candidate) / 1_000_000);
  }

  function probability(candidate) {
    if (!Number.isFinite(Number(candidate))) return NO_DATA;
    return `${(Number(candidate) / 10_000).toFixed(2)}%`;
  }

  function shares(candidate) {
    if (!Number.isFinite(Number(candidate))) return NO_DATA;
    return (Number(candidate) / 1_000_000).toFixed(6).replace(/\.?0+$/, "");
  }

  function ratio(candidate) {
    if (candidate === null || candidate === undefined || candidate === "") return NO_DATA;
    if (!Number.isFinite(Number(candidate))) return value(candidate);
    return `${(Number(candidate) * 100).toFixed(2)}%`;
  }

  function timestamp(candidate) {
    if (!Number.isFinite(Number(candidate))) return NO_DATA;
    return new Date(Number(candidate)).toISOString();
  }

  function setStatus(text, tone) {
    const node = element("connection-status");
    node.textContent = text;
    node.className = `status status-${tone}`;
  }

  function replaceRows(id, rows) {
    const container = element(id);
    container.replaceChildren();
    if (!rows.length) {
      container.textContent = NO_DATA;
      container.classList.add("muted");
      return;
    }
    container.classList.remove("muted");
    rows.forEach(([label, raw]) => {
      const row = document.createElement("div");
      row.className = "row";
      const name = document.createElement("span");
      const detail = document.createElement("span");
      name.textContent = label;
      detail.textContent = value(raw);
      row.append(name, detail);
      container.append(row);
    });
  }

  function identityParts(identity) {
    if (identity && typeof identity === "object") {
      const payload = identity.payload && typeof identity.payload === "object" ? identity.payload : {};
      return {
        market_id: firstDefined(identity.market_id, identity.market_identity, identity.id, payload.market_id),
        market_date: firstDefined(identity.market_date, payload.market_date),
      };
    }
    const text = typeof identity === "string" ? identity : null;
    const dateMatch = text && text.match(/\d{4}-\d{2}-\d{2}/);
    return { market_id: text, market_date: dateMatch ? dateMatch[0] : null };
  }

  function renderHealth() {
    const healthStatus = state.health && firstDefined(state.health.status, state.health.state);
    element("system-health").textContent = value(healthStatus);
    const sourceRows = Array.isArray(state.sources) ? state.sources : [];
    const runtimeSources = state.health && state.health.source_health && typeof state.health.source_health === "object"
      ? state.health.source_health : {};
    const names = new Set([
      ...Object.keys(runtimeSources),
      ...sourceRows.map((source) => firstDefined(source.source, source.name, source.source_id)).filter(Boolean),
    ]);
    replaceRows("source-health", [...names].sort().map((name) => {
      const provenance = sourceRows.find((source) => firstDefined(source.source, source.name, source.source_id) === name);
      const status = firstDefined(runtimeSources[name], provenance && provenance.status, provenance && provenance.health, provenance && provenance.state);
      const detail = status || (provenance
        ? `${value(provenance.event_count)} events · source ${value(provenance.last_source_timestamp_ms)}`
        : NO_DATA);
      return [name, detail];
    }));
  }

  function renderSignal() {
    const signal = state.strict_a_signal;
    if (!signal) {
      replaceRows("strict-a-signal", []);
      return;
    }
    replaceRows("strict-a-signal", [
      ["Status", signal.execution_eligible === true ? "ELIGIBLE" : "BLOCKED"],
      ["Checkpoint", signal.checkpoint_minutes === undefined ? null : `T-${signal.checkpoint_minutes}`],
      ["Side / bucket", `${value(signal.side)} / ${value(signal.bucket_index)}`],
      ["Model probability", probability(signal.model_probability_micros)],
      ["Market probability", probability(signal.market_q_micros)],
      ["Five-share VWAP", probability(signal.vwap5_micros)],
      ["Reason", signal.reason_code],
    ]);
  }

  function renderReadiness() {
    const readiness = state.execution_readiness;
    if (!readiness) {
      replaceRows("execution-readiness", []);
      element("block-reason").textContent = NO_DATA;
      return;
    }
    replaceRows("execution-readiness", [
      ["Ready", readiness.ready === true ? "YES" : "NO"],
      ["Checkpoint", readiness.checkpoint_minutes === undefined ? null : `T-${readiness.checkpoint_minutes}`],
      ["Requested shares", shares(readiness.requested_shares_micros)],
      ["Checked", readiness.checked_at_ms === undefined ? null : new Date(readiness.checked_at_ms).toISOString()],
    ]);
    element("block-reason").textContent = value(readiness.reason_code);
  }

  function renderAccount() {
    const account = state.paper_account;
    const bankroll = account && firstDefined(account.equity_usd_micros, account.starting_bankroll_usd_micros);
    element("bankroll").textContent = money(bankroll);
    element("realized-pnl").textContent = money(account && account.realized_pnl_usd_micros);
    element("unrealized-pnl").textContent = money(account && account.unrealized_pnl_usd_micros);
    colorSigned(element("realized-pnl"), account && account.realized_pnl_usd_micros);
    colorSigned(element("unrealized-pnl"), account && account.unrealized_pnl_usd_micros);
  }

  function colorSigned(node, candidate) {
    node.classList.remove("positive", "negative");
    if (Number(candidate) > 0) node.classList.add("positive");
    if (Number(candidate) < 0) node.classList.add("negative");
  }

  function renderPositions() {
    const rows = [];
    (Array.isArray(state.paper_positions) ? state.paper_positions : []).forEach((position) => {
      rows.push([
        `${value(position.market_date)} · ${value(position.status)}`,
        `${shares(position.filled_shares_micros)} shares @ ${probability(position.average_price_micros)}`,
      ]);
    });
    replaceRows("paper-position", rows);
  }

  function renderFills() {
    const rows = [];
    (Array.isArray(state.paper_fills) ? state.paper_fills : []).forEach((fill) => {
      rows.push([
        value(fill.fill_key),
        `${shares(fill.shares_micros)} @ ${probability(fill.price_micros)} · fee ${money(fill.fee_usd_micros)}`,
      ]);
    });
    replaceRows("paper-fills", rows);
  }

  function renderIncidents() {
    const rows = [];
    (Array.isArray(state.incidents) ? state.incidents : []).forEach((incident) => {
      const payload = incident.payload && typeof incident.payload === "object" ? incident.payload : {};
      rows.push([
        firstDefined(incident.severity, incident.status, "INCIDENT"),
        firstDefined(incident.reason_code, payload.reason_code, payload.code, incident.incident_key, incident.code, NO_DATA),
      ]);
    });
    replaceRows("incidents", rows);
  }

  function setPerformanceTransport(status, tone) {
    state.performanceTransportStatus = status;
    const node = element("performance-transport-status");
    node.textContent = status;
    node.className = `status status-${tone}`;
  }

  function renderPerformanceSystem() {
    const status = state.performanceStatus;
    const health = status && status.database_health;
    const blockingReason = status && status.blocking_reason;
    const revisions = status && status.performance;
    const freshness = status && status.source_freshness;
    const healthValue = health && health.status === "PASS" && !blockingReason ? "READY" : (blockingReason || (health && health.status) || "NOT_READY");
    const healthNode = element("performance-health-status");
    healthNode.textContent = healthValue;
    healthNode.className = `status status-${healthValue === "READY" ? "ok" : (healthValue === "NOT_READY" ? "warn" : "bad")}`;
    element("performance-blocking-reason").textContent = value(blockingReason || "NONE");
    element("performance-freshness").textContent = freshness
      ? `source ${timestamp(freshness.latest_source_timestamp_ms)} · received ${timestamp(freshness.latest_received_timestamp_ms)}`
      : NO_DATA;
    const counts = status && status.catchup && status.catchup.counts;
    element("performance-catchup").textContent = counts && Object.keys(counts).length
      ? Object.keys(counts).sort().map((key) => `${key} ${counts[key]}`).join(" · ")
      : NO_DATA;
    if (revisions && Number.isInteger(revisions.current_revision_count)) {
      element("performance-freshness").title = `${revisions.current_revision_count} current revisions · generated ${timestamp(revisions.latest_generated_at_ms)}`;
    }
  }

  function metricText(metrics, key, formatter = displayValue) {
    if (!metrics || !Object.prototype.hasOwnProperty.call(metrics, key)) return NO_DATA;
    return formatter(metrics[key]);
  }

  function renderStrategies() {
    const tbody = element("strategies-body");
    tbody.replaceChildren();
    const strategies = Array.isArray(state.performanceStrategies) ? state.performanceStrategies : [];
    element("strategy-count").textContent = strategies.length ? `${strategies.length} canonical identities` : NO_DATA;
    if (!strategies.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 8;
      cell.className = "empty-cell";
      cell.textContent = NO_DATA;
      row.append(cell);
      tbody.append(row);
      return;
    }
    strategies.forEach((strategy) => {
      const view = strategy.views && strategy.views[state.performanceView];
      const ready = view && view.status === "READY";
      const metrics = ready ? view.metrics : null;
      const row = document.createElement("tr");
      if (strategy.strategy_id === state.selectedStrategyId) row.classList.add("selected");
      const identity = document.createElement("td");
      const choose = document.createElement("button");
      choose.type = "button";
      choose.textContent = value(strategy.strategy_id);
      choose.addEventListener("click", () => void selectStrategy(strategy.strategy_id, true));
      const family = document.createElement("small");
      family.textContent = `${value(strategy.version)} · ${value(strategy.family)}`;
      identity.append(choose, family);
      const cells = [
        identity,
        tableCell(`${ready ? "READY" : "NOT_READY"} · ${value(strategy.activation_status)}`),
        tableCell(metricText(metrics, "opportunity_count")),
        tableCell(metricText(metrics, "accepted_signal_count")),
        tableCell(metricText(metrics, "resolved_signal_count")),
        tableCell(metricText(metrics, "win_rate_ratio", ratio)),
        tableCell(metricText(metrics, "pnl_usd_micros", money)),
        tableCell(metricText(metrics, "roi_ratio", ratio)),
      ];
      row.append(...cells);
      row.addEventListener("dblclick", () => void selectStrategy(strategy.strategy_id, true));
      tbody.append(row);
    });
  }

  function tableCell(content) {
    const cell = document.createElement("td");
    cell.textContent = displayValue(content);
    return cell;
  }

  function addMetricCard(container, label, result, note) {
    const card = document.createElement("div");
    card.className = "metric-card";
    const name = document.createElement("span");
    const amount = document.createElement("strong");
    name.textContent = label;
    amount.textContent = displayValue(result);
    card.append(name, amount);
    if (note) {
      const detail = document.createElement("small");
      detail.textContent = note;
      card.append(detail);
    }
    container.append(card);
  }

  function renderStrategyMetrics(metrics) {
    const container = element("strategy-metrics");
    container.replaceChildren();
    if (!metrics) {
      container.textContent = "NOT_READY";
      container.classList.add("muted");
      return;
    }
    container.classList.remove("muted");
    addMetricCard(container, "Opportunities", metricText(metrics, "opportunity_count"), `effective ${metricText(metrics, "effective_observation_count")}`);
    addMetricCard(container, "Accepted signals", metricText(metrics, "accepted_signal_count"), `emitted ${metricText(metrics, "emitted_signal_count")}`);
    addMetricCard(container, "Resolved / unresolved", `${metricText(metrics, "resolved_signal_count")} / ${metricText(metrics, "unresolved_signal_count")}`, `unscorable ${metricText(metrics, "unscorable_signal_count")}`);
    addMetricCard(container, "Wins / losses", `${metricText(metrics, "wins")} / ${metricText(metrics, "losses")}`, `resolved denominator ${metricText(metrics, "win_rate_denominator")}`);
    addMetricCard(container, "Win rate", metricText(metrics, "win_rate_ratio", ratio), `${metricText(metrics, "win_rate_numerator")} / ${metricText(metrics, "win_rate_denominator")}`);
    addMetricCard(container, "PnL @ 5 shares", metricText(metrics, "pnl_usd_micros", money), `turnover ${metricText(metrics, "turnover_usd_micros", money)}`);
    addMetricCard(container, "ROI", metricText(metrics, "roi_ratio", ratio), `${metricText(metrics, "roi_numerator_usd_micros", money)} / ${metricText(metrics, "roi_denominator_usd_micros", money)}`);
    addMetricCard(container, "Average price", metricText(metrics, "average_price_micros", probability), `priced ${metricText(metrics, "average_price_denominator")}`);
    addMetricCard(container, "Max / current drawdown", `${metricText(metrics, "max_drawdown_usd_micros", money)} / ${metricText(metrics, "current_drawdown_usd_micros", money)}`);
    addMetricCard(container, "Longest win / loss streak", `${metricText(metrics, "longest_win_streak")} / ${metricText(metrics, "longest_loss_streak")}`);
    addMetricCard(container, "Decision coverage", metricText(metrics, "decision_coverage_ratio", ratio), `${metricText(metrics, "decision_coverage_numerator")} / ${metricText(metrics, "decision_coverage_denominator")}`);
    addMetricCard(container, "Resolution coverage", metricText(metrics, "resolution_coverage_ratio", ratio), `${metricText(metrics, "resolution_coverage_numerator")} / ${metricText(metrics, "resolution_coverage_denominator")}`);
    addMetricCard(container, "Price coverage", metricText(metrics, "price_coverage_ratio", ratio), `${metricText(metrics, "price_coverage_numerator")} / ${metricText(metrics, "price_coverage_denominator")}`);
    const annualized = metricText(metrics, "annualized_return_ratio", ratio);
    addMetricCard(container, "Annualized return", annualized, `reason ${metricText(metrics, "annualized_return_reason_code")}`);
  }

  function rowSummary(point) {
    if (!point || typeof point !== "object") return displayValue(point);
    const fields = [];
    [
      ["resolved", "resolved_signal_count"], ["wins", "wins"], ["losses", "losses"],
      ["PnL", "pnl_usd_micros"], ["cumulative", "cumulative_pnl_usd_micros"],
      ["WR", "win_rate_ratio"], ["ROI", "roi_ratio"],
    ].forEach(([label, key]) => {
      if (!Object.prototype.hasOwnProperty.call(point, key)) return;
      const formatted = key.endsWith("_usd_micros") ? money(point[key]) : (key.endsWith("_ratio") ? ratio(point[key]) : displayValue(point[key]));
      fields.push(`${label} ${formatted}`);
    });
    return fields.length ? fields.join(" · ") : JSON.stringify(point);
  }

  function seriesRows(points, labelKeys) {
    return (Array.isArray(points) ? points : []).slice(-12).reverse().map((point) => {
      const label = labelKeys.map((key) => point && point[key]).find((item) => item !== null && item !== undefined) || NO_DATA;
      return [label, rowSummary(point)];
    });
  }

  function renderPerformanceSeries() {
    const payload = state.performanceTimeseries;
    const series = payload && payload.status === "READY" && payload.timeseries ? payload.timeseries : {};
    const cumulative = series.CUMULATIVE || [];
    replaceRows("cumulative-series", seriesRows(cumulative, ["market_date", "as_of_date", "observation_key"]));
    replaceRows("recent-resolutions", seriesRows(cumulative.slice(-10), ["market_date", "observation_key"]));
    replaceRows("monthly-series", seriesRows(series.MONTHLY || [], ["month", "period_key"]));
    const rolling = ["ROLLING_30D", "ROLLING_90D", "ROLLING_365D"].flatMap((kind) =>
      (Array.isArray(series[kind]) ? series[kind] : []).slice(-4).map((point) => ({ ...point, window_kind: kind }))
    );
    replaceRows("rolling-series", seriesRows(rolling, ["window_kind", "as_of_date", "observation_key"]));
  }

  function renderRecentDecisions() {
    const observations = Array.isArray(state.performanceObservations) ? state.performanceObservations : [];
    replaceRows("recent-decisions", observations.map((observation) => [
      `${displayValue(observation.market_date)} · T-${displayValue(observation.checkpoint_minutes)}m`,
      `${displayValue(observation.source_layer)} · ${observation.accepted === true ? "ACCEPTED" : "REJECTED"} · ${displayValue(observation.scoring_status)} · ${displayValue(observation.scoring_reason_code || observation.reason_code)}`,
    ]));
  }

  function renderStrategyDetail() {
    const detail = state.selectedStrategyDetail;
    if (!detail) {
      element("strategy-detail-title").textContent = NO_DATA;
      element("strategy-detail-labels").textContent = NO_DATA;
      element("performance-provenance").textContent = NO_DATA;
      renderStrategyMetrics(null);
      renderPerformanceSeries();
      renderRecentDecisions();
      return;
    }
    element("strategy-detail-title").textContent = detail.strategy_id;
    const labels = element("strategy-detail-labels");
    labels.replaceChildren();
    [detail.version, detail.family, detail.activation_status, ...(detail.eligibility_labels || [])].filter(Boolean).forEach((label) => {
      const badge = document.createElement("span");
      badge.className = `badge${String(label).includes("RESEARCH") || String(label).includes("NOT_ROBUST") ? " badge-research" : ""}`;
      badge.textContent = label;
      labels.append(badge);
    });
    if (detail.parent_strategy_id) {
      const parent = document.createElement("span");
      parent.className = "badge";
      parent.textContent = `PARENT ${detail.parent_strategy_id}`;
      labels.append(parent);
    }
    const view = detail.views && detail.views[state.performanceView];
    const ready = view && view.status === "READY";
    renderStrategyMetrics(ready ? view.metrics : null);
    element("performance-provenance").textContent = ready
      ? `view ${state.performanceView} · revision ${displayValue(view.revision && view.revision.revision_key)} · generated ${timestamp(view.revision && view.revision.generated_at_ms)} · ledger ${displayValue(view.revision && view.revision.source_ledger_sha256)} · contract ${displayValue(view.provenance && view.provenance.calculation_contract)}`
      : `${state.performanceView} · NOT_READY · no zero metrics fabricated`;
    renderPerformanceSeries();
    renderRecentDecisions();
  }

  function renderPerformance() {
    renderPerformanceSystem();
    renderStrategies();
    renderStrategyDetail();
  }

  function render() {
    const identity = identityParts(state.current_market_identity);
    element("market-id").textContent = value(identity.market_id);
    element("market-date").textContent = value(identity.market_date);
    renderHealth();
    renderSignal();
    renderReadiness();
    renderAccount();
    renderPositions();
    renderFills();
    renderIncidents();
    renderPerformance();
  }

  function applyBootstrap(payload) {
    if (!payload || typeof payload !== "object") throw new Error("INVALID_BOOTSTRAP");
    [
      "interface_version", "current_market_identity", "health", "sources",
      "strict_a_signal", "execution_readiness", "paper_account",
      "paper_positions", "paper_fills", "incidents",
    ].forEach((field) => {
      state[field] = payload[field] === undefined ? state[field] : payload[field];
    });
    if (Number.isInteger(payload.last_event_id) && payload.last_event_id >= state.last_event_id) {
      state.last_event_id = payload.last_event_id;
    }
    render();
  }

  function upsert(collectionName, keyName, payload) {
    const collection = Array.isArray(state[collectionName]) ? [...state[collectionName]] : [];
    const key = payload && payload[keyName];
    const index = key === undefined ? -1 : collection.findIndex((item) => item[keyName] === key);
    if (index >= 0) collection[index] = payload;
    else collection.unshift(payload);
    state[collectionName] = collection;
  }

  function validateEnvelope(event) {
    return event && Number.isInteger(event.event_id) && event.event_id >= 0 &&
      typeof event.topic === "string" && typeof event.event_type === "string" &&
      Object.prototype.hasOwnProperty.call(event, "payload") &&
      Number.isFinite(Number(event.created_at_ms));
  }

  function applyEvent(event) {
    if (!validateEnvelope(event)) throw new Error("INVALID_EVENT_ENVELOPE");
    if (event.event_id <= state.last_event_id) return;
    const payload = event.payload;
    switch (event.topic) {
      case "paper.fill": upsert("paper_fills", "fill_key", payload); break;
      case "paper.position": upsert("paper_positions", "position_key", payload); break;
      case "paper.account": state.paper_account = payload; break;
      case "paper.readiness": state.execution_readiness = payload; break;
      default:
        if (payload && payload.schema_version === "STRICT_A_SIGNAL_V1") state.strict_a_signal = payload;
        else void refreshBootstrap(false);
    }
    state.last_event_id = event.event_id;
    render();
    void refreshPerformance(false);
  }

  async function refreshBootstrap(showUnavailable) {
    try {
      const response = await fetch("/api/v1/bootstrap", { cache: "no-store", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(`BOOTSTRAP_HTTP_${response.status}`);
      applyBootstrap(await response.json());
      return true;
    } catch (_error) {
      if (showUnavailable) setStatus("BACKEND_UNAVAILABLE", "bad");
      render();
      return false;
    }
  }

  async function fetchPerformanceJson(path) {
    const response = await fetch(path, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`PERFORMANCE_HTTP_${response.status}`);
    return response.json();
  }

  async function selectStrategy(strategyId, scrollIntoView) {
    if (!strategyId) return;
    state.selectedStrategyId = strategyId;
    state.selectedStrategyDetail = null;
    state.performanceTimeseries = null;
    state.performanceObservations = [];
    renderPerformance();
    const requestSequence = ++performanceRequestSequence;
    const base = `/api/v1/performance/strategies/${encodeURIComponent(strategyId)}`;
    try {
      const [detail, timeseries, observations] = await Promise.all([
        fetchPerformanceJson(base),
        fetchPerformanceJson(`${base}/timeseries?view=${encodeURIComponent(state.performanceView)}`),
        fetchPerformanceJson(`${base}/observations?limit=25`),
      ]);
      if (requestSequence !== performanceRequestSequence || strategyId !== state.selectedStrategyId) return;
      state.selectedStrategyDetail = detail;
      state.performanceTimeseries = timeseries;
      state.performanceObservations = Array.isArray(observations.observations) ? observations.observations : [];
      setPerformanceTransport("LIVE", "ok");
      renderPerformance();
      if (scrollIntoView) element("strategy-detail").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (_error) {
      if (requestSequence !== performanceRequestSequence) return;
      setPerformanceTransport("BACKEND_UNAVAILABLE", "bad");
      renderPerformance();
    }
  }

  async function refreshPerformance(showUnavailable) {
    setPerformanceTransport("REFRESHING", "warn");
    try {
      const [status, strategies] = await Promise.all([
        fetchPerformanceJson("/api/v1/performance/status"),
        fetchPerformanceJson("/api/v1/performance/strategies"),
      ]);
      state.performanceStatus = status;
      state.performanceStrategies = Array.isArray(strategies.strategies) ? strategies.strategies : [];
      const selectedExists = state.performanceStrategies.some((strategy) => strategy.strategy_id === state.selectedStrategyId);
      if (!selectedExists) {
        const preferred = state.performanceStrategies.find((strategy) => strategy.strategy_id === "YES_STRICT_A_OPERATIONAL") || state.performanceStrategies[0];
        state.selectedStrategyId = preferred ? preferred.strategy_id : null;
      }
      setPerformanceTransport("LIVE", "ok");
      renderPerformance();
      if (state.selectedStrategyId) await selectStrategy(state.selectedStrategyId, false);
      return true;
    } catch (_error) {
      if (showUnavailable) setPerformanceTransport("BACKEND_UNAVAILABLE", "bad");
      renderPerformance();
      return false;
    }
  }

  function bindPerformanceControls() {
    element("performance-view-selector").querySelectorAll("[data-source-view]").forEach((button) => {
      button.addEventListener("click", () => {
        state.performanceView = button.dataset.sourceView;
        element("performance-view-selector").querySelectorAll("[data-source-view]").forEach((candidate) => {
          const active = candidate.dataset.sourceView === state.performanceView;
          candidate.classList.toggle("active", active);
          candidate.setAttribute("aria-pressed", String(active));
        });
        renderStrategies();
        void selectStrategy(state.selectedStrategyId, false);
      });
    });
  }

  function connect() {
    clearTimeout(reconnectTimer);
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${scheme}://${location.host}/ws/v1/events?after_event_id=${state.last_event_id}`);
    socket.addEventListener("open", () => {
      reconnectAttempt = 0;
      setStatus("LIVE", "ok");
    });
    socket.addEventListener("message", (message) => {
      try {
        applyEvent(JSON.parse(message.data));
      } catch (_error) {
        setStatus("INVALID_UPDATE", "bad");
        socket.close();
      }
    });
    socket.addEventListener("error", () => setStatus("BACKEND_UNAVAILABLE", "bad"));
    socket.addEventListener("close", scheduleReconnect);
  }

  function scheduleReconnect() {
    if (reconnectTimer !== null) clearTimeout(reconnectTimer);
    setStatus("RECONNECTING", "warn");
    const delay = Math.min(10_000, 500 * (2 ** reconnectAttempt));
    reconnectAttempt += 1;
    reconnectTimer = setTimeout(async () => {
      await refreshBootstrap(false);
      connect();
    }, delay);
  }

  render();
  bindPerformanceControls();
  void Promise.all([refreshBootstrap(true), refreshPerformance(true)]).finally(connect);
  window.setInterval(() => void refreshPerformance(false), 30_000);
})();
