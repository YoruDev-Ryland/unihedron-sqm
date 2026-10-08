"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)");

const state = {
  csrf: null,
  page: "dashboard",
  hours: 24,
  chart: [],
  geometry: null,
  hover: -1,
  annual: null,
  annualGeometry: null,
  annualHover: null,
  annualReveal: null,
  hideMoonlit: false,
  chartReveal: null,
  files: [],
  timer: null,
};

const pageTitles = {
  dashboard: "Overview",
  device: "Meter",
  import: "Data",
  settings: "Settings",
  account: "Account",
};

const collectorLabels = {
  collecting: "Collecting",
  connecting: "Connecting",
  offline: "Meter offline",
  unconfigured: "Setup needed",
  error: "Collector error",
  starting: "Starting",
};

const collectorNotes = {
  collecting: "Readings are arriving on schedule.",
  connecting: "Opening a connection to the meter.",
  offline: "The meter did not answer the last poll.",
  unconfigured: "No meter address saved yet.",
  error: "Collection stopped on an error.",
  starting: "Waiting for the first poll.",
};

/* ------------------------------------------------------------------ helpers */

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  if (!(options.body instanceof FormData) && options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (method !== "GET" && method !== "HEAD" && state.csrf) {
    headers.set("X-CSRF-Token", state.csrf);
  }
  const response = await fetch(path, { ...options, headers });
  let data;
  try {
    data = await response.json();
  } catch {
    data = {};
  }
  if (!response.ok) {
    const detail = data.detail;
    const error = new Error(detail?.message || detail || `Request failed (${response.status})`);
    error.status = response.status;
    error.detail = detail;
    throw error;
  }
  return data;
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show${error ? " error" : ""}`;
  clearTimeout(element.timer);
  element.timer = setTimeout(() => { element.className = "toast"; }, 3800);
}

function setMessage(element, message, type = "") {
  element.textContent = message;
  element.className = `form-message ${type}`;
}

function formatNumber(value, digits = 2) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  return Number(value).toFixed(digits);
}

function formatInteger(value) {
  if (value === null || value === undefined) return "—";
  return Number(value).toLocaleString();
}

function formatDate(value, includeDate = true) {
  if (!value) return "Never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, {
    ...(includeDate ? { dateStyle: "medium" } : {}),
    timeStyle: "short",
  }).format(date);
}

function shortDate(value) {
  if (!value) return "Never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const today = new Date();
  const sameDay = date.toDateString() === today.toDateString();
  return new Intl.DateTimeFormat(undefined, {
    ...(sameDay ? {} : { month: "short", day: "numeric" }),
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

function formatUnix(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0
    ? formatDate(new Date(number * 1000).toISOString())
    : "Never";
}

/* -------------------------------------------------------------------- theme */

const THEME_KEY = "sqm-theme";
const THEME_BASE = { dark: "#060810", light: "#f2f2ef" };

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function syncThemeUI() {
  const theme = currentTheme();
  const meta = $('meta[name="theme-color"]');
  if (meta) meta.content = THEME_BASE[theme];
  $("#theme-button").setAttribute("aria-pressed", String(theme === "light"));
  $("#theme-tip").textContent = theme === "light" ? "Dark mode" : "Light mode";
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem(THEME_KEY, theme); } catch { /* private mode */ }
  syncThemeUI();
  syncMeteorUI();
  renderChart();
  renderAnnualMap();
}

const REVEAL_MS = 620;

// Circular wipe out of the toggle: the new theme grows from under the button.
// The animation itself lives in CSS (see @keyframes theme-reveal) so it is in
// place before the first frame renders; here we only publish its geometry.
function revealTheme(theme, x, y) {
  const root = document.documentElement;
  root.style.setProperty("--reveal-x", `${x}px`);
  root.style.setProperty("--reveal-y", `${y}px`);
  root.style.setProperty("--reveal-r", `${Math.hypot(
    Math.max(x, window.innerWidth - x),
    Math.max(y, window.innerHeight - y),
  )}px`);

  if (REDUCED_MOTION.matches) {
    applyTheme(theme);
    return;
  }

  if (typeof document.startViewTransition === "function") {
    const transition = document.startViewTransition(() => applyTheme(theme));
    // A skipped transition rejects these; the swap has still happened.
    transition.ready.catch(() => {});
    transition.finished.catch(() => {});
    transition.updateCallbackDone.catch(() => {});
    // Safety net: the swap must land even if the transition is skipped.
    setTimeout(() => { if (currentTheme() !== theme) applyTheme(theme); }, REVEAL_MS + 200);
    return;
  }

  const overlay = document.createElement("div");
  overlay.className = "theme-reveal";
  overlay.style.background = THEME_BASE[theme];
  const settle = () => {
    if (!overlay.isConnected) return;
    applyTheme(theme);
    overlay.remove();
  };
  overlay.addEventListener("animationend", settle, { once: true });
  document.body.append(overlay);
  setTimeout(settle, REVEAL_MS + 200);
}

/* --------------------------------------------------------------------- sky */

const sky = { size: 0, meteorTimer: null, meteors: [], drawing: false };
const METEOR_KEY = "sqm-meteors";
// Delay range between meteors, in milliseconds, for each frequency setting.
const METEOR_RATES = {
  off: null,
  rare: [60000, 150000],
  normal: [25000, 75000],
  frequent: [4000, 12000],
  shower: [60, 420],
};
const STAR_TINTS = ["255, 255, 255", "255, 244, 226", "222, 232, 255", "255, 230, 200"];

// Deterministic, so the same constellations come back on every visit.
function seededRandom(seed) {
  let value = seed;
  return () => {
    value = (value + 0x6d2b79f5) | 0;
    let t = Math.imul(value ^ (value >>> 15), 1 | value);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function gaussian(random) {
  return Math.sqrt(-2 * Math.log(random() || 1e-9)) * Math.cos(2 * Math.PI * random());
}

// Paint the star field once. CSS turns the canvas around the pole, so the
// canvas must reach every viewport corner at any angle.
function drawStars() {
  const poleX = window.innerWidth * 0.66;
  const poleY = window.innerHeight * -0.06;
  const reach = Math.max(
    Math.hypot(poleX, poleY),
    Math.hypot(window.innerWidth - poleX, poleY),
    Math.hypot(poleX, window.innerHeight - poleY),
    Math.hypot(window.innerWidth - poleX, window.innerHeight - poleY),
  );
  const size = Math.ceil(reach * 2 + 40);
  const root = document.documentElement.style;
  root.setProperty("--pole-x", `${poleX}px`);
  root.setProperty("--pole-y", `${poleY}px`);
  root.setProperty("--sky-size", `${size}px`);
  if (size <= sky.size) return;
  sky.size = size;

  const canvas = $("#sky-stars");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  const random = seededRandom(7942);
  const centre = size / 2;

  // A faint Milky Way: soft glow plus a dense drift of tiny stars along a band.
  const bandAngle = -0.62;
  const along = { x: Math.cos(bandAngle), y: Math.sin(bandAngle) };
  const across = { x: -along.y, y: along.x };
  const bandOffset = size * 0.18;
  for (let index = 0; index < 160; index += 1) {
    const t = (random() - 0.5) * size;
    const spread = gaussian(random) * size * 0.05;
    const x = centre + along.x * t + across.x * (spread + bandOffset);
    const y = centre + along.y * t + across.y * (spread + bandOffset);
    const radius = size * (0.03 + random() * 0.05);
    const glow = ctx.createRadialGradient(x, y, 0, x, y, radius);
    glow.addColorStop(0, "rgba(190, 196, 222, 0.022)");
    glow.addColorStop(1, "rgba(190, 196, 222, 0)");
    ctx.fillStyle = glow;
    ctx.fillRect(x - radius, y - radius, radius * 2, radius * 2);
  }
  const dust = Math.round(size * size / 1400);
  for (let index = 0; index < dust; index += 1) {
    const t = (random() - 0.5) * size;
    const spread = gaussian(random) * size * 0.06;
    const x = centre + along.x * t + across.x * (spread + bandOffset);
    const y = centre + along.y * t + across.y * (spread + bandOffset);
    ctx.fillStyle = `rgba(${STAR_TINTS[index % 4]}, ${0.12 + random() * 0.28})`;
    ctx.fillRect(x, y, 0.8, 0.8);
  }

  // The general field: mostly faint points, a few brighter stars with halos.
  // Faint stars are whole pixels so they stay crisp at 1x; only the rare
  // bright ones are round, with a small halo.
  const field = Math.round(size * size / 1700);
  for (let index = 0; index < field; index += 1) {
    const x = Math.round(random() * size);
    const y = Math.round(random() * size);
    const magnitude = random() ** 9;
    const tint = STAR_TINTS[Math.floor(random() * 4)];
    if (magnitude < 0.12) {
      ctx.fillStyle = `rgba(${tint}, ${0.28 + random() * 0.42})`;
      ctx.fillRect(x, y, 1, 1);
      continue;
    }
    const radius = 0.7 + magnitude * 1.1;
    ctx.fillStyle = `rgba(${tint}, ${Math.min(1, 0.7 + magnitude)})`;
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fill();
    if (magnitude > 0.45) {
      const halo = ctx.createRadialGradient(x, y, 0, x, y, radius * 3.5);
      halo.addColorStop(0, `rgba(${tint}, 0.18)`);
      halo.addColorStop(1, `rgba(${tint}, 0)`);
      ctx.fillStyle = halo;
      ctx.fillRect(x - radius * 3.5, y - radius * 3.5, radius * 7, radius * 7);
    }
  }
}

function meteorSetting() {
  try {
    const stored = localStorage.getItem(METEOR_KEY);
    if (stored in METEOR_RATES) return stored;
  } catch { /* private mode */ }
  return "normal";
}

function scheduleMeteor(first = false) {
  clearTimeout(sky.meteorTimer);
  const rate = METEOR_RATES[meteorSetting()];
  if (!rate) return;
  const [low, high] = rate;
  const delay = first ? Math.min(low, 6000) : low + Math.random() * (high - low);
  sky.meteorTimer = setTimeout(launchMeteor, delay);
}

// A random downward path whose start and end both sit inside the viewport,
// so the whole streak is visible. Returns null if none fits.
function meteorPath(width, height) {
  const margin = Math.min(width, height) * 0.05 + 12;
  for (let attempt = 0; attempt < 24; attempt += 1) {
    const angle = Math.PI * (0.08 + Math.random() * 0.84);
    const travel = Math.min(width, height) * (0.22 + Math.random() * 0.3);
    const startX = margin + Math.random() * (width - margin * 2);
    const startY = margin + Math.random() * (height - margin * 2);
    const endX = startX + Math.cos(angle) * travel;
    const endY = startY + Math.sin(angle) * travel;
    if (endX >= margin && endX <= width - margin && endY >= margin && endY <= height - margin) {
      return { startX, startY, angle, travel };
    }
  }
  return null;
}

function launchMeteor() {
  scheduleMeteor();
  if (document.hidden || REDUCED_MOTION.matches || currentTheme() !== "dark") return;
  const path = meteorPath(window.innerWidth, window.innerHeight);
  if (!path) return;
  sky.meteors.push({
    ...path,
    tailLength: 90 + Math.random() * 110,
    duration: 750 + Math.random() * 400,
    started: performance.now(),
  });
  if (!sky.drawing) {
    sky.drawing = true;
    requestAnimationFrame(drawMeteors);
  }
}

// One loop draws every meteor in flight, then stops when none are left.
function drawMeteors(now) {
  const canvas = $("#meteors");
  const ratio = window.devicePixelRatio || 1;
  const width = window.innerWidth;
  const height = window.innerHeight;
  if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.lineWidth = 1.6;
  ctx.lineCap = "round";

  sky.meteors = sky.meteors.filter((meteor) => now - meteor.started < meteor.duration);
  sky.meteors.forEach((meteor) => {
    const progress = Math.max(0, (now - meteor.started) / meteor.duration);
    const flown = meteor.travel * progress;
    const headX = meteor.startX + Math.cos(meteor.angle) * flown;
    const headY = meteor.startY + Math.sin(meteor.angle) * flown;
    // The tail never reaches back past where the meteor appeared.
    const tail = Math.min(meteor.tailLength, flown);
    const tailX = headX - Math.cos(meteor.angle) * tail;
    const tailY = headY - Math.sin(meteor.angle) * tail;
    const fade = progress < 0.75 ? 1 : 1 - (progress - 0.75) / 0.25;
    const streak = ctx.createLinearGradient(headX, headY, tailX, tailY);
    streak.addColorStop(0, `rgba(255, 248, 236, ${0.95 * fade})`);
    streak.addColorStop(0.25, `rgba(255, 214, 150, ${0.45 * fade})`);
    streak.addColorStop(1, "rgba(255, 214, 150, 0)");
    ctx.strokeStyle = streak;
    ctx.beginPath();
    ctx.moveTo(headX, headY);
    ctx.lineTo(tailX, tailY);
    ctx.stroke();
  });

  if (sky.meteors.length) {
    requestAnimationFrame(drawMeteors);
  } else {
    sky.drawing = false;
  }
}

function setMeteorSetting(setting) {
  try { localStorage.setItem(METEOR_KEY, setting); } catch { /* private mode */ }
  syncMeteorUI();
  scheduleMeteor(true);
}

function syncMeteorUI() {
  const setting = meteorSetting();
  $$(".seg-btn[data-meteors]").forEach((button) => button.classList.toggle("is-active", button.dataset.meteors === setting));
  positionThumbs();
  $("#meteor-note").textContent = REDUCED_MOTION.matches
    ? "Your system’s reduced-motion setting keeps meteors off."
    : currentTheme() === "dark" ? "" : "Meteors appear in the dark theme.";
}

function startSky() {
  drawStars();
  syncMeteorUI();
  scheduleMeteor(true);
  let skyTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(skyTimer);
    skyTimer = setTimeout(drawStars, 200);
  });
}

/* ------------------------------------------------------------------- views */

function showLogin() {
  $("#app-view").hidden = true;
  $("#login-view").hidden = false;
  clearInterval(state.timer);
  setTimeout(() => $("#login-password").focus(), 40);
}

function showApp(auth) {
  state.csrf = auth.csrf_token;
  $("#login-view").hidden = true;
  $("#app-view").hidden = false;
  $("#password-banner").hidden = !auth.must_change_password;
  navigate("dashboard");
  requestAnimationFrame(positionThumbs);
  loadConfig();
  loadDashboard({ reveal: true });
  clearInterval(state.timer);
  state.timer = setInterval(() => {
    if (state.page === "dashboard") loadDashboard({ silent: true });
  }, 30000);
}

function navigate(page) {
  state.page = page;
  $$(".page").forEach((section) => section.classList.toggle("is-active", section.id === `page-${page}`));
  $$(".rail-btn[data-page]").forEach((button) => button.classList.toggle("is-active", button.dataset.page === page));
  $("#page-title").textContent = pageTitles[page] || page;
  $(".console-scroll").scrollTop = 0;
  if (page === "dashboard") {
    requestAnimationFrame(() => {
      positionThumbs();
      renderChart();
      renderAnnualMap();
    });
    loadDashboard({ silent: true });
    if (!state.annual) loadAnnual();
  }
  if (page === "import") loadImportHistory();
  if (page === "settings") loadSettings();
  if (page === "account") requestAnimationFrame(syncMeteorUI);
  if (page === "device") {
    loadConfig();
    loadMeter();
  }
}

/* --------------------------------------------------------------- dashboard */

function easeOutCubic(t) {
  return 1 - (1 - t) ** 3;
}

// Rolls a displayed number to its new value, so a fresh reading is noticed.
function animateNumber(element, target, digits) {
  const next = Number(target);
  const previous = Number.parseFloat(element.textContent);
  if (!Number.isFinite(next)) {
    element.textContent = "—";
    return;
  }
  if (REDUCED_MOTION.matches || !Number.isFinite(previous) || previous === next) {
    element.textContent = formatNumber(next, digits);
    return;
  }
  const started = performance.now();
  const duration = 700;
  cancelAnimationFrame(element.rollFrame);
  const step = (now) => {
    const progress = Math.min(1, (now - started) / duration);
    element.textContent = formatNumber(previous + (next - previous) * easeOutCubic(progress), digits);
    if (progress < 1) element.rollFrame = requestAnimationFrame(step);
  };
  element.rollFrame = requestAnimationFrame(step);
}

function updateSky(latest) {
  const bortle = latest?.bortle ?? null;
  $$("#bortle li").forEach((item) => item.classList.toggle("is-on", Number(item.dataset.class) === bortle));
  if (bortle) {
    $("#bortle-name").textContent = latest.bortle_name;
    $("#bortle-detail").textContent = `Bortle class ${bortle}, naked-eye limit about magnitude ${formatNumber(latest.nelm, 1)}`;
  } else {
    $("#bortle-name").textContent = latest ? "Too bright for a night-sky class" : "Waiting for a reading";
    $("#bortle-detail").textContent = latest ? "The meter is reading daylight or twilight." : "";
  }
}

function updateCollector(collector) {
  const label = collectorLabels[collector.state] || collector.state;
  const good = collector.state === "collecting";
  const bad = collector.state === "offline" || collector.state === "error";
  const tone = good ? "good" : bad ? "bad" : "";

  $("#collector-word").textContent = label;
  $("#side-status").textContent = label;
  $("#side-status-dot").className = tone;
  $("#live-dot").className = `live-dot ${tone}`;
  const note = $("#collector-note");
  note.textContent = collector.last_error || "";
  note.hidden = good || !collector.last_error;
}

function moonPhaseName(fraction, waxing) {
  if (fraction < 0.03) return "New moon";
  if (fraction > 0.97) return "Full moon";
  if (Math.abs(fraction - 0.5) < 0.04) return waxing ? "First quarter" : "Last quarter";
  return `${waxing ? "Waxing" : "Waning"} ${fraction < 0.5 ? "crescent" : "gibbous"}`;
}

async function loadDashboard({ silent = false, reveal = false } = {}) {
  try {
    const data = await api(`/api/dashboard?hours=${state.hours}`);
    const latest = data.latest;
    const device = data.device;

    updateCollector(data.collector);
    updateSky(latest);
    animateNumber($("#latest-mpsas"), latest ? latest.mpsas : NaN, 2);
    $("#latest-temp").textContent = latest ? `${formatNumber(latest.temperature_c, 1)} °C` : "—";
    $("#latest-time").textContent = latest ? shortDate(latest.timestamp) : "Waiting";
    const moon = data.moon;
    $("#moon-fact").textContent = `${moonPhaseName(moon.fraction, moon.waxing)}${
      moon.up === null ? "" : moon.up ? ", up" : ", down"}`;
    const meterLabel = data.collector.transport === "serial" ? "USB SQM" : "SQM‑LE";
    $("#station-name").textContent = device?.serial ? `${meterLabel} ${device.serial}` : "";

    $("#range-count").textContent = formatInteger(data.stats.count);
    $("#range-avg").textContent = formatNumber(data.stats.avg_mpsas);
    $("#range-max").textContent = formatNumber(data.stats.max_mpsas);
    $("#range-min").textContent = formatNumber(data.stats.min_mpsas);

    state.chart = data.chart || [];
    state.hover = -1;
    if (reveal) revealChart(); else renderChart();
    if (data.collector.last_error && !silent) toast(data.collector.last_error, true);
  } catch (error) {
    if (error.status === 401) return showLogin();
    if (!silent) toast(error.message, true);
  }
}

/* ------------------------------------------------------------------- chart */

function timeFormatter() {
  if (state.hours <= 36) return { hour: "numeric", minute: "2-digit" };
  if (state.hours <= 336) return { weekday: "short", hour: "numeric" };
  if (state.hours <= 2160) return { month: "short", day: "numeric" };
  return { month: "short", year: "2-digit" };
}

function renderChart() {
  const canvas = $("#history-chart");
  const points = state.chart;
  $("#chart-empty").hidden = points.length > 0;

  const rect = canvas.getBoundingClientRect();
  if (rect.width < 2 || rect.height < 2) return;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * ratio);
  canvas.height = Math.round(rect.height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, rect.width, rect.height);
  if (!points.length) {
    state.geometry = null;
    $("#chart-tip").hidden = true;
    return;
  }

  const pad = { top: 16, right: 12, bottom: 28, left: 42 };
  const plotWidth = rect.width - pad.left - pad.right;
  const plotHeight = rect.height - pad.top - pad.bottom;

  const values = points.map((point) => Number(point.mpsas));
  const times = points.map((point) => Number(point.ts) * 1000);
  const { low, high, step: tickStep } = niceScale(Math.min(...values), Math.max(...values));
  const firstTime = times[0];
  const lastTime = times[times.length - 1];
  const span = Math.max(1, lastTime - firstTime);

  const x = (time) => pad.left + ((time - firstTime) / span) * plotWidth;
  const y = (value) => pad.top + (1 - (value - low) / (high - low)) * plotHeight;
  state.geometry = { x, y, times, values, pad, width: rect.width, height: rect.height };

  const paint = {
    line: cssVar("--accent"),
    fill: cssVar("--chart-fill"),
    grid: cssVar("--chart-grid"),
    label: cssVar("--chart-label"),
    mark: cssVar("--chart-mark"),
    core: cssVar("--dot-core"),
    glow: cssVar("--accent-glow"),
  };

  ctx.font = canvasFont(12);
  ctx.fillStyle = paint.label;
  ctx.strokeStyle = paint.grid;
  ctx.lineWidth = 1;
  const tickDigits = tickStep < 1 ? 1 : 0;
  for (let value = low; value <= high + tickStep / 2; value += tickStep) {
    const py = Math.round(y(value)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(pad.left, py);
    ctx.lineTo(rect.width - pad.right, py);
    ctx.stroke();
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.fillText(value.toFixed(tickDigits), pad.left - 9, py);
  }

  const labelFormat = new Intl.DateTimeFormat(undefined, timeFormatter());
  const columns = rect.width < 460 ? 2 : 4;
  ctx.textBaseline = "alphabetic";
  for (let step = 0; step <= columns; step += 1) {
    const time = firstTime + (span * step) / columns;
    ctx.textAlign = step === 0 ? "left" : step === columns ? "right" : "center";
    ctx.fillText(labelFormat.format(new Date(time)), x(time), rect.height - 7);
  }

  const trace = () => {
    ctx.beginPath();
    points.forEach((point, index) => {
      const px = x(times[index]);
      const py = y(values[index]);
      if (index === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    });
  };

  // During the draw-in, everything right of the reveal edge stays hidden.
  ctx.save();
  if (state.chartReveal !== null) {
    ctx.beginPath();
    ctx.rect(0, 0, pad.left + plotWidth * state.chartReveal, rect.height);
    ctx.clip();
  }

  const gradient = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotHeight);
  gradient.addColorStop(0, paint.fill);
  gradient.addColorStop(1, "transparent");
  trace();
  ctx.lineTo(x(lastTime), pad.top + plotHeight);
  ctx.lineTo(x(firstTime), pad.top + plotHeight);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();

  trace();
  ctx.strokeStyle = paint.line;
  ctx.lineWidth = 1.7;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.stroke();

  const lastX = x(lastTime);
  const lastY = y(values[values.length - 1]);
  ctx.beginPath();
  ctx.arc(lastX, lastY, 3.4, 0, Math.PI * 2);
  ctx.fillStyle = paint.mark;
  ctx.shadowColor = paint.glow;
  ctx.shadowBlur = 10;
  ctx.fill();
  ctx.shadowBlur = 0;
  ctx.restore();

  const tip = $("#chart-tip");
  if (state.hover >= 0 && state.hover < points.length) {
    const hx = x(times[state.hover]);
    const hy = y(values[state.hover]);
    ctx.beginPath();
    ctx.setLineDash([3, 4]);
    ctx.moveTo(hx, pad.top);
    ctx.lineTo(hx, pad.top + plotHeight);
    ctx.strokeStyle = paint.label;
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.beginPath();
    ctx.arc(hx, hy, 4.2, 0, Math.PI * 2);
    ctx.fillStyle = paint.core;
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = paint.mark;
    ctx.stroke();

    const point = points[state.hover];
    tip.replaceChildren();
    const value = document.createElement("strong");
    value.textContent = `${formatNumber(point.mpsas)} mag/arcsec²`;
    const when = document.createElement("span");
    when.textContent = formatDate(new Date(times[state.hover]).toISOString());
    tip.append(value, when);
    const temperature = Number(point.temperature_c);
    if (Number.isFinite(temperature)) {
      const sensor = document.createElement("span");
      sensor.textContent = `Sensor ${formatNumber(temperature, 1)} °C`;
      tip.append(sensor);
    }
    tip.hidden = false;
    tip.style.left = `${Math.max(70, Math.min(rect.width - 70, hx))}px`;
    tip.style.top = `${hy}px`;
  } else {
    tip.hidden = true;
  }
}

// Round axis steps (0.5, 1, 2…) instead of arbitrary fractions of the range.
function niceScale(min, max) {
  let low = min;
  let high = max;
  if (high - low < 1) { low -= 0.5; high += 0.5; }
  const rough = (high - low) / 5;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].map((factor) => factor * magnitude).find((candidate) => candidate >= rough);
  return {
    low: Math.floor(low / step) * step,
    high: Math.ceil(high / step) * step,
    step,
  };
}

function canvasFont(size, weight = 400) {
  return `${weight} ${size}px ${getComputedStyle(document.body).fontFamily}`;
}

// Animate a 0→1 progress value, calling `frame` each step and once at the end
// with null. Reduced motion skips straight to the end.
function animateProgress(duration, frame) {
  if (REDUCED_MOTION.matches) {
    frame(null);
    return;
  }
  const started = performance.now();
  const step = (now) => {
    const progress = Math.min(1, (now - started) / duration);
    frame(progress < 1 ? easeOutCubic(progress) : null);
    if (progress < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function revealChart() {
  animateProgress(800, (progress) => {
    state.chartReveal = progress;
    renderChart();
  });
}

function handleChartHover(event) {
  const geometry = state.geometry;
  if (!geometry || !state.chart.length) return;
  const rect = $("#history-chart").getBoundingClientRect();
  const px = event.clientX - rect.left;
  let nearest = 0;
  let distance = Infinity;
  for (let index = 0; index < geometry.times.length; index += 1) {
    const delta = Math.abs(geometry.x(geometry.times[index]) - px);
    if (delta < distance) { distance = delta; nearest = index; }
  }
  if (nearest !== state.hover) {
    state.hover = nearest;
    requestAnimationFrame(renderChart);
  }
}

function clearChartHover() {
  if (state.hover === -1) return;
  state.hover = -1;
  requestAnimationFrame(renderChart);
}

/* -------------------------------------------------------------- annual map */

function parseHexColor(value) {
  const source = value.trim().replace("#", "");
  if (source.length !== 6) return { red: 128, green: 128, blue: 128 };
  return {
    red: Number.parseInt(source.slice(0, 2), 16),
    green: Number.parseInt(source.slice(2, 4), 16),
    blue: Number.parseInt(source.slice(4, 6), 16),
  };
}

function mixColor(left, right, amount) {
  const blend = (start, end) => Math.round(start + (end - start) * amount);
  return `rgb(${blend(left.red, right.red)}, ${blend(left.green, right.green)}, ${blend(left.blue, right.blue)})`;
}

function annualCellColor(value, annual) {
  const bright = parseHexColor(cssVar("--year-map-bright"));
  const middle = parseHexColor(cssVar("--year-map-mid"));
  const dark = parseHexColor(cssVar("--year-map-dark"));
  const fraction = Math.max(0, Math.min(1,
    (Number(value) - annual.scale_min_mpsas)
      / (annual.scale_max_mpsas - annual.scale_min_mpsas)));
  return fraction <= 0.5
    ? mixColor(bright, middle, fraction * 2)
    : mixColor(middle, dark, (fraction - 0.5) * 2);
}

// Twilight fades in linearly from transparent to opaque across the fade range.
function annualCellOpacity(value, annual) {
  return Math.max(0, Math.min(1,
    (Number(value) - annual.fade_min_mpsas) / (annual.fade_max_mpsas - annual.fade_min_mpsas)));
}

function annualDate(year, day) {
  return new Date(Date.UTC(year, 0, day + 1, 12));
}

function annualTime(totalMinutes) {
  const minutes = ((totalMinutes % 1440) + 1440) % 1440;
  const hour = Math.floor(minutes / 60);
  return `${String(hour).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
}

async function loadAnnual(year = null, silent = false) {
  const selector = $("#annual-year");
  selector.disabled = true;
  $("#annual-caption").textContent = "Loading…";
  $("#annual-empty").textContent = "No nighttime readings for this year.";
  try {
    const suffix = year === null ? "" : `?year=${year}`;
    const annual = await api(`/api/annual${suffix}`);
    state.annual = annual;
    state.annualHover = null;

    const years = [...new Set([annual.year, ...(annual.available_years || [])])]
      .sort((left, right) => right - left);
    selector.replaceChildren(...years.map((optionYear) => {
      const option = document.createElement("option");
      option.value = String(optionYear);
      option.textContent = String(optionYear);
      option.selected = optionYear === annual.year;
      return option;
    }));
    $("#annual-caption").textContent =
      `${formatInteger(annual.observed_nights)} of ${annual.days} nights recorded, ${annual.timezone} time`;
    $("#annual-foot").textContent =
      `Each column is one night from ${annualTime(annual.night_start_hour * 60)} to ${annualTime(annual.night_end_hour * 60)}, shaded by 30-minute averages. Gaps are times without a reading.${
        annual.darkness ? " Dotted lines mark astronomical dusk and dawn." : ""}`;
    $("#moonlit-toggle").hidden = !annual.darkness;
    $("#annual-map").setAttribute(
      "aria-label",
      `${annual.year} sky brightness map with ${annual.observed_nights} observing nights`,
    );
    if (silent) renderAnnualMap(); else revealAnnualMap();
  } catch (error) {
    $("#annual-caption").textContent = "Unavailable";
    if (error.status === 401) return showLogin();
    if (!silent) toast(error.message, true);
  } finally {
    selector.disabled = false;
  }
}

function renderAnnualMap() {
  const canvas = $("#annual-map");
  if (!canvas) return;
  const annual = state.annual;
  const cells = annual?.cells || [];
  $("#annual-empty").hidden = cells.length > 0;

  const rect = canvas.getBoundingClientRect();
  if (rect.width < 2 || rect.height < 2) return;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * ratio);
  canvas.height = Math.round(rect.height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, rect.width, rect.height);
  if (!annual) {
    state.annualGeometry = null;
    return;
  }

  const pad = {
    top: 6,
    right: 4,
    bottom: 26,
    left: rect.width < 500 ? 38 : 44,
  };
  const plotWidth = rect.width - pad.left - pad.right;
  const plotHeight = rect.height - pad.top - pad.bottom;
  const dayWidth = plotWidth / annual.days;
  const slotHeight = plotHeight / annual.slots;
  const cellMap = new Map(
    cells.map((cell) => [`${cell.day}:${cell.slot}`, cell]),
  );
  state.annualGeometry = {
    pad,
    plotWidth,
    plotHeight,
    dayWidth,
    slotHeight,
    cellMap,
    width: rect.width,
    height: rect.height,
  };

  ctx.fillStyle = cssVar("--year-map-empty");
  ctx.fillRect(pad.left, pad.top, plotWidth, plotHeight);

  // While developing, nights fill in from January onward; a faint glowing
  // edge marks the front, like an exposure being developed.
  const revealDay = state.annualReveal === null ? Infinity : state.annualReveal * annual.days;
  cells.forEach((cell) => {
    if (cell.day > revealDay) return;
    // Multiply with the twilight fade rather than replacing it.
    ctx.globalAlpha = annualCellOpacity(cell.mpsas, annual) * (cell.moon && state.hideMoonlit ? 0.18 : 1);
    ctx.fillStyle = annualCellColor(cell.mpsas, annual);
    ctx.fillRect(
      pad.left + cell.day * dayWidth,
      pad.top + cell.slot * slotHeight,
      Math.max(1, dayWidth + 0.2),
      Math.max(1, slotHeight + 0.2),
    );
  });
  ctx.globalAlpha = 1;

  if (revealDay !== Infinity) {
    const edge = pad.left + revealDay * dayWidth;
    const glow = ctx.createLinearGradient(edge - 36, 0, edge, 0);
    glow.addColorStop(0, "rgba(255, 220, 160, 0)");
    glow.addColorStop(1, cssVar("--accent-glow"));
    ctx.fillStyle = glow;
    ctx.fillRect(Math.max(pad.left, edge - 36), pad.top, Math.min(36, edge - pad.left), plotHeight);
  }

  // Dotted lines trace astronomical dusk and dawn through the year.
  if (annual.darkness && revealDay === Infinity) {
    const toY = (minutes) => pad.top + (minutes / annual.slot_minutes) * slotHeight;
    ctx.strokeStyle = cssVar("--chart-mark");
    ctx.globalAlpha = 0.55;
    ctx.lineWidth = 1;
    ctx.setLineDash([2, 3]);
    ["dusk", "dawn"].forEach((edge) => {
      ctx.beginPath();
      let drawing = false;
      annual.darkness.forEach((night, day) => {
        if (!night) { drawing = false; return; }
        const x = pad.left + (day + 0.5) * dayWidth;
        const y = toY(night[edge]);
        if (drawing) ctx.lineTo(x, y); else ctx.moveTo(x, y);
        drawing = true;
      });
      ctx.stroke();
    });
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
  }

  const grid = cssVar("--chart-grid");
  const label = cssVar("--chart-label");
  ctx.font = canvasFont(12);
  ctx.strokeStyle = grid;
  ctx.fillStyle = label;
  ctx.lineWidth = 1;

  // Label every three hours from 18:00, placed by the server's night window.
  const slotsPerHour = 60 / annual.slot_minutes;
  const timeMarks = [18, 21, 0, 3, 6].map((hour) => [
    ((hour - annual.night_start_hour + 24) % 24) * slotsPerHour,
    `${String(hour).padStart(2, "0")}:00`,
  ]).filter(([slot]) => slot <= annual.slots);
  timeMarks.forEach(([slot, text]) => {
    const y = Math.round(pad.top + slot * slotHeight) + 0.5;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(pad.left + plotWidth, y);
    ctx.stroke();
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.fillText(text, pad.left - 7, y);
  });

  const yearStart = Date.UTC(annual.year, 0, 1);
  const monthStarts = Array.from({ length: 13 }, (_, month) =>
    Math.round((Date.UTC(annual.year, month, 1) - yearStart) / 86400000));
  for (let month = 0; month < 12; month += 1) {
    const startDay = monthStarts[month];
    const endDay = monthStarts[month + 1];
    const x = Math.round(pad.left + startDay * dayWidth) + 0.5;
    ctx.beginPath();
    ctx.moveTo(x, pad.top);
    ctx.lineTo(x, pad.top + plotHeight);
    ctx.stroke();
    ctx.textAlign = "center";
    ctx.textBaseline = "alphabetic";
    ctx.fillText(
      new Intl.DateTimeFormat(undefined, { month: "short", timeZone: "UTC" })
        .format(new Date(Date.UTC(annual.year, month, 1))),
      pad.left + ((startDay + endDay) / 2) * dayWidth,
      rect.height - 7,
    );
  }
  ctx.beginPath();
  const endX = Math.round(pad.left + plotWidth) + 0.5;
  ctx.moveTo(endX, pad.top);
  ctx.lineTo(endX, pad.top + plotHeight);
  ctx.stroke();

  const tip = $("#annual-tip");
  if (state.annualHover) {
    const { day, slot } = state.annualHover;
    const cell = cellMap.get(`${day}:${slot}`);
    const x = pad.left + day * dayWidth;
    const y = pad.top + slot * slotHeight;
    ctx.strokeStyle = cssVar("--chart-mark");
    ctx.lineWidth = 1.3;
    ctx.strokeRect(x - 0.5, y - 0.5, Math.max(2, dayWidth + 1), slotHeight + 1);

    tip.replaceChildren();
    const value = document.createElement("strong");
    value.textContent = cell
      ? `${formatNumber(cell.mpsas)} mag/arcsec²`
      : "No reading";
    const startMinutes = annual.night_start_hour * 60 + slot * annual.slot_minutes;
    const date = document.createElement("span");
    date.textContent = `Night of ${new Intl.DateTimeFormat(undefined, {
      weekday: "short", month: "short", day: "numeric", timeZone: "UTC",
    }).format(annualDate(annual.year, day))}`;
    const time = document.createElement("span");
    time.textContent = `${annualTime(startMinutes)}–${annualTime(startMinutes + annual.slot_minutes)}${
      cell ? `, ${formatInteger(cell.count)} readings` : ""}`;
    tip.append(value, date, time);
    if (cell?.moon) {
      const moon = document.createElement("span");
      moon.textContent = `Moon up, ${Math.round(cell.moon_fraction * 100)}% lit`;
      tip.append(moon);
    }
    tip.hidden = false;
    tip.style.left = `${Math.max(80, Math.min(rect.width - 80, x + dayWidth / 2))}px`;
    tip.style.top = `${Math.max(42, y)}px`;
  } else {
    tip.hidden = true;
  }
}

function revealAnnualMap() {
  animateProgress(1400, (progress) => {
    state.annualReveal = progress;
    renderAnnualMap();
  });
}

function handleAnnualHover(event) {
  const geometry = state.annualGeometry;
  const annual = state.annual;
  if (!geometry || !annual) return;
  const rect = $("#annual-map").getBoundingClientRect();
  const x = event.clientX - rect.left - geometry.pad.left;
  const y = event.clientY - rect.top - geometry.pad.top;
  if (x < 0 || y < 0 || x >= geometry.plotWidth || y >= geometry.plotHeight) {
    return clearAnnualHover();
  }
  const next = {
    day: Math.min(annual.days - 1, Math.floor(x / geometry.dayWidth)),
    slot: Math.min(annual.slots - 1, Math.floor(y / geometry.slotHeight)),
  };
  if (
    !state.annualHover
    || next.day !== state.annualHover.day
    || next.slot !== state.annualHover.slot
  ) {
    state.annualHover = next;
    requestAnimationFrame(renderAnnualMap);
  }
}

function clearAnnualHover() {
  if (!state.annualHover) return;
  state.annualHover = null;
  requestAnimationFrame(renderAnnualMap);
}

/* --------------------------------------------------------------- range dock */

function positionThumbs() {
  $$(".seg").forEach((group) => {
    const active = group.querySelector(".seg-btn.is-active");
    const thumb = group.querySelector(".seg-thumb");
    if (!active || !thumb || !active.offsetWidth) return;
    thumb.style.width = `${active.offsetWidth}px`;
    thumb.style.transform = `translateX(${active.offsetLeft}px)`;
  });
}

function selectRange(button) {
  $$(".seg-btn[data-hours]").forEach((item) => item.classList.toggle("is-active", item === button));
  state.hours = Number(button.dataset.hours);
  $("#range-title").textContent = button.dataset.label;
  positionThumbs();
  loadDashboard({ reveal: true });
}

/* -------------------------------------------------------------------- meter */

function syncTransportUI(resetResults = false) {
  const serial = $("#device-transport").value === "serial";
  $("#ethernet-fields").hidden = serial;
  $("#serial-fields").hidden = !serial;
  $("#network-discovery-panel").hidden = serial;
  $("#serial-discovery-panel").hidden = !serial;
  $("#discovery-heading").textContent = serial ? "Find mapped USB meters" : "Find SQM‑LE meters";
  $("#discovery-copy").textContent = serial
    ? "Tests serial devices exposed to the container and accepts only meters that answer Unihedron’s identity command."
    : "Uses Unihedron’s UDP broadcast. Add your LAN subnet as a fallback when Docker or a VLAN swallows the replies.";
  if (resetResults) {
    $("#discovery-title").textContent = "No scan run yet";
    const target = $("#discovery-results");
    target.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = serial
      ? "Map the USB meter into the container, then scan for it here."
      : "Run discovery with the meter powered on and attached to the same LAN.";
    target.append(empty);
  }
}

async function loadConfig() {
  try {
    const data = await api("/api/config");
    $("#device-transport").value = data.transport || "ethernet";
    $("#device-host").value = data.host || "";
    $("#device-port").value = data.port || 10001;
    $("#serial-device").value = data.serial_device || "";
    $("#serial-baud").value = data.baud_rate || 115200;
    syncTransportUI();
    $("#directory-import-wrap").hidden = !data.import_directory_available;
    $("#import-directory-label").textContent = data.import_directory;
  } catch (error) {
    if (error.status === 401) showLogin();
  }
}

async function submitDevice(testOnly) {
  const message = $("#device-message");
  const transport = $("#device-transport").value;
  const body = {
    transport,
    host: $("#device-host").value.trim(),
    port: Number($("#device-port").value),
    serial_device: $("#serial-device").value.trim(),
    baud_rate: Number($("#serial-baud").value),
  };
  setMessage(message, testOnly ? "Testing connection…" : "Saving meter…");
  try {
    const result = await api(testOnly ? "/api/device/test" : "/api/device", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (testOnly) {
      setMessage(message, result.device.interval_reporting
        ? `Reached SQM serial ${result.device.serial}, but it isn’t answering commands. See Meter details below.`
        : `Connected: SQM model ${result.device.model}, serial ${result.device.serial}.`, "success");
    } else {
      const endpoint = result.transport === "serial"
        ? `${result.serial_device} at ${result.baud_rate} baud`
        : `${result.host}:${result.port}`;
      setMessage(message, `Collecting from ${endpoint}.`, "success");
      toast("Meter configuration saved");
      setTimeout(() => navigate("dashboard"), 700);
    }
  } catch (error) {
    setMessage(message, error.message, "error");
  }
}

function renderDevices(devices) {
  const target = $("#discovery-results");
  target.replaceChildren();
  if (!devices.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = $("#device-transport").value === "serial"
      ? "No mapped serial device answered as an SQM. Check the Compose device mapping and permissions."
      : "No meters replied. Add your LAN subnet above, or enter the meter’s address by hand.";
    target.append(empty);
    return;
  }
  devices.forEach((device) => {
    const serial = device.transport === "serial";
    const row = document.createElement("div");
    row.className = "result";

    const address = document.createElement("div");
    const host = document.createElement("strong");
    host.textContent = serial ? device.path : device.host;
    const port = document.createElement("span");
    port.textContent = serial ? `${device.baud_rate} baud` : `TCP ${device.port}`;
    address.append(host, port);

    const identity = document.createElement("div");
    const identityTitle = document.createElement("strong");
    identityTitle.textContent = device.device ? `SQM serial ${device.device.serial}` : "Lantronix device";
    const identityMeta = document.createElement("small");
    identityMeta.textContent = !device.device
      ? "Could not verify with ix"
      : device.device.interval_reporting
        ? "Not answering commands, model unknown"
        : `Model ${device.device.model}, protocol ${device.device.protocol}`;
    identity.append(identityTitle, identityMeta);

    const source = document.createElement("div");
    const sourceTitle = document.createElement("strong");
    sourceTitle.className = device.verified ? "verified" : "";
    sourceTitle.textContent = device.verified ? "Verified SQM" : "Discovered";
    const sourceMeta = document.createElement("small");
    sourceMeta.textContent = device.mac || (serial ? "Mapped serial device" : device.source);
    source.append(sourceTitle, sourceMeta);

    const select = document.createElement("button");
    select.className = "btn ghost sm";
    select.textContent = "Use this meter";
    select.addEventListener("click", () => {
      $("#device-transport").value = device.transport || "ethernet";
      syncTransportUI();
      if (serial) {
        $("#serial-device").value = device.path;
        $("#serial-baud").value = device.baud_rate;
      } else {
        $("#device-host").value = device.host;
        $("#device-port").value = device.port;
      }
      $(".console-scroll").scrollTo({ top: 0, behavior: "smooth" });
      toast(`${serial ? device.path : device.host} selected — save to begin collecting`);
    });

    row.append(address, identity, source, select);
    target.append(row);
  });
}

async function discoverMeters(event) {
  event.preventDefault();
  const spinner = $("#discovery-spinner");
  spinner.hidden = false;
  $("#discovery-title").textContent = "Scanning the local network…";
  try {
    const subnet = $("#scan-subnet").value.trim();
    const data = await api("/api/discover", {
      method: "POST",
      body: JSON.stringify({
        broadcast_address: $("#broadcast-address").value.trim() || "255.255.255.255",
        subnet: subnet || null,
      }),
    });
    $("#discovery-title").textContent = `${data.count} device${data.count === 1 ? "" : "s"} found`;
    renderDevices(data.devices);
  } catch (error) {
    $("#discovery-title").textContent = "Discovery failed";
    renderDevices([]);
    toast(error.message, true);
  } finally {
    spinner.hidden = true;
  }
}

async function discoverSerialMeters(event) {
  event.preventDefault();
  const spinner = $("#discovery-spinner");
  spinner.hidden = false;
  $("#discovery-title").textContent = "Scanning mapped serial devices…";
  try {
    const data = await api("/api/discover/serial", {
      method: "POST",
      body: JSON.stringify({}),
    });
    $("#discovery-title").textContent = `${data.count} USB meter${data.count === 1 ? "" : "s"} found`;
    renderDevices(data.devices);
  } catch (error) {
    $("#discovery-title").textContent = "USB discovery failed";
    renderDevices([]);
    toast(error.message, true);
  } finally {
    spinner.hidden = true;
  }
}

/* ------------------------------------------------------------------ imports */

function selectFiles(files) {
  state.files = [...files].filter((file) => /\.(dat|csv)$/i.test(file.name));
  $("#selected-files").textContent = state.files.length
    ? `${state.files.length} file${state.files.length === 1 ? "" : "s"} ready`
    : "No supported files selected";
  $("#upload-button").disabled = !state.files.length;
}

async function runImport(kind) {
  const progress = $("#import-progress");
  const message = $("#import-message");
  progress.hidden = false;
  setMessage(message, kind === "upload" ? "Importing selected files…" : "Importing mounted directory…");
  try {
    let result;
    if (kind === "upload") {
      const form = new FormData();
      state.files.forEach((file) => form.append("files", file));
      result = await api("/api/import/upload", { method: "POST", body: form });
    } else {
      result = await api("/api/import/directory", { method: "POST" });
    }
    const summary = `Added ${formatInteger(result.imported)} readings from ${result.files} files; ${formatInteger(result.duplicates)} duplicates skipped`;
    setMessage(message, `${summary}${result.bad_lines ? `; ${result.bad_lines} bad rows` : ""}.`, result.bad_lines ? "" : "success");
    toast(summary);
    state.files = [];
    $("#log-files").value = "";
    selectFiles([]);
    await loadImportHistory();
  } catch (error) {
    setMessage(message, error.message, "error");
  } finally {
    progress.hidden = true;
  }
}

async function loadImportHistory() {
  try {
    const data = await api("/api/imports");
    const body = $("#import-history");
    body.replaceChildren();
    if (!data.imports.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 5;
      cell.className = "muted";
      cell.textContent = "No imports yet.";
      row.append(cell);
      body.append(row);
      return;
    }
    data.imports.forEach((item) => {
      const row = document.createElement("tr");
      [item.filename, formatInteger(item.imported), formatInteger(item.duplicates), formatInteger(item.bad_lines), formatDate(item.created_iso)]
        .forEach((value) => {
          const cell = document.createElement("td");
          cell.textContent = value;
          row.append(cell);
        });
      body.append(row);
    });
  } catch (error) {
    if (error.status === 401) showLogin();
  }
}

/* ------------------------------------------------------------ meter control */

const FRESHNESS_LABELS = {
  frequency: "Fresh (frequency mode)",
  period: "Fresh (period mode)",
  stale: "Stale: too dark for a new reading yet",
};

function fact(label, value) {
  const item = document.createElement("div");
  const name = document.createElement("span");
  name.textContent = label;
  const content = document.createElement("strong");
  content.textContent = value ?? "—";
  content.title = value ?? "";
  item.append(name, content);
  return item;
}

function describeInterval(period, threshold) {
  if (!period) return "off";
  const darker = threshold > 0 ? `, only readings darker than ${formatNumber(threshold)}` : "";
  return `every ${period} s${darker}`;
}

function renderInterval(interval) {
  const form = $("#interval-form");
  if (!interval) {
    $("#interval-title").textContent = "Unknown";
    $("#interval-summary").textContent = "The meter did not report its interval settings.";
    form.hidden = true;
    return;
  }
  $("#interval-title").textContent = interval.enabled
    ? `On, every ${interval.ram_period_s} s`
    : "Off";
  $("#interval-title").classList.toggle("on", interval.enabled);
  const now = describeInterval(interval.ram_period_s, interval.ram_threshold_mpsas);
  const boot = describeInterval(interval.eeprom_period_s, interval.eeprom_threshold_mpsas);
  $("#interval-summary").textContent = now === boot
    ? `Now and after a restart: ${now}.`
    : `Now: ${now}. After a restart: ${boot}.`;
  $("#interval-period").value = interval.ram_period_s;
  $("#interval-threshold").value = "";
  $("#interval-off").disabled = !interval.enabled && !interval.enabled_at_boot;
  form.hidden = false;
}

function renderMeter(details) {
  const health = details.health;
  const banner = $("#meter-health");
  banner.hidden = health.status === "ok";
  banner.classList.toggle("bad", health.status !== "ok");
  $("#meter-health-text").textContent = health.message;

  const info = details.info;
  $("#meter-title").textContent = info?.serial ? `Meter ${info.serial}` : "Meter unavailable";
  const facts = [];
  if (info) {
    facts.push(
      fact("Model", info.model ?? "Unknown"),
      fact("Firmware feature", info.feature ?? "Unknown"),
      fact("Protocol", info.protocol ?? "Unknown"),
    );
  }
  if (details.lock) {
    const rules = details.lock.rules;
    const note = details.lock.locked && rules && !rules.report_interval
      ? ", interval changes allowed"
      : "";
    facts.push(fact("Lock switch", `${details.lock.locked ? "Locked" : "Unlocked"}${note}`));
  }
  if (details.freshness) facts.push(fact("Latest reading", FRESHNESS_LABELS[details.freshness]));
  const calibration = details.calibration;
  if (calibration) {
    facts.push(
      fact("Light calibration", `${formatNumber(calibration.light_offset_mpsas)} mag/arcsec² at ${formatNumber(calibration.light_temperature_c, 1)} °C`),
      fact("Dark calibration", `${formatNumber(calibration.dark_period_s, 3)} s at ${formatNumber(calibration.dark_temperature_c, 1)} °C`),
    );
  }
  $("#meter-facts").replaceChildren(...facts);

  const errors = Object.entries(details.errors || {});
  setMessage(
    $("#meter-message"),
    errors.length ? `Some details were unavailable: ${errors.map(([key, text]) => `${key} (${text})`).join("; ")}` : "",
    errors.length ? "error" : "",
  );
  renderInterval(health.status === "ok" ? details.interval : null);
  renderLogger(details.logger);
}

function renderLogger(logger) {
  $("#logger-card").hidden = !logger;
  if (logger) $("#logger-summary").textContent = `${formatInteger(logger.records)} records stored in the meter.`;
}

async function pollLogger() {
  try {
    const status = await api("/api/meter/logger");
    const bar = $("#logger-progress");
    bar.hidden = !status.running && !status.read;
    bar.firstElementChild.style.width = `${status.total ? (status.read / status.total) * 100 : 0}%`;
    $("#logger-download").disabled = status.running;
    if (status.running) {
      setMessage($("#logger-message"), `Reading ${formatInteger(status.read)} of ${formatInteger(status.total)} records…`);
      setTimeout(pollLogger, 1000);
    } else if (status.error) {
      const kept = status.imported ? ` ${formatInteger(status.imported)} readings were saved before it stopped.` : "";
      setMessage($("#logger-message"), `The download stopped: ${status.error}.${kept}`, "error");
    } else if (status.total) {
      const skipped = status.skipped ? ` ${formatInteger(status.skipped)} unreadable records were skipped.` : "";
      setMessage($("#logger-message"), `Added ${formatInteger(status.imported)} readings; ${formatInteger(status.duplicates)} were already stored.${skipped}`, "success");
    }
  } catch (error) {
    if (error.status === 401) showLogin();
  }
}

async function loadMeter() {
  const button = $("#meter-refresh");
  button.disabled = true;
  setMessage($("#meter-message"), "Reading the meter…");
  try {
    renderMeter(await api("/api/meter"));
  } catch (error) {
    if (error.status === 401) return showLogin();
    $("#meter-title").textContent = error.status === 409 ? "No meter configured" : "Meter unavailable";
    $("#meter-facts").replaceChildren();
    $("#meter-health").hidden = true;
    setMessage($("#meter-message"), error.status === 409 ? "Save a meter connection first." : error.message, error.status === 409 ? "" : "error");
    renderInterval(null);
    renderLogger(null);
  } finally {
    button.disabled = false;
  }
}

async function changeInterval(periodSeconds) {
  const message = $("#interval-message");
  const threshold = $("#interval-threshold").value.trim();
  const body = {
    period_s: periodSeconds,
    threshold_mpsas: periodSeconds && threshold !== "" ? Number(threshold) : null,
    permanent: $("#interval-permanent").checked,
  };
  setMessage(message, "Sending to the meter…");
  try {
    const interval = await api("/api/meter/interval", {
      method: "POST",
      body: JSON.stringify(body),
    });
    renderInterval(interval);
    setMessage(message, "The meter confirmed the change.", "success");
    toast(interval.enabled ? "Interval reporting updated." : "Interval reporting turned off.");
  } catch (error) {
    if (error.status === 401) return showLogin();
    setMessage(message, error.message, "error");
  }
}

/* ----------------------------------------------------------------- settings */

const MQTT_STATUS = { off: "Off", connecting: "Connecting", connected: "Connected" };

function fillSettings(settings) {
  $$(".settings-form").forEach((form) => {
    const section = settings[form.dataset.section] || {};
    Object.entries(section).forEach(([name, field]) => {
      const input = form.elements[name];
      if (!input) return;
      if (input.type === "checkbox") input.checked = Boolean(field.value);
      else if (input.type === "password") {
        input.value = "";
        input.placeholder = field.set ? "Saved; leave blank to keep" : "";
      } else input.value = field.value ?? "";
      input.disabled = field.locked;
      input.title = field.locked ? "Set by the environment" : "";
    });
  });
}

function showMqttStatus(status) {
  const tag = $("#mqtt-status");
  tag.textContent = MQTT_STATUS[status] || "Error";
  tag.title = status.startsWith("error") ? status.slice(7) : "";
  tag.classList.toggle("on", status === "connected");
}

async function loadSettings() {
  try {
    const data = await api("/api/settings");
    fillSettings(data.settings);
    showMqttStatus(data.status.mqtt);
  } catch (error) {
    if (error.status === 401) showLogin();
  }
}

async function saveSettings(form) {
  const message = form.querySelector(".form-message");
  const body = {};
  [...form.elements].forEach((input) => {
    if (!input.name || input.disabled) return;
    if (input.type === "checkbox") body[input.name] = input.checked;
    else if (input.type === "password") { if (input.value !== "") body[input.name] = input.value; }
    else body[input.name] = input.value;
  });
  setMessage(message, "Saving…");
  try {
    await api(`/api/settings/${form.dataset.section}`, { method: "PUT", body: JSON.stringify(body) });
    setMessage(message, "Saved.", "success");
    loadSettings();
  } catch (error) {
    if (error.status === 401) return showLogin();
    setMessage(message, error.detail?.message || error.message, "error");
  }
}

/* -------------------------------------------------------------------- wiring */

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = $("#login-error");
  error.textContent = "";
  try {
    const auth = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: $("#login-username").value,
        password: $("#login-password").value,
      }),
    });
    $("#login-password").value = "";
    showApp(auth);
  } catch (problem) {
    error.textContent = problem.message;
  }
});

$("#logout-button").addEventListener("click", async () => {
  try { await api("/api/auth/logout", { method: "POST" }); } catch {}
  state.csrf = null;
  showLogin();
});

$$(".rail-btn[data-page]").forEach((button) => {
  button.addEventListener("click", () => navigate(button.dataset.page));
});
$$("[data-go]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.go)));
$$(".seg-btn[data-hours]").forEach((button) => button.addEventListener("click", () => selectRange(button)));
$$(".seg-btn[data-meteors]").forEach((button) => button.addEventListener("click", () => setMeteorSetting(button.dataset.meteors)));

$("#theme-button").addEventListener("click", (event) => {
  const rect = event.currentTarget.getBoundingClientRect();
  revealTheme(
    currentTheme() === "dark" ? "light" : "dark",
    rect.left + rect.width / 2,
    rect.top + rect.height / 2,
  );
});

$("#refresh-button").addEventListener("click", (event) => {
  const button = event.currentTarget;
  button.classList.remove("spinning");
  void button.offsetWidth;
  button.classList.add("spinning");
  loadDashboard({ reveal: true });
  loadAnnual(state.annual?.year ?? null, true);
});

const chartWrap = document.querySelector(".chart-wrap");
chartWrap.addEventListener("mousemove", handleChartHover);
chartWrap.addEventListener("mouseleave", clearChartHover);
const annualWrap = document.querySelector(".year-map-wrap");
annualWrap.addEventListener("mousemove", handleAnnualHover);
annualWrap.addEventListener("mouseleave", clearAnnualHover);
$("#annual-year").addEventListener("change", (event) => {
  loadAnnual(Number(event.currentTarget.value));
});

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    positionThumbs();
    renderChart();
    renderAnnualMap();
  }, 90);
});

$("#device-form").addEventListener("submit", (event) => { event.preventDefault(); submitDevice(false); });
$("#test-device").addEventListener("click", () => submitDevice(true));
$("#device-transport").addEventListener("change", () => syncTransportUI(true));
$("#discovery-form").addEventListener("submit", discoverMeters);
$$(".settings-form").forEach((form) => form.addEventListener("submit", (event) => {
  event.preventDefault();
  saveSettings(form);
}));
$("[data-test-alert]").addEventListener("click", async (event) => {
  const message = event.currentTarget.closest("form").querySelector(".form-message");
  setMessage(message, "Sending a test alert…");
  try {
    const result = await api("/api/settings/alerts/test", { method: "POST" });
    setMessage(message, result.ok ? `Test alert sent (${result.detail}).` : `The alert URL answered: ${result.detail}`, result.ok ? "success" : "error");
  } catch (error) {
    setMessage(message, error.message, "error");
  }
});
$("#meter-refresh").addEventListener("click", loadMeter);
function syncExportLink() {
  const range = $("#export-range").value;
  const params = new URLSearchParams({ format: $("#export-format").value });
  $("#export-dates").hidden = range !== "custom";
  if (range === "custom") {
    // Whole local days, from the start of From to the end of To.
    const from = $("#export-from").valueAsDate;
    const to = $("#export-to").valueAsDate;
    const startOfDay = (date) => new Date(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate());
    if (from) params.set("since", String(Math.floor(startOfDay(from).getTime() / 1000)));
    if (to) params.set("until", String(Math.floor(startOfDay(to).getTime() / 1000) + 86399));
  } else if (range !== "all") {
    params.set("since", String(Math.floor(Date.now() / 1000) - Number(range) * 3600));
  }
  $("#export-link").href = `/api/export?${params}`;
}
["#export-range", "#export-format", "#export-from", "#export-to"].forEach((selector) =>
  $(selector).addEventListener("change", syncExportLink));
$("#export-link").addEventListener("click", syncExportLink);
$("#moonlit-toggle input").addEventListener("change", (event) => {
  state.hideMoonlit = event.currentTarget.checked;
  renderAnnualMap();
});
$("#logger-download").addEventListener("click", async () => {
  try {
    await api("/api/meter/logger/download", { method: "POST" });
    pollLogger();
  } catch (error) {
    setMessage($("#logger-message"), error.message, "error");
  }
});
$("#interval-off").addEventListener("click", () => changeInterval(0));
$("#interval-form").addEventListener("submit", (event) => {
  event.preventDefault();
  changeInterval(Number($("#interval-period").value));
});
$("#serial-discovery-form").addEventListener("submit", discoverSerialMeters);

const dropZone = $("#drop-zone");
const fileInput = $("#log-files");
dropZone.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => selectFiles(fileInput.files));
["dragenter", "dragover"].forEach((name) => dropZone.addEventListener(name, (event) => {
  event.preventDefault();
  dropZone.classList.add("dragging");
}));
["dragleave", "drop"].forEach((name) => dropZone.addEventListener(name, (event) => {
  event.preventDefault();
  dropZone.classList.remove("dragging");
}));
dropZone.addEventListener("drop", (event) => selectFiles(event.dataTransfer.files));
$("#upload-button").addEventListener("click", () => runImport("upload"));
$("#directory-import-button").addEventListener("click", () => runImport("directory"));

$("#password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = $("#password-message");
  const next = $("#new-password").value;
  if (next !== $("#confirm-password").value) {
    return setMessage(message, "New passwords do not match.", "error");
  }
  try {
    await api("/api/account/password", {
      method: "POST",
      body: JSON.stringify({
        current_password: $("#current-password").value,
        new_password: next,
      }),
    });
    state.csrf = null;
    showLogin();
    toast("Password changed. Sign in again.");
  } catch (error) {
    setMessage(message, error.message, "error");
  }
});

async function initialLoad() {
  syncThemeUI();
  startSky();
  try {
    const auth = await api("/api/auth/status");
    if (auth.authenticated) showApp(auth); else showLogin();
  } catch {
    showLogin();
  }
}

initialLoad();
