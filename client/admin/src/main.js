import "./styles.css";

const API_BASE = import.meta.env.VITE_ADMIN_API_BASE || "http://localhost:8081/api/admin";

const probeHealthEl = document.getElementById("probeHealth");
const metricsEl = document.getElementById("metrics");
const devicesTbody = document.querySelector("#devicesTable tbody");
const refreshBtn = document.getElementById("refreshBtn");
const commandForm = document.getElementById("commandForm");
const commandResultEl = document.getElementById("commandResult");
const eventsLogEl = document.getElementById("eventsLog");
const events = [];

async function fetchJson(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, options);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || response.statusText);
  }
  return data;
}

function renderDevices(devices) {
  devicesTbody.innerHTML = "";
  for (const device of devices) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${device.device_id}</td>
      <td><span class="status status-${device.status}">${device.status}</span></td>
      <td>${device.room_name || "-"}</td>
      <td>${device.last_seen || "-"}</td>
      <td>${device.missed_probes}</td>
    `;
    devicesTbody.appendChild(tr);
  }
}

function appendEvent(line) {
  events.unshift(line);
  if (events.length > 20) {
    events.pop();
  }
  eventsLogEl.textContent = events.join("\n");
}

async function refresh() {
  try {
    const [health, metrics, deviceList] = await Promise.all([
      fetchJson("/probe/health"),
      fetchJson("/metrics"),
      fetchJson("/devices"),
    ]);
    probeHealthEl.textContent = JSON.stringify(health, null, 2);
    metricsEl.textContent = JSON.stringify(metrics, null, 2);
    renderDevices(deviceList.devices);
  } catch (error) {
    probeHealthEl.textContent = String(error);
    metricsEl.textContent = String(error);
  }
}

refreshBtn.addEventListener("click", () => {
  refresh();
});

commandForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const deviceId = document.getElementById("deviceId").value.trim();
  const topic = document.getElementById("topic").value.trim();
  const payloadText = document.getElementById("payload").value.trim();

  try {
    const payload = payloadText ? JSON.parse(payloadText) : {};
    const command = await fetchJson(`/devices/${deviceId}/commands`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic, payload }),
    });
    commandResultEl.textContent = JSON.stringify(command, null, 2);
    await refresh();
  } catch (error) {
    commandResultEl.textContent = String(error);
  }
});

setInterval(refresh, 10000);

const eventSource = new EventSource(`${API_BASE}/stream/events`);
eventSource.addEventListener("message", (event) => {
  appendEvent(`${new Date().toISOString()} ${event.data}`);
  refresh();
});
eventSource.addEventListener("ping", () => {
  appendEvent(`${new Date().toISOString()} ping`);
});
eventSource.onerror = () => {
  appendEvent(`${new Date().toISOString()} stream disconnected`);
};

refresh();
