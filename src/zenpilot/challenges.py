"""Recognizing CAPTCHAs and bot checks, so Claude hands them to the person instead of clicking.

zenpilot never solves challenges. They exist to ask for a human, and the human is right there:
read_page marks them, actions on their frames are refused, and handoff_to_user asks the
person at the Mac to complete them in Zen.
"""

import re

# Frame URLs of challenge widgets, by vendor.
FRAME_PATTERNS = [
    (re.compile(r"^https://challenges\.cloudflare\.com/"), "Cloudflare Turnstile"),
    (re.compile(r"^https://([a-z0-9-]+\.)*hcaptcha\.com/"), "hCaptcha"),
    (re.compile(r"^https://(www\.)?(google\.com|recaptcha\.net)/recaptcha/"), "reCAPTCHA"),
    (re.compile(r"^https://([a-z0-9-]+\.)*captcha-delivery\.com/"), "DataDome"),
    (re.compile(r"^https://([a-z0-9-]+\.)*(arkoselabs\.com|funcaptcha\.com)/"), "Arkose Labs"),
    (re.compile(r"^https://([a-z0-9-]+\.)*(px-cloud\.net|px-cdn\.net|perimeterx\.net)/"), "HUMAN (PerimeterX)"),
    (re.compile(r"^https://([a-z0-9-]+\.)*(geetest\.com|geevisit\.com)/"), "GeeTest"),
    (re.compile(r"^https://([a-z0-9-]+\.)*awswaf\.com/"), "AWS WAF CAPTCHA"),
    (re.compile(r"^https://([a-z0-9-]+\.)*(friendlycaptcha\.com|frcapi\.com)/"), "Friendly Captcha"),
    (re.compile(r"^https://([a-z0-9-]+\.)*mtcaptcha\.com/"), "MTCaptcha"),
]

# Challenge markers in a page itself: [vendor, ...] for what's visible.
DETECT_JS = """() => {
  const found = new Set();
  const visible = (el) => {
    if (!el.checkVisibility?.({ checkVisibilityCSS: true })) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const markers = [
    [".cf-turnstile, #challenge-form, #challenge-stage, #cf-challenge-running, #turnstile-wrapper", "Cloudflare"],
    // Invisible variants put these classes on the submit button itself; that's not a challenge.
    [".g-recaptcha:not(button, input)", "reCAPTCHA"],
    [".h-captcha:not(button, input)", "hCaptcha"],
    ["#px-captcha, #px-captcha-wrapper", "HUMAN (PerimeterX)"],
    ["#ddv1-captcha-container, .captcha__human__container", "DataDome"],
    ["#FunCaptcha, div[data-pkey], #arkose-container", "Arkose Labs"],
    [".geetest_holder, .geetest_panel, .geetest_box", "GeeTest"],
    ["awswaf-captcha, #captcha-container[data-awswaf]", "AWS WAF CAPTCHA"],
    [".frc-captcha", "Friendly Captcha"],
  ];
  for (const [sel, vendor] of markers) {
    for (const el of document.querySelectorAll(sel)) if (visible(el)) { found.add(vendor); break; }
  }
  const title = document.title.trim();
  if (/^(just a moment|attention required!? \\| cloudflare|checking your browser)/i.test(title))
    found.add("Cloudflare");
  if (/^access to this page has been denied/i.test(title) && document.querySelector("#px-captcha"))
    found.add("HUMAN (PerimeterX)");
  return [...found];
}"""


def frame_vendor(url: str) -> str | None:
    """The challenge vendor whose widget a frame URL loads, if any.

    Invisible widgets (reCAPTCHA v3's badge, invisible hCaptcha) only score the visit in the
    background and ask nothing of anyone, so they don't count.
    """
    if "invisible" in url:
        return None
    return next((vendor for pattern, vendor in FRAME_PATTERNS if pattern.match(url)), None)


def note(vendors: list[str]) -> str:
    """What to tell the model when a challenge is on screen."""
    names = ", ".join(sorted(set(vendors)))
    return (
        f"[zenpilot: this page is showing a CAPTCHA or bot check ({names}). Don't try to solve it or "
        "click it. Call handoff_to_user so the person can complete it in Zen, then read_page again.]"
    )
