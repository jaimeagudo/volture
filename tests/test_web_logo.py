"""The logo embedded in the ESP32's web page (esp32/web_logo.js) must be the repository's logo."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_web_logo_matches_the_repository_logo():
    svg = (ROOT / "volture-logo.svg").read_text()
    assert json.dumps(svg) in (ROOT / "esp32/web_logo.js").read_text(), \
        "volture-logo.svg changed: regenerate the LOGO_SVG string in esp32/web_logo.js"
