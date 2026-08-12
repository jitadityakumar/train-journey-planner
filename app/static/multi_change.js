// Second stage of the two-stage /results flow (GitHub issue #26): only
// loaded when the server-rendered first pass (direct/1-change) came back
// empty. Fetches the OTP-sidecar-backed 2-5 change tier and replaces the
// "no results" placeholder in #multi-change-root with either journeys, an
// empty-state, or a degraded-mode banner — never blended with the first
// pass (OTP_SIDECAR_PLAN.md decision #1).

function formatDuration(totalMinutes) {
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours === 0) return `${minutes}m`;
  if (minutes === 0) return `${hours}h`;
  return `${hours}h${minutes}m`;
}

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "className") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) node.appendChild(child);
  return node;
}

function renderLeg(leg) {
  const head = el("div", { className: "leg-head" }, [
    el("span", {
      text:
        leg.departure_time.slice(0, 5) +
        (leg.departure_next_day ? "+1" : "") +
        " → " +
        leg.arrival_time.slice(0, 5) +
        (leg.arrival_next_day ? "+1" : ""),
    }),
    el("span", { text: formatDuration(leg.duration_minutes) }),
  ]);
  const parts = [head];
  if (leg.operator) parts.push(el("div", { className: "trip-operator", text: leg.operator }));
  const metaText = [leg.route_description, leg.headsign ? `towards ${leg.headsign}` : null]
    .filter(Boolean)
    .join(" · ");
  if (metaText) parts.push(el("div", { className: "trip-meta", text: metaText }));
  return parts;
}

function renderJourney(journey) {
  const badgeLabel = journey.num_changes === 1 ? "1 change" : `${journey.num_changes} changes`;
  const children = [el("span", { className: "badge badge-multi-change", text: badgeLabel })];
  if (journey.is_past) children.unshift(el("span", { className: "badge badge-past", text: "Past" }));

  journey.legs.forEach((leg, i) => {
    children.push(...renderLeg(leg));
    if (i < journey.legs.length - 1) {
      children.push(
        el("div", {
          className: "change-marker",
          text: `change at ${leg.destination.name} (${leg.destination.crs_code})`,
        })
      );
    }
  });
  children.push(el("div", { className: "trip-total", text: `${formatDuration(journey.duration_minutes)} total` }));

  return el("div", { className: "trip" }, children);
}

async function loadMultiChangeResults() {
  const root = document.getElementById("multi-change-root");
  if (!root) return;

  const params = new URLSearchParams({
    from: root.dataset.from,
    to: root.dataset.to,
    date: root.dataset.date,
    time: root.dataset.time,
    window_minutes: root.dataset.windowMinutes,
  });

  const sidecarKnownDown = root.dataset.sidecarHealthy === "false";
  if (sidecarKnownDown) {
    renderDegraded(root);
    return;
  }

  root.replaceChildren(
    el("div", { className: "searching" }, [
      el("div", { className: "spinner" }),
      el("span", {
        text: "No direct or 1-change connections found — searching for 2-5 change journeys…",
      }),
    ])
  );

  let data;
  try {
    const resp = await fetch(`/api/journeys/multi-change?${params}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    data = await resp.json();
  } catch (err) {
    renderDegraded(root);
    return;
  }

  if (!data.sidecar_healthy) {
    renderDegraded(root);
    return;
  }

  if (data.journeys.length === 0) {
    root.replaceChildren(el("div", { className: "no-results", text: "No journeys found at all in this window." }));
    return;
  }

  root.replaceChildren(...data.journeys.map(renderJourney));
}

function renderDegraded(root) {
  root.replaceChildren(
    el("div", {
      className: "degraded-banner",
      text: "Deeper search (2-5 changes) is temporarily unavailable — showing direct/1-change results only.",
    }),
    el("div", { className: "no-results", text: "No direct or single-change trains found in this window." })
  );
}

loadMultiChangeResults();
