// Runs in zenpilot's sandbox via script.callFunction: (tag, command, jsonArgument).
// Draws the "Claude is using this tab" layer: a cursor that glides to where zenpilot is about
// to click, a ripple on click, a glow around the page, a typing badge and a scroll hint. The
// host is a randomly named element in the top layer with a closed shadow root, inert,
// aria-hidden and pointer-events:none, so it never takes clicks, focus or hit tests, and
// read_page skips it. Styles are inline and animations use the Web Animations API, so a
// strict page CSP can't block them. State lives in the sandbox, which the page can't see.
// Every command keeps it alive; after a quiet minute it removes itself.
(tag, cmd, arg) => {
  const a = arg ? JSON.parse(arg) : {};
  const ORANGE = "#d97757";
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const css = (el, props) => {
    for (const [k, v] of Object.entries(props)) el.style.setProperty(k, v, "important");
    return el;
  };
  const div = (parent, props) => parent.appendChild(css(document.createElement("div"), props));

  const build = () => {
    const host = document.createElement(tag);
    host.setAttribute("aria-hidden", "true");
    host.inert = true;
    host.popover = "manual";
    css(host, {
      all: "initial", display: "block", position: "fixed", inset: "0", width: "100vw", height: "100vh",
      margin: "0", padding: "0", border: "0", background: "transparent", overflow: "visible",
      "max-width": "none", "max-height": "none", "pointer-events": "none", "z-index": "2147483647",
      "color-scheme": "normal",
    });
    const root = host.attachShadow({ mode: "closed" });
    const glowWrap = div(root, { position: "fixed", inset: "0", opacity: "0", transition: "opacity 400ms ease" });
    const glow = div(glowWrap, {
      position: "absolute", inset: "0",
      "box-shadow": `inset 0 0 0 2px ${ORANGE}, inset 0 0 18px 4px ${ORANGE}88, inset 0 0 42px 8px ${ORANGE}33`,
    });
    if (!reduced) glow.animate([{ opacity: 0.55 }, { opacity: 1 }],
      { duration: 1100, direction: "alternate", iterations: Infinity, easing: "ease-in-out" });
    const cursor = div(root, {
      position: "fixed", left: "0", top: "0", width: "24px", height: "24px", opacity: "0",
      transition: "opacity 250ms ease", filter: "drop-shadow(0 1px 2px rgba(0,0,0,.35))",
    });
    const svg = cursor.appendChild(document.createElementNS("http://www.w3.org/2000/svg", "svg"));
    for (const [k, v] of [["width", "24"], ["height", "24"], ["viewBox", "0 0 24 24"]]) svg.setAttribute(k, v);
    const arrow = svg.appendChild(document.createElementNS("http://www.w3.org/2000/svg", "path"));
    arrow.setAttribute("d", "M3 2 L3 19.5 L7.6 15.4 L10.9 22.6 L14 21.2 L10.8 14.2 L17 14.2 Z");
    for (const [k, v] of [["fill", ORANGE], ["stroke", "white"], ["stroke-width", "1.6"], ["stroke-linejoin", "round"]])
      arrow.setAttribute(k, v);
    const badge = div(root, {
      position: "fixed", left: "0", top: "0", padding: "3px 8px", "border-radius": "10px",
      background: ORANGE, color: "white", font: "600 11px/16px system-ui, sans-serif", opacity: "0",
      transition: "opacity 200ms ease", "white-space": "nowrap", "box-shadow": "0 1px 3px rgba(0,0,0,.3)",
    });
    document.documentElement.appendChild(host);
    return { host, root, glowWrap, cursor, badge, pos: null, timers: {} };
  };

  let s = globalThis.zenpilotOverlay;
  if (!s || !s.host.isConnected) {
    for (const old of document.querySelectorAll(tag)) old.remove(); // left by an earlier session
    if (cmd === "hide" || cmd === "show" || cmd === "remove") return true;
    s = globalThis.zenpilotOverlay = build();
  }
  const later = (name, ms, fn) => {
    clearTimeout(s.timers[name]);
    s.timers[name] = setTimeout(fn, ms);
  };
  const place = (x, y) => `translate(${x - 3}px, ${y - 2}px)`;

  if (cmd === "remove") {
    s.host.remove();
    globalThis.zenpilotOverlay = null;
    return true;
  }
  if (cmd === "hide" || cmd === "show") {
    css(s.host, { visibility: cmd === "hide" ? "hidden" : "visible" });
    // Resolve once the change is painted (two frames), or soon anyway in a hidden window.
    return new Promise((resolve) => {
      setTimeout(resolve, 150);
      requestAnimationFrame(() => requestAnimationFrame(resolve));
    });
  }

  // Stay above anything the page put in the top layer since the last command.
  try {
    if (s.host.matches(":popover-open")) s.host.hidePopover();
    s.host.showPopover();
  } catch {}
  s.glowWrap.style.setProperty("opacity", "1", "important");
  later("glow", 2500, () => s.glowWrap.style.setProperty("opacity", "0", "important"));
  later("cursor", 20000, () => s.cursor.style.setProperty("opacity", "0", "important"));
  later("remove", 60000, () => {
    s.host.remove();
    globalThis.zenpilotOverlay = null;
  });

  if (cmd === "move") {
    // points: [[x, y, ms since previous], ...], already eased; the cursor follows them linearly.
    const pts = a.points;
    if (!pts.length) return true;
    const start = s.pos || [pts[0][0], pts[0][1]];
    const total = pts.reduce((t, p) => t + p[2], 0);
    const step = Math.max(1, Math.ceil(pts.length / 48));
    const frames = [{ transform: place(...start), offset: 0 }];
    let t = 0;
    pts.forEach((p, i) => {
      t += p[2];
      if (i % step === 0 || i === pts.length - 1) frames.push({ transform: place(p[0], p[1]), offset: total ? t / total : 1 });
    });
    s.cursor.style.setProperty("opacity", "1", "important");
    s.cursor.style.setProperty("transform", frames.at(-1).transform, "important");
    if (total > 0 && !reduced) s.cursor.animate(frames, { duration: total, easing: "linear" });
    s.pos = pts.at(-1).slice(0, 2);
    return true;
  }
  if (cmd === "click") {
    const [x, y] = [a.x, a.y];
    s.cursor.style.setProperty("opacity", "1", "important");
    s.cursor.style.setProperty("transform", place(x, y), "important");
    s.pos = [x, y];
    if (!reduced) {
      const ring = div(s.root, {
        position: "fixed", left: `${x - 14}px`, top: `${y - 14}px`, width: "28px", height: "28px",
        "border-radius": "50%", border: `2px solid ${ORANGE}`, "box-sizing": "border-box",
      });
      ring.animate([{ transform: "scale(.4)", opacity: 1 }, { transform: "scale(1.6)", opacity: 0 }],
        { duration: 380, easing: "ease-out" }).finished.then(() => ring.remove(), () => ring.remove());
    }
    return true;
  }
  if (cmd === "badge") {
    // Shown just above (or below) a rect: "Typing…" on a field, "Scrolling" at the wheel point.
    const [x, y, w, h] = a.rect;
    s.badge.textContent = a.text;
    const top = y > 28 ? y - 24 : y + h + 6;
    css(s.badge, { opacity: "1", transform: `translate(${Math.max(4, Math.min(x, innerWidth - 120))}px, ${top}px)` });
    later("badge", a.ms || 1500, () => s.badge.style.setProperty("opacity", "0", "important"));
    return true;
  }
  return true; // "active": just the glow
}
