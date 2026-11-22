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
