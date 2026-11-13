// Marks the page as scripted so CSS can hide the no-JavaScript submit button.
document.documentElement.classList.add("js");

// Links to other websites (a customer's news, OpenRouter's model list) open in a new tab, so
// Wardmind stays where it was. In the desktop app, a new tab means the computer's own browser.
document.addEventListener("click", (event) => {
  const link = event.target instanceof Element ? event.target.closest("a[href]") : null;
  if (!(link instanceof HTMLAnchorElement)) return;
  if (link.origin === window.location.origin || !/^https?:$/.test(link.protocol)) return;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
});

// Every print button opens the browser's print dialog, where "Save as PDF" is one of the choices.
document.querySelectorAll("[data-print]").forEach((button) => {
  button.addEventListener("click", () => window.print());
});

// Copy as text on the onboarding brief. The plain text sits in a <template> the server renders, so
// the copy matches the page. The result goes in the status line beside the buttons.
// Order of attempts: the async clipboard API; then the older copy-from-selection command, which
// needs no permission; and if both are refused, the text is shown selected for Ctrl+C.
const COPY_DONE = "Copied.";
const COPY_MANUAL = "Your browser blocked copying. The text is selected below: press Ctrl+C to copy it.";

function copyBySelection(field) {
  field.hidden = false;
  field.focus();
  field.select();
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  }
}

document.querySelectorAll("[data-copy-target]").forEach((button) => {
  button.addEventListener("click", async () => {
    const source = document.querySelector(button.dataset.copyTarget);
    const status = button.closest(".brief-actions")?.querySelector('[role="status"]');
    const field = document.getElementById(`${source?.id}-fallback`);
    const text = source instanceof HTMLTemplateElement ? source.content.textContent.trim() : "";
    const say = (message) => { if (status) status.textContent = message; };
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      say(COPY_DONE);
      return;
    } catch {
      // Fall through to the selection-based copy.
    }
    if (!field) return say(COPY_MANUAL);
    field.value = text;
    if (copyBySelection(field)) {
      field.hidden = true;
      button.focus();
      say(COPY_DONE);
    } else {
      say(COPY_MANUAL);
    }
  });
});

// Email drafts (the account's "Draft an email" page): each Copy button copies the readonly box its
// data-draft-copy names, and the status line under that box says what happened. The box can be
// selected by hand without JavaScript, and the buttons are shown only when JavaScript runs.
document.querySelectorAll("[data-draft-copy]").forEach((button) => {
  button.addEventListener("click", async () => {
    const field = document.getElementById(button.dataset.draftCopy);
    const status = button.closest(".draft-field")?.querySelector("[data-copy-status]");
    if (!(field instanceof HTMLTextAreaElement)) return;
    const say = (message) => { if (status) status.textContent = message; };
    try {
      await navigator.clipboard.writeText(field.value);
      say("Copied.");
      return;
    } catch {
      // Fall through to selecting the text for Ctrl+C.
    }
    field.focus();
    field.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch {
      copied = false;
    }
    say(copied ? "Copied." : "Your browser blocked copying. The text is selected: press Ctrl+C to copy it.");
  });
});

// Client updates: a client's link is a readonly box. Focusing it selects the whole link, and Copy
// link copies it, saying so in the status line under the box. The box shows the link without
// JavaScript; the Copy button is hidden then.
document.querySelectorAll("[data-select-all]").forEach((field) => {
  field.addEventListener("focus", () => field.select());
  field.addEventListener("click", () => field.select());
});

document.querySelectorAll("[data-copy-link]").forEach((button) => {
  button.addEventListener("click", async () => {
    const field = document.getElementById(button.dataset.copyLink);
    const status = button.closest(".client-link")?.querySelector("[data-copy-status]");
    if (!(field instanceof HTMLInputElement)) return;
    const say = (message) => { if (status) status.textContent = message; };
    try {
      await navigator.clipboard.writeText(field.value);
      say("Copied.");
      return;
    } catch {
      // Fall through to selecting the link for Ctrl+C.
    }
    field.focus();
    field.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch {
      copied = false;
    }
    say(copied ? "Copied." : "Your browser blocked copying. The link is selected: press Ctrl+C to copy it.");
  });
});

// Live preview on Settings > Appearance. Choosing a theme, accent or density shows it on the page
// at once. Nothing is saved until the form is submitted, so leaving the page keeps the saved values.
const APPEARANCE_ATTRIBUTES = new Map([
  ["theme", "data-theme"],
  ["accent", "data-accent"],
  ["density", "data-density"],
]);

document.addEventListener("change", (event) => {
  const input = event.target;
  if (!(input instanceof HTMLInputElement) || input.type !== "radio") return;
  const attribute = APPEARANCE_ATTRIBUTES.get(input.name);
  if (!attribute || !input.form || !input.form.hasAttribute("data-appearance")) return;
  // "Match my computer" is no attribute at all, so the operating system's setting applies again.
  if (input.name === "theme" && input.value === "system") {
    document.documentElement.removeAttribute(attribute);
  } else {
    document.documentElement.setAttribute(attribute, input.value);
  }
});

// Settings > Scoring. Each slider's <output> shows its value and unit. The server writes the first
// text, so the page reads correctly without JavaScript; this keeps the text in step as a slider moves.
function showSliderValue(slider) {
  const output = document.querySelector(`output[for="${slider.id}"]`);
  if (!output) return;
  const value = Number(slider.value);
  const singular = output.dataset.suffixOne;
  const suffix = value === 1 && singular ? singular : (output.dataset.suffix || "");
  output.textContent = `${value}${suffix}`;
}

document.querySelectorAll('input[type="range"]').forEach(showSliderValue);
document.addEventListener("input", (event) => {
  const slider = event.target;
  if (slider instanceof HTMLInputElement && slider.type === "range") showSliderValue(slider);
});

// Controls marked data-autosubmit submit their form when they change: a connection's include
// checkbox, the sheet picker, and the file input on Set up. Each keeps a submit button for
// browsers without JavaScript.
document.addEventListener("change", (event) => {
  const control = event.target;
  if (!(control instanceof HTMLElement) || !control.hasAttribute("data-autosubmit")) return;
  if (control.form) control.form.requestSubmit();
});

// On the import form, choosing a file fills the Name box with the file's name, as long as the
// CSM hasn't typed a name yet.
document.addEventListener("change", (event) => {
  const input = event.target;
  if (!(input instanceof HTMLInputElement) || input.type !== "file") return;
  if (!input.hasAttribute("data-fill-name")) return;
  const name = input.form?.querySelector('input[name="new_name"]');
  const file = input.files?.[0];
  if (!(name instanceof HTMLInputElement) || !file || name.value.trim()) return;
  name.value = file.name.replace(/\.[^.]+$/, "");
});

// Set up: a file dropped on an import row is imported at once. The row shows an outline while a
// file is over it. Anywhere else on the page a dropped file is ignored, so the browser never
// opens it in place of the page.
document.querySelectorAll("[data-dropzone]").forEach((zone) => {
  const input = zone.querySelector('input[type="file"]');
  const over = (event) => {
    event.preventDefault();
    zone.classList.add("is-dragging");
  };
  const leave = () => zone.classList.remove("is-dragging");
  zone.addEventListener("dragenter", over);
  zone.addEventListener("dragover", over);
  zone.addEventListener("dragleave", leave);
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    leave();
    const files = event.dataTransfer?.files;
    if (!(input instanceof HTMLInputElement) || !files || files.length === 0) return;
    input.files = files;
    input.form?.requestSubmit();
  });
});
["dragover", "drop"].forEach((type) => {
  window.addEventListener(type, (event) => event.preventDefault());
});

// Add a signal reading on the edit page. The chosen signal decides which value box shows: Champion
// left takes Yes or No, every other signal takes a number. Each option names its box in a data
// attribute. Without JavaScript both boxes stay on the page, and the server reads the matching one.
function showReadingInput(select) {
  const option = select.selectedOptions[0];
  const wanted = option?.dataset.readingInput;
  if (!wanted || !select.form) return;
  select.form.querySelectorAll("[data-reading-field]").forEach((field) => {
    field.hidden = field.dataset.readingField !== wanted;
  });
  // One of the book's own signals names its unit beside the number box ("calls", "%").
  select.form.querySelectorAll("[data-reading-unit-text]").forEach((unit) => {
    unit.textContent = option.dataset.readingUnit || "";
  });
}

document.querySelectorAll("select[data-reading-kind]").forEach(showReadingInput);

// Data & privacy: show the fields for whichever writes AI summaries, OpenRouter or a model on
// this computer. Without JavaScript both sets show, and the chosen one is used.
function showAIProvider() {
  const chosen = document.querySelector("input[data-ai-provider]:checked");
  if (!chosen) return;
  document.querySelectorAll("[data-ai-provider-part]").forEach((part) => {
    part.hidden = part.dataset.aiProviderPart !== chosen.value;
  });
}
showAIProvider();
document.addEventListener("change", (event) => {
  if (event.target instanceof HTMLInputElement && event.target.hasAttribute("data-ai-provider")) {
    showAIProvider();
  }
});
document.addEventListener("change", (event) => {
  const select = event.target;
  if (select instanceof HTMLSelectElement && select.hasAttribute("data-reading-kind")) {
    showReadingInput(select);
  }
});

// Charts. A tooltip box is built with textContent only, so nothing read from the page is ever
// parsed as HTML. Its position is set through the style object (allowed by the CSP, unlike a style
// attribute in the markup). Without JavaScript every chart still reads as before: the server draws
// it and the words beside it say the numbers.
function makeTip(host) {
  const tip = document.createElement("div");
  tip.className = "chart-tip";
  tip.hidden = true;
  tip.setAttribute("aria-hidden", "true");
  host.append(tip);
  return tip;
}

// The tip is a reading slip: its left rule takes the colour of `tone` (a health, or a renewal
// call), and a line with a `figure` sets that number large with its word beside it.
const TIP_TONES = ["at_risk", "watch", "healthy", "commit", "likely", "none"];

function fillTip(tip, title, lines, tone) {
  tip.replaceChildren();
  TIP_TONES.forEach((name) => tip.classList.toggle(`chart-tip--${name}`, name === tone));
  const head = document.createElement("strong");
  head.className = "chart-tip-title";
  head.textContent = title;
  tip.append(head);
  lines.forEach((line) => {
    if (!line || (!line.text && line.figure === undefined)) return;
    if (line.figure !== undefined) {
      const reading = document.createElement("span");
      reading.className = "chart-tip-reading";
      const figure = document.createElement("span");
      figure.className = "chart-tip-figure";
      figure.textContent = String(line.figure);
      const word = document.createElement("span");
      word.className = `chart-tip-word${line.tone ? ` chart-tip-word--${line.tone}` : ""}`;
      word.textContent = line.text;
      reading.append(figure, word);
      tip.append(reading);
      return;
    }
    const row = document.createElement("span");
    row.className = `chart-tip-line${line.tone ? ` chart-tip-line--${line.tone}` : ""}`;
    row.textContent = line.text;
    tip.append(row);
  });
}

// Places the tip above the point (x, y), given in the host's own pixels, and keeps it inside.
function placeTip(tip, host, x, y) {
  tip.hidden = false;
  tip.classList.remove("chart-tip--beside");
  const width = tip.offsetWidth;
  const room = host.clientWidth;
  const left = Math.min(Math.max(x - width / 2, 0), Math.max(room - width, 0));
  tip.style.left = `${left}px`;
  tip.style.top = `${y}px`;
}

// Places the tip beside a vertical rule at x, level with the top of the host, so it never covers
// the point being read: to the right of the rule where it fits, else to the left.
function placeTipBeside(tip, host, x) {
  tip.hidden = false;
  tip.classList.add("chart-tip--beside");
  const width = tip.offsetWidth;
  const gap = 14;
  const room = host.clientWidth;
  const left = x + gap + width <= room ? x + gap : Math.max(x - gap - width, 0);
  tip.style.left = `${left}px`;
  tip.style.top = "0px";
}

// The risk trend on an account page: hover, or focus the chart and use the arrow keys, to read
// each day's risk, health, change from the reading before, and what happened that day.
function changeText(change) {
  if (change === null || change === undefined) return "";
  if (change === 0) return "No change from the reading before";
  return `${change > 0 ? "Up" : "Down"} ${Math.abs(change)} from the reading before`;
}

function setupTrend(figure) {
  let days;
  try {
    days = JSON.parse(figure.dataset.trend || "[]");
  } catch {
    return;
  }
  const svg = figure.querySelector(".trend-chart");
  const frame = figure.querySelector(".trend-frame");
  const focus = svg?.querySelector(".trend-focus");
  const rule = focus?.querySelector(".trend-rule");
  const ring = focus?.querySelector(".trend-ring");
  const say = figure.querySelector("[data-trend-say]");
  if (!svg || !frame || !focus || !rule || !ring || !Array.isArray(days) || days.length === 0) return;
  const tip = makeTip(frame);
  let current = -1;

  // The draw-in: the line's dash is set to its own length and run down to nothing, once.
  const line = svg.querySelector(".trend-line");
  if (line instanceof SVGGeometryElement && window.matchMedia("(prefers-reduced-motion: no-preference)").matches) {
    const length = line.getTotalLength();
    line.style.strokeDasharray = `${length}`;
    line.style.strokeDashoffset = `${length}`;
    line.classList.add("is-drawing");
    svg.querySelector(".trend-area")?.classList.add("is-drawing");
    line.addEventListener("animationend", () => {
      line.classList.remove("is-drawing");
      line.style.strokeDasharray = "";
      line.style.strokeDashoffset = "";
    }, { once: true });
  }

  const toFrame = (x, y) => {
    const matrix = svg.getScreenCTM();
    const box = frame.getBoundingClientRect();
    if (!matrix) return { x: 0, y: 0 };
    return { x: x * matrix.a + matrix.e - box.left, y: y * matrix.d + matrix.f - box.top };
  };

  const lines = (day) => {
    const out = [];
    if (typeof day.risk === "number") {
      out.push({ figure: day.risk, text: day.health, tone: day.tone });
      out.push({ text: changeText(day.change) });
      if (day.rank) out.push({ text: `Rank ${day.rank} in your book` });
    } else {
      out.push({ text: "No score kept this day" });
    }
    (day.events || []).forEach((event) => out.push({ text: event, tone: "event" }));
    return out;
  };

  const show = (index, announce) => {
    const day = days[index];
    if (!day) return;
    current = index;
    focus.classList.add("is-shown");
    rule.setAttribute("x1", day.x);
    rule.setAttribute("x2", day.x);
    const hasRisk = typeof day.y === "number";
    ring.setAttribute("class", `trend-ring${hasRisk ? ` trend-ring--${day.tone}` : " is-hidden"}`);
    if (hasRisk) {
      ring.setAttribute("cx", day.x);
      ring.setAttribute("cy", day.y);
    }
    const shown = lines(day);
    fillTip(tip, day.day, shown, hasRisk ? day.tone : "");
    placeTipBeside(tip, frame, toFrame(day.x, 0).x);
    if (announce && say) {
      const spoken = shown.map((line) => (line.figure !== undefined ? `Risk ${line.figure}, ${line.text}` : line.text));
      say.textContent = [day.day, ...spoken].filter(Boolean).join(". ");
    }
  };

  const hide = () => {
    current = -1;
    focus.classList.remove("is-shown");
    tip.hidden = true;
  };

  const nearest = (clientX) => {
    const matrix = svg.getScreenCTM();
    if (!matrix) return -1;
    const x = (clientX - matrix.e) / matrix.a;
    let best = 0;
    days.forEach((day, index) => {
      if (Math.abs(day.x - x) < Math.abs(days[best].x - x)) best = index;
    });
    return best;
  };

  svg.addEventListener("pointermove", (event) => {
    const index = nearest(event.clientX);
    if (index !== current) show(index, false);
  });
  svg.addEventListener("pointerleave", () => {
    if (document.activeElement !== svg) hide();
  });

  svg.setAttribute("tabindex", "0");
  svg.setAttribute("aria-describedby", "trend-keys");
  svg.addEventListener("focus", () => show(days.length - 1, true));
  svg.addEventListener("blur", hide);
  svg.addEventListener("keydown", (event) => {
    const moves = { ArrowLeft: -1, ArrowRight: 1, ArrowDown: -1, ArrowUp: 1 };
    let next = null;
    if (event.key in moves) next = Math.min(Math.max((current < 0 ? days.length - 1 : current) + moves[event.key], 0), days.length - 1);
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = days.length - 1;
    else if (event.key === "Escape") {
      hide();
      return;
    }
    if (next === null) return;
    event.preventDefault();
    show(next, true);
  });
}

document.querySelectorAll("[data-trend]").forEach(setupTrend);

// Stacked bars (the brief's ARR by health, the renewals page's ARR by call). Hovering a segment
// shows its figures; hovering a segment or its legend entry fades the others, so one band stands out.
function setupStack(chart) {
  const tip = makeTip(chart);
  const keyed = chart.querySelectorAll("[data-key]");
  const highlight = (key) => {
    chart.classList.toggle("is-picking", Boolean(key));
    keyed.forEach((item) => item.classList.toggle("is-picked", item.dataset.key === key));
  };
  chart.querySelectorAll("[data-tip-title]").forEach((segment) => {
    segment.addEventListener("pointermove", (event) => {
      highlight(segment.dataset.key);
      fillTip(tip, segment.dataset.tipTitle, [{ text: segment.dataset.tipText || "" }], segment.dataset.key);
      const box = chart.getBoundingClientRect();
      const bar = segment.closest("svg")?.getBoundingClientRect();
      placeTip(tip, chart, event.clientX - box.left, (bar ? bar.top : event.clientY) - box.top);
    });
    segment.addEventListener("pointerleave", () => {
      highlight("");
      tip.hidden = true;
    });
  });
  chart.querySelectorAll("li[data-key]").forEach((entry) => {
    entry.addEventListener("pointerenter", () => highlight(entry.dataset.key));
    entry.addEventListener("pointerleave", () => highlight(""));
  });
}

document.querySelectorAll("[data-stack]").forEach(setupStack);

// A model whose providers may train on what they're sent (an OpenRouter ":free" model). The model
// field says so as it is typed, and every use asks first: a form marked data-training-confirm
// (with the name of a checkbox that must be ticked, when it has one) opens this dialog instead of
// submitting. Proceed stays disabled for five seconds, its cover drawing back from right to left,
// so it can't be clicked through by reflex. Proceed adds allow_training=1, which the server
// requires; without JavaScript the server's refusal offers the same choice.
const TRAINING_WAIT_SECONDS = 5;
const FREE_MODEL = /:free\s*$/i;

document.addEventListener("input", (event) => {
  const field = event.target;
  if (!(field instanceof HTMLInputElement) || !field.hasAttribute("data-training-model-field")) {
    return;
  }
  const warning = document.querySelector("[data-training-warning]");
  if (warning) warning.hidden = !FREE_MODEL.test(field.value);
});

function svgIcon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `/static/icons.svg#${name}`);
  svg.append(use);
  return svg;
}

function makeTrainingDialog() {
  const dialog = document.createElement("dialog");
  dialog.className = "confirm-dialog";
  dialog.setAttribute("aria-labelledby", "training-dialog-title");
  dialog.setAttribute("aria-describedby", "training-dialog-text");

  const head = document.createElement("div");
  head.className = "confirm-dialog-head";
  const mark = document.createElement("span");
  mark.className = "confirm-dialog-mark";
  mark.append(svgIcon("health-watch"));
  const title = document.createElement("h2");
  title.className = "confirm-dialog-title";
  title.id = "training-dialog-title";
  title.textContent = "Use a model that may train on your data?";
  head.append(mark, title);

  const text = document.createElement("p");
  text.className = "confirm-dialog-text";
  text.id = "training-dialog-text";

  const wait = document.createElement("p");
  wait.className = "confirm-dialog-wait";

  const actions = document.createElement("div");
  actions.className = "confirm-dialog-actions";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "button";
  cancel.textContent = "Cancel";
  cancel.addEventListener("click", () => finishTraining(false));
  const proceed = document.createElement("button");
  proceed.type = "button";
  proceed.className = "button-primary proceed-wait";
  const label = document.createElement("span");
  label.className = "proceed-wait-label";
  label.textContent = "Proceed";
  const cover = document.createElement("span");
  cover.className = "proceed-wait-cover";
  cover.setAttribute("aria-hidden", "true");
  proceed.append(label, cover);
  proceed.addEventListener("click", () => finishTraining(true));
  actions.append(cancel, proceed);

  dialog.append(head, text, wait, actions);
  // Escape, or the dialog closing any other way, is a Cancel.
  dialog.addEventListener("cancel", () => finishTraining(false));
  dialog.addEventListener("close", () => finishTraining(false));
  document.body.append(dialog);
  return { dialog, text, wait, cancel, proceed };
}

let trainingDialog = null;
let pendingTraining = null; // the form waiting on the dialog, and the dialog's clock

// Proceed or Cancel. The buttons call this directly rather than through the dialog's close
// event, which a page that isn't painting can hold back.
function finishTraining(go) {
  if (!pendingTraining) return;
  const { form, submitter, tick, animation } = pendingTraining;
  pendingTraining = null;
  window.clearInterval(tick);
  animation.cancel();
  if (trainingDialog.dialog.open) trainingDialog.dialog.close();
  if (!go || trainingDialog.proceed.disabled) return;
  const field = document.createElement("input");
  field.type = "hidden";
  field.name = "allow_training";
  field.value = "1";
  form.append(field);
  form.dataset.trainingOk = "1";
  if (submitter && submitter.form === form) form.requestSubmit(submitter);
  else form.requestSubmit();
}

function confirmTraining(form, submitter) {
  if (!trainingDialog) trainingDialog = makeTrainingDialog();
  const { dialog, text, wait, cancel, proceed } = trainingDialog;
  const model = form.dataset.trainingModel || "This model";
  const what = form.dataset.trainingWhat || "the customer's website";
  text.textContent =
    `${model} is a free model. Its providers may keep what Wardmind sends, the text from ` +
    `${what}, and train on it. Wardmind otherwise only uses providers that don't.`;

  // Proceed unlocks once five seconds have passed and the cover has gone. The cover can start a
  // frame late; a window that isn't painting at all never draws it, and then the clock alone
  // decides (nobody can be looking at it).
  const cover = proceed.querySelector(".proceed-wait-cover");
  const total = TRAINING_WAIT_SECONDS * 1000;
  const animation = cover.animate([{ transform: "scaleX(1)" }, { transform: "scaleX(0)" }], {
    duration: total,
    easing: "linear",
    fill: "forwards",
  });
  const opened = performance.now();
  proceed.disabled = true;
  const check = () => {
    const elapsed = performance.now() - opened;
    const drawn = Number(animation.currentTime) || 0;
    if (elapsed >= total && (animation.playState === "finished" || drawn === 0)) {
      window.clearInterval(tick);
      animation.finish();
      wait.textContent = "";
      proceed.disabled = false;
      return;
    }
    const left = Math.max(1, Math.ceil((total - Math.min(elapsed, drawn || elapsed)) / 1000));
    wait.textContent = `You can proceed in ${left} second${left === 1 ? "" : "s"}.`;
  };
  const tick = window.setInterval(check, 200);
  pendingTraining = { form, submitter, tick, animation };
  check();
  dialog.showModal();
  cancel.focus();
}

// Capture runs before htmx's own submit handler on the form, so stopping here stops htmx too.
document.addEventListener(
  "submit",
  (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-training-confirm")) return;
    if (form.dataset.trainingOk === "1") {
      delete form.dataset.trainingOk; // this once: the next use asks again
      return;
    }
    form.querySelectorAll('input[name="allow_training"]').forEach((field) => field.remove());
    const needs = form.dataset.trainingConfirm;
    if (needs) {
      const box = form.elements.namedItem(needs);
      if (!(box instanceof HTMLInputElement) || !box.checked) return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
    confirmTraining(form, event.submitter);
  },
  true,
);
