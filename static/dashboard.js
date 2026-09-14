// AI assistance: parts of this file were written with Claude (Anthropic).
// Architecture, review and debugging are my own. See README.md, "AI use".

// The whole dashboard: fetch one JSON document on a timer and redraw from it.
// No framework and no build step; the page has exactly three panels and they
// all describe the same instant because they come from the same response.

const REFRESH_MS = 3000;
const COLORS = ["#2563eb", "#16794a", "#b42318", "#8a6100", "#7c3aed", "#0e7490"];

let chart = null;

function clock(iso) {
  // Timestamps arrive as UTC; the reader wants their own wall clock.
  return new Date(iso).toLocaleTimeString();
}

function duration(seconds) {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function card(monitor) {
  const uptime = monitor.uptime === null ? "—" : `${monitor.uptime}%`;
  let last = "not checked yet";
  if (monitor.last_checked_at) {
    last = monitor.last_ok
      ? `${Math.round(monitor.last_latency_ms)} ms`
      : monitor.last_error;
    last += ` · ${clock(monitor.last_checked_at)}`;
  }
  const forced = monitor.forced_down ? '<span class="forced">forced</span>' : "";
  const element = document.createElement("div");
  element.className = "card";
  element.innerHTML = `
    <div class="card-head">
      <span class="card-name"></span>
      <span class="state state-${monitor.state}">${monitor.state}</span>
    </div>
    <div class="uptime">${uptime}</div>
    <div class="card-foot"><span class="card-last"></span> ${forced}</div>`;
  // Names, targets and error texts come from the user and from the network,
  // so they are set as text rather than parsed as HTML.
  element.querySelector(".card-name").textContent = monitor.name;
  element.querySelector(".card-last").textContent = last;
  element.title = monitor.target;
  return element;
}

function renderCards(monitors) {
  const box = document.getElementById("cards");
  box.replaceChildren(...monitors.map(card));
  if (!monitors.length) {
    box.textContent = "No monitors yet.";
  }
}

function renderIncidents(incidents) {
  const box = document.getElementById("incidents");
  if (!incidents.length) {
    box.textContent = "No incidents recorded.";
    return;
  }
  const table = document.createElement("table");
  table.innerHTML =
    "<thead><tr><th>Monitor</th><th>Started</th><th>Ended</th>" +
    "<th>Duration</th><th>Cause</th></tr></thead><tbody></tbody>";
  const body = table.querySelector("tbody");
  for (const incident of incidents) {
    const row = body.insertRow();
    row.className = incident.ended_at ? "" : "ongoing";
    const cells = [
      incident.monitor,
      clock(incident.started_at),
      incident.ended_at ? clock(incident.ended_at) : "ongoing",
      duration(incident.seconds),
      incident.cause || "",
    ];
    for (const value of cells) row.insertCell().textContent = value;
  }
  box.replaceChildren(table);
}

function renderChart(series, windowMinutes) {
  const datasets = series.map((line, index) => ({
    label: line.name,
    data: line.points,
    borderColor: COLORS[index % COLORS.length],
    backgroundColor: COLORS[index % COLORS.length],
    borderWidth: 2,
    pointRadius: 0,
    tension: 0.25,
  }));
  if (chart) {
    chart.data.datasets = datasets;
    chart.update("none"); // no animation: the redraw is every few seconds
    return;
  }
  chart = new Chart(document.getElementById("latency"), {
    type: "line",
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "nearest", intersect: false },
      scales: {
        // x is minutes ago, a plain number, so Chart.js needs no date adapter.
        x: {
          type: "linear",
          min: -windowMinutes,
          max: 0,
          ticks: { callback: (value) => `${value}m` },
        },
        y: { beginAtZero: true, title: { display: true, text: "ms" } },
      },
      plugins: {
        tooltip: {
          callbacks: {
            title: (items) => `${Math.abs(items[0].parsed.x).toFixed(1)} min ago`,
            label: (item) => `${item.dataset.label}: ${item.parsed.y} ms`,
          },
        },
      },
    },
  });
}

async function refresh() {
  const updated = document.getElementById("updated");
  try {
    const response = await fetch("/api/dashboard");
    const data = await response.json();
    renderCards(data.monitors);
    renderChart(data.series, data.window_minutes);
    renderIncidents(data.incidents);
    updated.textContent = `uptime over ${data.uptime_hours}h · updated ${clock(data.now)}`;
  } catch (error) {
    // A missed poll is not worth a broken page: keep what is on screen.
    updated.textContent = "connection lost, retrying…";
  }
}

refresh();
setInterval(refresh, REFRESH_MS);
