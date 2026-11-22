// The only client-side behaviour. Everything else is plain forms that POST and redirect.

// Web Awesome themes switch on a .wa-dark class rather than prefers-color-scheme.
const dark = matchMedia("(prefers-color-scheme: dark)");
const applyTheme = () => document.documentElement.classList.toggle("wa-dark", dark.matches);
applyTheme();
dark.addEventListener("change", applyTheme);

// "No store yet" > "More": submit the picked store, or open the new-store dialog
// pointed at the same line.
document.addEventListener("wa-select", (event) => {
  const form = event.target.closest("form.store-picker");
  if (!form) return;
  const value = event.detail.item.value;
  if (value === ":new") {
    document.getElementById("new-store-form").action = form.action;
    document.getElementById("new-store").open = true;
    return;
  }
  form.append(Object.assign(document.createElement("input"), { type: "hidden", name: "store", value }));
  form.submit();
});
