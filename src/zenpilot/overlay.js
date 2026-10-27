// Runs in the page via script.callFunction before a click, type or hover, so the user can
// watch what Claude does. Display only: aria-hidden, pointer-events none, no layout change,
// and walker.js skips it. rect is the target's {x, y, w, h} in viewport pixels.
(x, y, kind, label, rect) => {
  const ID = "__zenpilot_overlay";
  const FADE_MS = 1500;
  const COLOR = "#e8590c";
  let root = document.getElementById(ID);
  if (!root || !root.isConnected) {
    root = document.createElement("div");
    root.id = ID;
    root.setAttribute("aria-hidden", "true");
    root.style.cssText = "position:fixed;inset:0;z-index:2147483647;pointer-events:none;" +
      "margin:0;padding:0;border:0;opacity:1;transition:opacity 300ms;";
    const style = document.createElement("style");
    style.textContent = "@keyframes __zp_ripple{from{transform:scale(1);opacity:.9}to{transform:scale(4);opacity:0}}";
    const box = document.createElement("div");
    box.dataset.part = "box";
    box.style.cssText = `position:fixed;border:2px solid ${COLOR};border-radius:4px;` +
      "box-shadow:0 0 0 1px rgba(255,255,255,.6);";
    const cursor = document.createElement("div");
    cursor.dataset.part = "cursor";
    cursor.style.cssText = `position:fixed;width:14px;height:14px;margin:-7px 0 0 -7px;border-radius:50%;` +
      `background:rgba(232,89,12,.55);border:2px solid #fff;box-sizing:border-box;` +
      "transition:left 250ms ease,top 250ms ease;";
    const chip = document.createElement("div");
    chip.dataset.part = "label";
    chip.style.cssText = "position:fixed;padding:2px 6px;border-radius:4px;background:#1a1a1a;color:#fff;" +
      "font:12px/1.4 system-ui,sans-serif;white-space:nowrap;";
    root.append(style, box, cursor, chip);
    document.documentElement.appendChild(root);
  }
  const part = (name) => root.querySelector(`[data-part="${name}"]`);
  const box = part("box");
  const cursor = part("cursor");
  const chip = part("label");

  root.style.opacity = "1";
  Object.assign(box.style, { left: rect.x + "px", top: rect.y + "px",
    width: rect.w + "px", height: rect.h + "px", display: "block" });
  Object.assign(cursor.style, { left: x + "px", top: y + "px" });
  chip.textContent = label;
  Object.assign(chip.style, { left: x + 12 + "px", top: y + 12 + "px" });

  if (kind === "click") {
    const ripple = document.createElement("div");
    ripple.style.cssText = `position:fixed;left:${x}px;top:${y}px;width:14px;height:14px;margin:-7px 0 0 -7px;` +
      `border:2px solid ${COLOR};border-radius:50%;box-sizing:border-box;` +
      "animation:__zp_ripple 400ms ease-out forwards;";
    root.appendChild(ripple);
    setTimeout(() => ripple.remove(), 450);
  }

  clearTimeout(root._zpFade);
  root._zpFade = setTimeout(() => { root.style.opacity = "0"; }, FADE_MS);
}
