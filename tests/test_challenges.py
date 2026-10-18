"""Recognizing CAPTCHA frames, so Claude hands them to the person instead of clicking them."""

import pytest

from zenpilot.challenges import frame_vendor, note


@pytest.mark.parametrize(
    "url,vendor",
    [
        ("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/abc", "Cloudflare Turnstile"),
        ("https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=checkbox", "hCaptcha"),
        ("https://www.google.com/recaptcha/api2/anchor?ar=1&k=abc&size=normal", "reCAPTCHA"),
        ("https://www.recaptcha.net/recaptcha/api2/bframe?k=abc", "reCAPTCHA"),
        ("https://geo.captcha-delivery.com/captcha/?initialCid=abc", "DataDome"),
        ("https://client-api.arkoselabs.com/fc/gc/?token=abc", "Arkose Labs"),
        # Invisible widgets only score the visit; nobody is asked anything.
        ("https://www.google.com/recaptcha/api2/anchor?ar=1&k=abc&size=invisible", None),
        ("https://www.google.com/maps/embed?pb=abc", None),
        ("https://evil.example/challenges.cloudflare.com/", None),
    ],
)  # fmt: skip
def test_frame_vendor(url, vendor):
    assert frame_vendor(url) == vendor


def test_note_points_to_handoff():
    text = note(["reCAPTCHA", "Cloudflare", "reCAPTCHA"])
    assert "(Cloudflare, reCAPTCHA)" in text and "handoff_to_user" in text and "Don't try to solve" in text
