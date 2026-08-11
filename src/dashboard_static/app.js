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
  };

  let socket = null;
  let reconnectTimer = null;
  let reconnectAttempt = 0;

  const element = (id) => document.getElementById(id);
  const value = (candidate) => candidate === null || candidate === undefined || candidate === "" ? NO_DATA : String(candidate);
  const firstDefined = (...candidates) => candidates.find((candidate) => candidate !== null && candidate !== undefined && candidate !== "");

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
  void refreshBootstrap(true).finally(connect);
})();
