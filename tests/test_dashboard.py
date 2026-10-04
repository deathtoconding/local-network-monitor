"""Contract tests for the dashboard assets.

The dashboard is plain HTML/CSS/JS with no build step and no framework, which
means nothing at build time catches the classic failures: a script that queries an
element the markup does not contain, an asset that quietly points at a CDN, or a
table that loses its accessibility scaffolding during a redesign.

These tests are that build step. They are deliberately about structure and
invariants, not pixels: behaviour is verified in a real DOM (jsdom) during
development, and the packaging test in ``test_api.py`` proves the assets ship and
are served.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "network_monitor" / "web"

HTML = (WEB_DIR / "index.html").read_text(encoding="utf-8")
CSS = (WEB_DIR / "css" / "app.css").read_text(encoding="utf-8")
JS = (WEB_DIR / "js" / "app.js").read_text(encoding="utf-8")

#: Section ids in the order the page tells its story. If a redesign reorders or
#: renames these, this list is the place to say so on purpose.
STORY_SECTIONS = [
    "now-title",  # 1 · right now
    "attention-title",  # 2 · what needs attention
    "trend-title",  # 3 · what has been happening
    "who-title",  # 4 · who is using the network
    "interfaces-title",  # 5 · where the traffic goes
    "health-title",  # 6 · the monitor itself
]


class TestElementContract:
    """Every element the script touches must exist in the markup."""

    def test_ids_referenced_by_javascript_exist_in_html(self):
        html_ids = set(re.findall(r'id="([^"]+)"', HTML))
        js_ids = set(re.findall(r'\$\("([^"]+)"\)', JS))
        assert js_ids, "the script should resolve elements through $()"
        missing = sorted(js_ids - html_ids)
        assert not missing, f"script queries elements that do not exist: {missing}"

    def test_every_rendered_list_has_a_container_and_an_empty_state(self):
        for container in (
            "attention-list",
            "process-list",
            "connections-body",
            "interfaces-body",
            "collectors-body",
        ):
            assert f'id="{container}"' in HTML, f"missing container {container}"
        for empty_state in ("attention-empty", "process-empty"):
            assert f'id="{empty_state}"' in HTML, f"missing empty state {empty_state}"

    def test_assets_referenced_by_html_exist_on_disk(self):
        referenced = re.findall(r'(?:href|src)="(/static/[^"?]+)', HTML)
        assert referenced, "the page must reference its own assets"
        for url in referenced:
            relative = url.removeprefix("/static/")
            assert (WEB_DIR / relative).is_file(), f"{url} does not exist in the package"


class TestOfflineAndPrivacyInvariants:
    """The dashboard must work on a machine with no internet access at all."""

    @pytest.mark.parametrize("asset", ["index.html", "css/app.css", "js/app.js"])
    def test_no_remote_resources(self, asset):
        text = (WEB_DIR / asset).read_text(encoding="utf-8")
        for pattern in ("http://", "https://", "//cdn.", "@import", "fonts.googleapis"):
            assert pattern not in text, f"{asset} references the network: {pattern}"

    def test_api_calls_are_relative(self):
        """Relative URLs are what let the dashboard work behind a proxy prefix."""
        paths = re.findall(r'"(api/[^"]*)"', JS)
        assert paths, "the script should call the API through relative paths"
        assert not any(path.startswith("/") for path in paths)

    def test_script_never_uses_innerhtml(self):
        code = re.sub(r"/\*.*?\*/", "", JS, flags=re.S)  # ignore comments
        assert "innerHTML" not in code, "use textContent/DOM building, never innerHTML"
        assert "document.write" not in code
        assert "eval(" not in code


class TestAccessibilityAndStory:
    """Structure that carries meaning for screen readers and for the narrative."""

    def test_landmarks_and_skip_link(self):
        assert HTML.count("<h1") == 1, "exactly one top-level heading"
        assert '<a class="skip-link" href="#main">' in HTML
        for landmark in ("<header", "<main", "<footer"):
            assert landmark in HTML

    def test_live_region_announces_the_verdict(self):
        assert 'aria-live="polite"' in HTML, "the headline must be announced when it changes"
        assert 'id="main"' in HTML and "aria-busy" in HTML

    def test_story_sections_are_present_and_ordered(self):
        positions = [HTML.find(f'id="{section}"') for section in STORY_SECTIONS]
        assert all(position > 0 for position in positions), "every story section must exist"
        assert positions == sorted(positions), "the story is told in order, top to bottom"

    def test_tables_have_captions_and_scoped_headers(self):
        tables = re.findall(r"<table>.*?</table>", HTML, flags=re.S)
        assert len(tables) >= 3, "connections, interfaces and collectors tables"
        for table in tables:
            assert "<caption" in table, "every table needs a caption for screen readers"
            headers = re.findall(r"<th ([^>]*)>", table)
            assert headers, "a data table needs headers"
            assert all("scope=" in attributes for attributes in headers)

    def test_severity_is_never_colour_alone(self):
        """Each state carries a word and a glyph, not just a colour."""
        assert "SEVERITY_GLYPH" in JS, "severity needs a non-colour affordance"
        assert ".severity--warning" in CSS and ".severity--critical" in CSS
        assert ".state-chip[data-state=" in CSS, "collector state needs the same treatment"

    def test_motion_and_contrast_preferences_are_honoured(self):
        assert "prefers-reduced-motion" in CSS
        assert "prefers-color-scheme" in CSS, "light and dark are both first-class"
        assert ":focus-visible" in CSS, "keyboard focus must stay visible"


class TestDesignSystem:
    """Tokens and states, so a redesign is a change of values, not of structure."""

    def test_tokens_are_defined_and_used(self):
        # Several tokens share a line (`--s1: 4px;  --s2: 8px; …`), so this must
        # not be anchored to the start of a line.
        declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", CSS))
        assert len(declared) >= 30, "the design system defines colour, space and type tokens"
        for required in (
            "--surface-1",
            "--text-muted",
            "--down",
            "--up",
            "--warn",
            "--crit",
            "--s" + "4",
        ):
            assert required in declared, f"missing token {required}"
        assert "var(--" in CSS

    def test_loading_empty_and_error_states_exist(self):
        assert 'body[data-state="loading"]' in CSS, "skeletons need a state hook"
        assert ".skeleton" in CSS
        assert ".empty-state" in CSS
        assert 'class="banner banner--error"' in HTML and 'role="alert"' in HTML

    def test_freshness_is_a_first_class_signal(self):
        """A monitoring dashboard that cannot say how old its data is is a hazard."""
        assert 'id="freshness"' in HTML
        for state in ("fresh", "stale", "failed", "waiting"):
            assert f'freshness[data-state="{state}"]' in CSS, f"missing freshness state {state}"


class TestDocumentationOfIntent:
    def test_files_explain_their_design_decisions(self):
        assert "Design notes" in CSS
        assert (
            "the UI is deliberately structured as a story" in JS.lower()
            or "structured as a story" in JS.lower()
        )
