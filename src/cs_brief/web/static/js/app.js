// Marks the page as scripted so CSS can hide the no-JavaScript submit button.
document.documentElement.classList.add("js");

// Every print button opens the browser's print dialog, where "Save as PDF" is one of the choices.
document.querySelectorAll("[data-print]").forEach((button) => {
  button.addEventListener("click", () => window.print());
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
