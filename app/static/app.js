// Station-name/CRS autocomplete for the journey search form. Adapted from
// the same pattern in the direct-train-summary sibling app.

let stationsByLabel = new Map(); // lowercased "name (crs)" -> crs
let stationsByCrs = new Set();
let allStations = []; // [{crs, name, label}]

async function loadStations(delayMs = 2000) {
  try {
    const resp = await fetch("/api/stations");
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const stations = await resp.json();
    for (const s of stations) {
      const crs = s.crs_code.toUpperCase();
      const label = `${s.name} (${crs})`;
      stationsByLabel.set(label.toLowerCase(), crs);
      stationsByCrs.add(crs);
      allStations.push({ crs, name: s.name, label });
    }
    document.querySelectorAll("input.station-input").forEach(setUpAutocomplete);
  } catch (err) {
    // Most likely the dataset is still cold-starting — the app's own
    // documented cold-start window is ~30s, and /api/stations 503s until
    // it's ready — so retry indefinitely with a capped backoff rather than
    // giving up. Until it succeeds, the plain-text inputs still work
    // (typed CRS codes resolve at submit time, and the server validates/
    // renders a friendly error either way).
    setTimeout(() => loadStations(Math.min(delayMs * 1.5, 10000)), delayMs);
  }
}

// Ranks CRS matches ahead of name matches, exact/prefix ahead of substring,
// so e.g. searching "WAT" surfaces Waterloo (CRS match) before Watford,
// Wateringbury, etc. (name matches that happen to start with "wat" too).
function rankStation(query, station) {
  const q = query.toLowerCase();
  const crs = station.crs.toLowerCase();
  const name = station.name.toLowerCase();
  if (crs === q) return 0;
  if (crs.startsWith(q)) return 1;
  if (name.startsWith(q)) return 2;
  if (crs.includes(q)) return 3;
  if (name.includes(q)) return 4;
  return null;
}

function matchStations(query, limit = 8) {
  if (!query) return [];
  const ranked = [];
  for (const station of allStations) {
    const rank = rankStation(query, station);
    if (rank !== null) ranked.push({ rank, station });
  }
  ranked.sort((a, b) => a.rank - b.rank || a.station.name.localeCompare(b.station.name));
  return ranked.slice(0, limit).map(r => r.station);
}

let autocompleteSeq = 0;

function setUpAutocomplete(input) {
  // The station list can still be loading (see loadStations' retry) while
  // the user has already focused and started typing — replaceWith() below
  // would otherwise silently drop focus out from under them.
  const hadFocus = document.activeElement === input;
  const wrap = document.createElement("span");
  wrap.className = "autocomplete-wrap";
  input.replaceWith(wrap);
  wrap.appendChild(input);
  if (hadFocus) input.focus();

  const menuId = `${input.id || "station"}-menu-${autocompleteSeq++}`;
  const menu = document.createElement("ul");
  menu.className = "autocomplete-menu";
  menu.id = menuId;
  menu.setAttribute("role", "listbox");
  menu.hidden = true;
  wrap.appendChild(menu);

  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-controls", menuId);

  let activeIndex = -1;
  let currentMatches = [];

  function setActive(index) {
    const items = menu.children;
    if (activeIndex >= 0 && items[activeIndex]) items[activeIndex].classList.remove("active");
    activeIndex = index;
    if (activeIndex >= 0 && items[activeIndex]) {
      items[activeIndex].classList.add("active");
      items[activeIndex].scrollIntoView({ block: "nearest" });
      input.setAttribute("aria-activedescendant", items[activeIndex].id);
    } else {
      input.removeAttribute("aria-activedescendant");
    }
  }

  function closeMenu() {
    menu.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    activeIndex = -1;
  }

  function selectStation(station) {
    input.value = station.label;
    closeMenu();
  }

  function render(query) {
    currentMatches = matchStations(query);
    menu.innerHTML = "";
    activeIndex = -1;
    if (!currentMatches.length) {
      closeMenu();
      return;
    }
    currentMatches.forEach((station, i) => {
      const item = document.createElement("li");
      item.id = `${menuId}-opt-${i}`;
      item.setAttribute("role", "option");
      item.textContent = station.label;
      item.addEventListener("mousedown", (e) => {
        e.preventDefault();
        selectStation(station);
      });
      menu.appendChild(item);
    });
    menu.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }

  input.addEventListener("input", () => render(input.value.trim()));
  input.addEventListener("focus", () => render(input.value.trim()));
  input.addEventListener("blur", closeMenu);

  input.addEventListener("keydown", (e) => {
    if (menu.hidden || !currentMatches.length) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((activeIndex + 1) % currentMatches.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((activeIndex - 1 + currentMatches.length) % currentMatches.length);
    } else if (e.key === "Enter") {
      if (activeIndex >= 0) {
        e.preventDefault();
        selectStation(currentMatches[activeIndex]);
      }
    } else if (e.key === "Escape") {
      closeMenu();
    }
  });
}

// Resolves a raw autocomplete input value to a known station, matching
// either the full "Name (CRS)" label (picked from the list) or a bare CRS
// code typed directly. Returns null if it matches neither.
function lookupStation(rawValue) {
  const value = rawValue.trim();
  if (!value) return null;
  const byLabel = stationsByLabel.get(value.toLowerCase());
  if (byLabel) return byLabel;
  const upper = value.toUpperCase();
  if (stationsByCrs.has(upper)) return upper;
  return null;
}

function setUpSearchForm() {
  const form = document.getElementById("search-form");
  if (!form) return;

  function validateField(input, errorEl) {
    const raw = input.value.trim();
    // Station list hasn't loaded (or failed to) — nothing to validate
    // against yet, so let the server be the source of truth.
    if (!allStations.length) return true;

    const resolved = lookupStation(raw);
    if (resolved) {
      input.value = resolved;
      errorEl.hidden = true;
      return true;
    }
    // Not a recognized name/label, but shaped like a CRS code (e.g. a typo
    // or a station this feed doesn't have) — let the server's own
    // unknown-station error report it rather than blocking client-side.
    if (/^[A-Za-z]{3}$/.test(raw)) {
      input.value = raw.toUpperCase();
      errorEl.hidden = true;
      return true;
    }
    errorEl.textContent = "Select a station from the list, or enter its 3-letter CRS code.";
    errorEl.hidden = false;
    return false;
  }

  form.addEventListener("submit", (e) => {
    const fromOk = validateField(document.getElementById("from_"), document.getElementById("from_-error"));
    const toOk = validateField(document.getElementById("to"), document.getElementById("to-error"));
    if (!fromOk || !toOk) e.preventDefault();
  });
}

loadStations();
setUpSearchForm();
