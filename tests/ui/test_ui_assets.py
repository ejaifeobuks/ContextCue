"""Static checks on the kiosk front end (services/ui/assets).

The UI must work offline on the board, reference only icons it bundles, keep
exactly two primary tabs, and call only actions the application implements.
The pure presentation rules are unit-tested in test_ui_logic.test.js, which
this module runs when Node.js is installed.
"""

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "services/ui/assets"
sys.path.insert(0, str(ROOT / "tests/application"))
import support  # noqa: E402,F401  (sets the import paths for contextcue_app)

from contextcue_app import ContextCueApp  # noqa: E402


def read(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def sprite_ids() -> set[str]:
    return set(re.findall(r'<symbol id="([a-z0-9-]+)"', read("icons/sprite.svg")))


def arguments(text: str) -> list[str]:
    """Top-level arguments of a call whose "(" was just before `text`."""
    args, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                return args + [text[start:i]]
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(text[start:i])
            start = i + 1
    return args + [text[start:]]


def quoted_names(arg: str) -> set[str]:
    # Skip comparison operands such as tone === "ok".
    return set(re.findall(r'(?<!=== )"([a-z][a-z0-9-]*)"', arg))


def referenced_icons() -> set[str]:
    names = set()
    html = read("index.html")
    names |= set(re.findall(r'<use href="#([a-z0-9-]+)"', html))
    app = read("app.js")
    # icon(name, cls): first argument; pill(tone, text, icon) and
    # group(title, body, icon): third argument.
    for fn, index in (("icon", 0), ("pill", 2), ("group", 2)):
        for match in re.finditer(rf"\b{fn}\(", app):
            args = arguments(app[match.end():])
            if len(args) > index:
                names |= quoted_names(args[index])
    names |= set(re.findall(r'\["[a-z]+", "[^"]+", "([a-z0-9-]+)"\]', app))  # SECTIONS
    logic = read("ui-logic.js")
    names |= set(re.findall(r'\bicon: "([a-z0-9-]+)"', logic))
    category_icons = re.search(r"CATEGORY_ICON = \{(.*?)\};", logic, re.S).group(1)
    names |= set(re.findall(r':\s*"([a-z0-9-]+)"', category_icons))
    return names


class AssetTests(unittest.TestCase):
    def test_no_network_resources(self):
        for name in ("index.html", "styles.css", "app.js", "ui-logic.js", "icons/sprite.svg", "scene.svg"):
            text = read(name)
            self.assertNotRegex(text, r"(?:src|href)\s*=\s*[\"']?(?:https?:)?//", name)
            self.assertNotRegex(text, r"@import|url\(\s*[\"']?(?:https?:)?//", name)
            self.assertNotIn("fonts.googleapis", text, name)

    def test_every_referenced_icon_is_bundled(self):
        used = referenced_icons()
        self.assertGreater(len(used), 30)  # the scan is finding the icon calls
        self.assertEqual(sorted(used - sprite_ids()), [])

    def test_icons_are_inlined_once_not_referenced_externally(self):
        # External <use href="file.svg#id"> references re-resolve on every
        # re-render; the page inlines the sprite once instead.
        for name in ("index.html", "app.js"):
            self.assertNotIn("sprite.svg#", read(name), name)
        self.assertIn('fetch("icons/sprite.svg")', read("app.js"))

    def test_font_is_bundled_with_its_license(self):
        self.assertTrue((ASSETS / "fonts/inter-latin-wght-normal.woff2").is_file())
        self.assertIn("SIL Open Font License", read("fonts/LICENSE"))
        self.assertIn('url("fonts/inter-latin-wght-normal.woff2")', read("styles.css"))

    def test_icon_license_is_bundled(self):
        self.assertIn("ISC License", read("icons/LICENSE"))
        self.assertIn("lucide-static 0.460.0", read("icons/sprite.svg"))

    def test_weather_map_covers_exactly_the_contract_conditions(self):
        contract = (ROOT / "services/application/contextcue_app/contracts.py").read_text()
        conditions = re.search(r"condition: str\s+# ([a-z |]+)", contract).group(1)
        expected = {c.strip() for c in conditions.split("|")}
        block = re.search(r"const WEATHER = \{(.*?)\};", read("ui-logic.js"), re.S).group(1)
        self.assertEqual(set(re.findall(r"^\s*([a-z]+):", block, re.M)), expected)

    def test_exactly_two_primary_tabs(self):
        nav = re.search(r'<nav id="tabbar".*?</nav>', read("index.html"), re.S).group(0)
        self.assertEqual(re.findall(r'data-tab="([a-z]+)"', nav), ["glance", "settings"])

    def test_ui_actions_exist_in_the_application(self):
        app = read("app.js")
        actions = set(re.findall(r'\bact\("([a-z_]+)"', app))
        self.assertGreater(len(actions), 10)
        missing = sorted(a for a in actions if not hasattr(ContextCueApp, f"_act_{a}"))
        self.assertEqual(missing, [])


@unittest.skipUnless(shutil.which("node"), "Node.js not installed")
class LogicTests(unittest.TestCase):
    def test_ui_logic_node_suite(self):
        result = subprocess.run(["node", "--test", str(Path(__file__).with_name("test_ui_logic.test.js"))],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
