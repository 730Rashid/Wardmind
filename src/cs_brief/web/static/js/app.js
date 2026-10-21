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
