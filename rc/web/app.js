"use strict";

/* ═══════════════════════════════════════════════════════════════════
   helpers
   ═══════════════════════════════════════════════════════════════════ */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const api = {
  async get(cmd) {
    const res = await fetch("/api/" + cmd, { cache: "no-store" });
    const body = await res.json();
    if (!body.ok) throw new Error(body.error || "ошибка запроса");
    return body.data;
  },
  async post(cmd, payload) {
    const res = await fetch("/api/" + cmd, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
    const body = await res.json();
    if (!body.ok) throw new Error(body.error || "ошибка запроса");
    return body.data;
  },
};

let toastTimer = null;
function toast(message, isError) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("err", !!isError);
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 4000);
}

function humanSize(bytes) {
  if (bytes < 1024) return bytes + " Б";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " КБ";
  return (bytes / 1024 / 1024).toFixed(1) + " МБ";
}

function fmtUptime(seconds) {
  if (!seconds) return "";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h) return h + " ч " + m + " мин";
  return m + " мин";
}

// ═══════════════════════════════════════════════════════════════════
// METRICS
// ═══════════════════════════════════════════════════════════════════
async function refreshMetrics() {
  try {
    const data = await api.get("sys.info");

    // CPU load
    const load1 = data.load?.["1"] || 0;
    const cpuEl = $("#m-cpu");
    cpuEl.textContent = load1.toFixed(2);
    cpuEl.parentElement.className = "metric-item" + (load1 > 3 ? " danger" : load1 > 1.5 ? " warn" : "");

    // Memory
    const memPct = data.memory?.pct || 0;
    const memEl = $("#m-mem");
    memEl.textContent = memPct.toFixed(0) + "% (" + (data.memory?.used_gb || 0) + "/" + (data.memory?.total_gb || "?") + " ГБ)";
    memEl.parentElement.className = "metric-item" + (memPct > 90 ? " danger" : memPct > 70 ? " warn" : "");

    // Disk
    const diskEl = $("#m-disk");
    if (data.disks?.length) {
      diskEl.textContent = data.disks.map(d => d.pct.toFixed(0) + "%").join(", ");
    }

    // Temps
    const tempEl = $("#m-temp");
    if (data.temps && Object.keys(data.temps).length) {
      const maxT = Math.max(...Object.values(data.temps));
      tempEl.textContent = Object.entries(data.temps)
        .map(([k, v]) => k + " " + v.toFixed(0) + "°C")
        .join(" · ");
      tempEl.parentElement.className = "metric-item" + (maxT > 85 ? " danger" : maxT > 70 ? " warn" : "");
    }

    // Uptime from status
  } catch (err) {
    console.warn("metrics error:", err);
  }
}

// ═══════════════════════════════════════════════════════════════════
// STATUS
// ═══════════════════════════════════════════════════════════════════
async function refreshStatus() {
  const pill = $("#status");
  try {
    const status = await api.get("status");
    const info = status.agent.info || {};
    pill.className = "pill ok";
    pill.textContent =
      "на связи · " + (info.host || "agent") + " · " +
      (info.user || "?") + " · " +
      (info.screen ? info.screen.width + "x" + info.screen.height : "");

    // Uptime
    const up = $("#m-uptime");
    if (info.uptime) {
      up.textContent = fmtUptime(info.uptime);
    }
  } catch (err) {
    pill.className = "pill bad";
    pill.textContent = "агент недоступен: " + err.message;
  }
}

// ═══════════════════════════════════════════════════════════════════
// APP LAUNCHERS
// ═══════════════════════════════════════════════════════════════════
function renderAppLaunchers() {
  const container = $("#app-launchers");
  const apps = [
    { name: "firefox", label: "🦊 Firefox" },
    { name: "terminal", label: "⬛ Терминал" },
  ];
  container.innerHTML = "";
  for (const app of apps) {
    const btn = document.createElement("button");
    btn.className = "app-btn";
    btn.textContent = app.label;
    btn.addEventListener("click", async () => {
      try {
        await api.post("app.launch", { name: app.name });
        toast("Запуск: " + app.label);
      } catch (err) {
        toast("Ошибка запуска: " + err.message, true);
      }
    });
    container.appendChild(btn);
  }
}

// ═══════════════════════════════════════════════════════════════════
// WINDOWS
// ═══════════════════════════════════════════════════════════════════
let windowsCache = [];

async function refreshWindows() {
  const tbody = $("#windows tbody");
  try {
    const data = await api.get("win.list");
    windowsCache = data.windows || [];
    tbody.innerHTML = "";
    for (const win of windowsCache) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td></td><td></td><td></td><td></td>" +
        '<td><button class="small" data-act="focus">фокус</button> ' +
        '<button class="small" data-act="close">закрыть</button></td>';
      const cells = tr.querySelectorAll("td");
      cells[0].textContent = win.title || "(пусто)";
      cells[1].textContent = win.class || "";
      cells[2].textContent = win.pid || "";
      cells[3].textContent = (win.w || "?") + "×" + (win.h || "?");
      cells[4].dataset.id = win.id;
      tbody.appendChild(tr);
    }
  } catch (err) {
    toast("Не удалось получить список окон: " + err.message, true);
  }
}

$("#windows").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-act]");
  if (!button) return;
  const id = Number(button.closest("td").dataset.id);
  const win = windowsCache.find((w) => w.id === id) || {};
  try {
    if (button.dataset.act === "focus") {
      await api.post("win.focus", { id });
      toast("Фокус: " + win.title);
    } else if (button.dataset.act === "close") {
      if (!confirm("Закрыть окно «" + (win.title || "?") + "»?")) return;
      await api.post("win.close", { id });
      toast("Окно закрыто");
      refreshWindows();
    }
  } catch (err) {
    toast(err.message, true);
  }
});

// ═══════════════════════════════════════════════════════════════════
// POWER
// ═══════════════════════════════════════════════════════════════════
$("#btn-wake").addEventListener("click", async () => {
  try {
    const result = await api.post("wake");
    toast(result.result || "Команда включения отправлена");
  } catch (err) {
    toast(err.message, true);
  }
});

$("#btn-reboot").addEventListener("click", async () => {
  if (!confirm("Перезагрузить удалённый ПК?")) return;
  try {
    await api.post("power", { action: "reboot" });
    toast("Перезагрузка запущена");
  } catch (err) {
    toast(err.message, true);
  }
});

$("#btn-off").addEventListener("click", async () => {
  if (!confirm("Выключить удалённый ПК?")) return;
  try {
    await api.post("power", { action: "off" });
    toast("Выключение запущено");
  } catch (err) {
    toast(err.message, true);
  }
});

// ═══════════════════════════════════════════════════════════════════
// CLIPBOARD
// ═══════════════════════════════════════════════════════════════════
$("#btn-clip-get").addEventListener("click", async () => {
  try {
    const data = await api.get("clip.get");
    $("#clip").value = data.text || "";
    toast("Буфер обмена получен (" + (data.text || "").length + " символов)");
  } catch (err) {
    toast(err.message, true);
  }
});

$("#btn-clip-set").addEventListener("click", async () => {
  try {
    await api.post("clip.set", { text: $("#clip").value });
    toast("Буфер обмена отправлен на удалённый ПК");
  } catch (err) {
    toast(err.message, true);
  }
});

$("#btn-clip-copy").addEventListener("click", async () => {
  const text = $("#clip").value;
  try {
    await navigator.clipboard.writeText(text);
    toast("Скопировано в буфер обмена этого компьютера");
  } catch (err) {
    toast("Не удалось скопировать: " + err.message, true);
  }
});

// ═══════════════════════════════════════════════════════════════════
// TERMINAL
// ═══════════════════════════════════════════════════════════════════
let termConnected = false;
let termTid = null;
let termWs = null;

const termEl = $("#terminal");
let termInstance = null;
let fitAddonInstance = null;
let resizeTimer = null;
let lastCols = 0;
let lastRows = 0;

function termInitTerminal() {
  const termElInner = document.createElement("div");
  termElInner.className = "xterm";
  termEl.innerHTML = "";
  termEl.appendChild(termElInner);

  termInstance = new Terminal({
    cursorBlink: true,
    fontFamily: '"SF Mono", Menlo, Monaco, Consolas, ui-monospace, monospace',
    fontSize: 13,
    lineHeight: 1.3,
    theme: {
      background: "#0d1015",
      foreground: "#d4d4d4",
      cursor: "#d4d4d4",
      selectionBackground: "#ffffff40",
    },
  });

  fitAddonInstance = new FitAddon.FitAddon();
  termInstance.loadAddon(fitAddonInstance);

  termInstance.open(termElInner);
}

function termConnectWs() {
  try {
    if (termInstance) {
      termInstance.dispose();
      termInstance = null;
    }
    if (fitAddonInstance) {
      fitAddonInstance = null;
    }

    const wsUrl = "ws://" + location.host + "/term.ws";
    termWs = new WebSocket(wsUrl);
    termWs.binaryType = "arraybuffer";

    termWs.onopen = () => {
      console.log("terminal WS connected");
      termInitTerminal();
      // Register input handler after terminal is created
      termInstance.onData((d) => {
        if (termWs && termWs.readyState === WebSocket.OPEN) {
          termWs.send(JSON.stringify({ cmd: "term.data", data: d }));
        }
      });
      // Fit after the terminal is opened and visible
      requestAnimationFrame(() => {
        if (fitAddonInstance) fitAddonInstance.fit();
        if (termInstance) {
          const cols = termInstance.cols;
          const rows = termInstance.rows;
          if (cols !== lastCols || rows !== lastRows) {
            termWs.send(JSON.stringify({ cmd: "term.resize", cols: cols, rows: rows }));
            lastCols = cols;
            lastRows = rows;
          }
        }
      });

      // ResizeObserver for container size changes
      new ResizeObserver((entries) => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(() => {
          if (fitAddonInstance) fitAddonInstance.fit();
          if (termInstance) {
            const cols = termInstance.cols;
            const rows = termInstance.rows;
            if (cols !== lastCols || rows !== lastRows) {
              termWs.send(JSON.stringify({ cmd: "term.resize", cols: cols, rows: rows }));
              lastCols = cols;
              lastRows = rows;
            }
          }
        }, 150);
      }).observe(termEl);
    };

    termWs.onmessage = (event) => {
      if (!termInstance) return;
      try {
        let msg;
        if (typeof event.data === "string") {
          try {
            msg = JSON.parse(event.data);
          } catch (e) {
            // Not JSON — treat as raw terminal output
            if (event.data) termInstance.write(event.data);
            return;
          }
        } else {
          // Binary frame — decode as UTF-8
          const text = new TextDecoder().decode(event.data);
          try {
            msg = JSON.parse(text);
          } catch (e) {
            if (text) termInstance.write(text);
            return;
          }
        }
        if (!msg || typeof msg !== "object") return;
        if (msg.cmd === "term.data") {
          termInstance.write(String(msg.data ?? ""));
        } else if (msg.cmd === "term.clear") {
          termInstance.clear();
        } else if (msg.cmd === "term.error" || msg.cmd === "term.err") {
          termInstance.write("\r\n\x1b[31m[error: " + String(msg.data ?? msg.error ?? "unknown") + "]\x1b[0m\r\n");
        }
      } catch (err) {
        console.error("term WS message error:", err);
      }
    };

    termWs.onclose = () => {
      console.log("terminal WS closed");
      termConnected = false;
      termTid = null;
      if (termInstance) {
        termInstance.dispose();
        termInstance = null;
      }
      $("#term-status").textContent = "отключён";
      $("#term-connect").textContent = "подключить";
    };

    termWs.onerror = () => {
      console.log("terminal WS error");
    };
  } catch (err) {
    toast("Ошибка терминала: " + err.message, true);
  }
}

async function termConnect() {
  try {
    termConnectWs();
    termConnected = true;
    $("#term-status").textContent = "подключён";
    $("#term-connect").textContent = "отключить";
  } catch (err) {
    toast("Ошибка терминала: " + err.message, true);
  }
}

async function termClose() {
  if (!termConnected) return;
  if (termWs) {
    termWs.close();
    termWs = null;
  }
  termConnected = false;
  termTid = null;
  if (termInstance) {
    termInstance.dispose();
    termInstance = null;
  }
  $("#term-status").textContent = "отключён";
  $("#term-connect").textContent = "подключить";
}

$("#term-connect").addEventListener("click", () => {
  if (termConnected) {
    termClose();
  } else {
    termConnect();
  }
});

// ═══════════════════════════════════════════════════════════════════
// HIDE/SHOW TOOLBAR (F11 toggle)
// ═══════════════════════════════════════════════════════════════════
let toolbarHidden = false;

function toggleToolbar() {
  toolbarHidden = !toolbarHidden;
  document.body.classList.toggle("hide-header", toolbarHidden);
  $("#btn-hide").textContent = toolbarHidden ? "☰" : "☰";
  $("#btn-hide").title = toolbarHidden ? "Показать панель" : "Скрыть панель (F11)";
}

$("#btn-hide").addEventListener("click", toggleToolbar);

// Double-click on body to show toolbar if hidden
document.addEventListener("dblclick", (e) => {
  if (toolbarHidden && e.clientY < 60) {
    toggleToolbar();
  }
});

// ═══════════════════════════════════════════════════════════════════
// MISC
// ═══════════════════════════════════════════════════════════════════
$("#btn-viewer").addEventListener("click", () => {
  const source = "screen";
  fetch("/api/viewer.open", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source }),
  })
    .then((r) => r.json())
    .then((body) => {
      if (!body.ok) alert("не удалось открыть окно: " + body.error);
    })
    .catch((err) => alert("не удалось открыть окно: " + err));
});

$("#btn-refresh").addEventListener("click", () => {
  refreshStatus();
  refreshWindows();
});

// ═══════════════════════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════════════════════
renderAppLaunchers();
refreshStatus();
refreshWindows();
refreshMetrics();

// Poll intervals
setInterval(refreshStatus, 5000);
setInterval(refreshWindows, 5000);
setInterval(refreshMetrics, 10000);
