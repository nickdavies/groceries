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

// Item page product search: fetch the same page with ?q= and swap in its results,
// instead of reloading. Falls back to a normal submit if the fetch fails.
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!form.matches("form.product-search")) return;
  event.preventDefault();
  const url = new URL(form.action, location.href);
  url.search = new URLSearchParams(new FormData(form));
  const button = form.querySelector("button");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Searching…";
  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error(response.statusText);
    const page = new DOMParser().parseFromString(await response.text(), "text/html");
    document.getElementById("search-results").replaceWith(page.getElementById("search-results"));
    history.replaceState(null, "", url.pathname + url.search + "#products");
  } catch {
    form.submit();
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
});

// Merge page: editing a unit or style re-renders the page with the new values and swaps
// in every [data-swap] element (the previews and the submit row, which carries the exact
// rewrites to write). The submit button stays disabled until the preview matches the
// fields, so what's written is always what was shown.
let mergeTimer;
document.addEventListener("input", (event) => {
  const form = event.target.closest("form.merge-form");
  if (!form) return;
  form.querySelector("#merge-submit button").disabled = true;
  clearTimeout(mergeTimer);
  mergeTimer = setTimeout(() => refreshMerge(form), 300);
});

async function refreshMerge(form) {
  const params = new URLSearchParams(new FormData(form));
  params.delete("forms");
  const url = `${form.action}?${params}`;
  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error(response.statusText);
    const page = new DOMParser().parseFromString(await response.text(), "text/html");
    for (const fresh of page.querySelectorAll("[data-swap]")) {
      document.getElementById(fresh.id)?.replaceWith(fresh);
    }
    history.replaceState(null, "", url);
  } catch {
    location.href = url;
  }
}
