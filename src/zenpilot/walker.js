// Runs in the page via script.callFunction. Walks the rendered tree (including
// open shadow roots) and returns [items, truncated], where each item is
// [depth, line, node|null, frameWindow|null]. Nodes come back as BiDi sharedIds.
(maxItems) => {
  const INTERACTIVE = [
    "a[href]", "button", "input:not([type=hidden])", "select", "textarea", "summary",
    "[contenteditable='']", "[contenteditable=true]", "[onclick]", "[tabindex]:not([tabindex='-1'])",
    ...["button", "link", "checkbox", "radio", "tab", "menuitem", "menuitemcheckbox", "menuitemradio",
        "option", "switch", "textbox", "combobox", "searchbox", "slider", "spinbutton", "treeitem"]
      .map((r) => `[role=${r}]`),
  ].join(",");
  const LANDMARKS = new Set(["HEADER", "NAV", "MAIN", "ASIDE", "FOOTER", "FORM", "DIALOG", "FIELDSET"]);
  const LANDMARK_ROLES = new Set(["banner", "navigation", "main", "complementary", "contentinfo",
    "form", "dialog", "alertdialog", "search"]);
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "HEAD", "svg", "math"]);
  const INPUT_ROLES = { checkbox: "checkbox", radio: "radio", button: "button", submit: "button",
    reset: "button", image: "button", range: "slider", search: "searchbox", file: "file" };

  const clip = (s, n) => {
    s = (s || "").replace(/\s+/g, " ").trim();
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  };
  const q = (s) => JSON.stringify(s);
  // Parent in the rendered (flat) tree: slotted elements render inside their slot.
  const parentOf = (el) => el.assignedSlot || el.parentElement || el.getRootNode().host || null;

  // Elements that only a JS handler makes clickable: the outermost one with a pointer
  // cursor, neither inside nor around a real control (labels already name their control).
  const pointer = new Set();
  const isPointer = (el) => {
    if (el.tagName === "LABEL" || el === document.body || getComputedStyle(el).cursor !== "pointer") return false;
    if (el.querySelector(INTERACTIVE)) return false;
    const p = parentOf(el);
    if (p instanceof Element && getComputedStyle(p).cursor === "pointer") return false;
    for (let a = el; a; a = parentOf(a)) if (a.matches(INTERACTIVE)) return false;
    return true;
  };
  const interactive = (el) => el.matches(INTERACTIVE) || pointer.has(el);

  // Mark every ancestor of something interesting, so containers without
  // interactive content can be emitted as a single text line.
  const hasInner = new Set();
  const roots = [document];
  for (let i = 0; i < roots.length; i++) {
    for (const el of roots[i].querySelectorAll("*")) {
      if (isPointer(el)) pointer.add(el);
      const interesting = el.shadowRoot || interactive(el) || el.tagName === "IFRAME" || el.tagName === "FRAME";
      if (el.shadowRoot) roots.push(el.shadowRoot);
      if (!interesting) continue;
      for (let p = parentOf(el); p && !hasInner.has(p); p = parentOf(p)) hasInner.add(p);
    }
  }

  const rendered = (el) =>
    el.checkVisibility({ checkVisibilityCSS: true }) || getComputedStyle(el).display === "contents";
  const hasBox = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const role = (el) => {
    const r = el.getAttribute("role");
    if (r) return r;
    switch (el.tagName) {
      case "A": return "link";
      case "BUTTON": case "SUMMARY": return "button";
      case "SELECT": return "select";
      case "TEXTAREA": return "textbox";
      case "INPUT": return INPUT_ROLES[el.type] || "textbox";
    }
    return el.isContentEditable ? "textbox" : "clickable";
  };

  const name = (el) => {
    let n = el.getAttribute("aria-label");
    const ids = !n && el.getAttribute("aria-labelledby");
    if (ids) n = ids.split(/\s+/).map((id) => el.getRootNode().getElementById?.(id)?.textContent || "").join(" ");
    if (!n && el.labels?.length) n = el.labels[0].innerText;
    if (!n && ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName)) {
      n = el.placeholder || el.title || (["submit", "button", "reset"].includes(el.type) ? el.value : "") || el.alt;
    }
    if (!n) n = el.innerText || el.title || el.querySelector?.("img[alt]")?.alt || "";
    return clip(n, 80);
  };

  const describe = (el) => {
    const r = role(el);
    let line = r;
    const n = name(el);
    if (n) line += " " + q(n);
    if (r === "textbox" && el.tagName === "INPUT" && el.type !== "text") line += ` type=${el.type}`;
    if (el.tagName === "SELECT") {
      line += " value=" + q(clip(el.selectedOptions[0]?.text, 40));
    } else if ((r === "textbox" || r === "searchbox") && el.value) {
      line += " value=" + (el.type === "password" ? '"•••"' : q(clip(el.value, 60)));
    } else if (el.isContentEditable && el.innerText.trim() && n !== clip(el.innerText, 80)) {
      line += " value=" + q(clip(el.innerText, 60));
    }
    if (el.checked || el.getAttribute("aria-checked") === "true") line += " checked";
    if (el.getAttribute("aria-selected") === "true") line += " selected";
    const exp = el.getAttribute("aria-expanded");
    if (exp) line += exp === "true" ? " expanded" : " collapsed";
    if (el.disabled || el.getAttribute("aria-disabled") === "true") line += " disabled";
    if (el.tagName === "A" && el.href && !el.href.startsWith("javascript:")) {
      try {
        const u = new URL(el.href);
        const short = u.origin === location.origin ? u.pathname + u.search : u.host + u.pathname;
        line += " -> " + clip(short, 60);
      } catch {}
    }
    return line;
  };

  const out = [];
  let truncated = false;
  const emit = (depth, line, node = null, frame = null) => {
    if (out.length >= maxItems) { truncated = true; return false; }
    out.push([depth, line, node, frame]);
    return true;
  };

  const children = (el) => {
    if (el.shadowRoot) return [...el.shadowRoot.childNodes];
    if (el.tagName === "SLOT") {
      const assigned = el.assignedNodes({ flatten: true });
      return assigned.length ? assigned : [...el.childNodes];
    }
    return [...el.childNodes];
  };

  // named: the name of the control we're inside, whose text needn't be repeated.
  const walk = (node, depth, named = "") => {
    if (truncated) return;
    if (node.nodeType === 3) {
      const t = clip(node.data, 100);
      const parent = node.parentElement;
      if (t.length > 1 && !named.includes(t) && !(parent && interactive(parent)) && !parent?.closest("label")?.control) emit(depth, q(t));
      return;
    }
    if (node.nodeType !== 1) return;
    const el = node;
    if (SKIP.has(el.tagName) || el.getAttribute("aria-hidden") === "true" || !rendered(el)) return;

    if (el.tagName === "IFRAME" || el.tagName === "FRAME") {
      if (hasBox(el)) emit(depth, "frame " + q(clip(el.title || el.name || el.src, 60)), null, el.contentWindow);
      return;
    }
    if (interactive(el)) {
      if (hasBox(el)) emit(depth, describe(el), el);
      if (hasInner.has(el)) for (const c of children(el)) walk(c, depth + 1, name(el));
      return;
    }
    if (/^H[1-6]$/.test(el.tagName)) {
      const t = clip(el.innerText, 100);
      if (t) emit(depth, el.tagName.toLowerCase() + " " + q(t));
      if (hasInner.has(el)) for (const c of children(el)) walk(c, depth + 1, named);
      return;
    }
    if (el.tagName === "IMG") {
      const alt = clip(el.alt, 80);
      if (alt && el.width >= 24 && el.height >= 24) emit(depth, "img " + q(alt));
      return;
    }
    const roleAttr = el.getAttribute("role");
    if (LANDMARKS.has(el.tagName) || LANDMARK_ROLES.has(roleAttr)) {
      const label = clip(el.getAttribute("aria-label") || "", 40);
      const at = out.length;
      emit(depth, (roleAttr || el.tagName.toLowerCase()) + (label ? " " + q(label) : ""));
      for (const c of children(el)) walk(c, depth + 1, named);
      if (out.length === at + 1 && !truncated) out.pop(); // drop empty landmarks
      return;
    }
    // Short containers without interactive content become one text line.
    if (el.tagName === "LABEL" && el.control) {
      // Its text is already the control's name; still walk it for nested controls.
      if (hasInner.has(el)) for (const c of children(el)) walk(c, depth, named);
      return;
    }
    // A slot's innerText is its fallback content, not what's slotted, so always walk it.
    if (el.tagName !== "SLOT" && !hasInner.has(el) && (el.children.length === 0 || el.innerText.length <= 200)) {
      const t = clip(el.innerText, 100);
      if (t.length > 1 && !named.includes(t)) emit(depth, q(t));
      return;
    }
    for (const c of children(el)) walk(c, depth, named);
  };

  walk(document.body || document.documentElement, 0);
  return [out, truncated];
}
