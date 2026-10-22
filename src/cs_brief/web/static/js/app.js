// Marks the page as scripted so CSS can hide the no-JavaScript submit button.
document.documentElement.classList.add("js");

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
  const wanted = select.selectedOptions[0]?.dataset.readingInput;
  if (!wanted || !select.form) return;
  select.form.querySelectorAll("[data-reading-field]").forEach((field) => {
    field.hidden = field.dataset.readingField !== wanted;
  });
}

document.querySelectorAll("select[data-reading-kind]").forEach(showReadingInput);
document.addEventListener("change", (event) => {
  const select = event.target;
  if (select instanceof HTMLSelectElement && select.hasAttribute("data-reading-kind")) {
    showReadingInput(select);
  }
});
