"""Guards for the Tailwind CSS build pipeline.

`new_base.html` loads a single generated stylesheet, built from
`cl/assets/tailwind/input.css` and the classes found in the templates by
`npm run build` (production) or `npm run dev` (the docker dev stack and
CI). No Python code runs that step, so a toolchain change that breaks it
(a PostCSS plugin or Tailwind upgrade, a lost content glob) would
otherwise only show up as an unstyled v2 page. These tests make it a test
failure instead: one inspects the compiled stylesheet and one loads a v2
page in a browser and checks that its rules took effect.
"""

import re
import time

from django.conf import settings
from django.contrib.staticfiles import finders
from django.test.utils import override_settings
from django.urls import reverse
from selenium.webdriver.common.by import By
from timeout_decorator import timeout_decorator
from waffle.testutils import override_flag

from cl.tests.base import SELENIUM_TIMEOUT, BaseSeleniumTest
from cl.tests.cases import SimpleTestCase

BUILD_HINT = (
    "The Tailwind stylesheet is not built. Run `npm run build` (or "
    "`npm run dev`) in cl/ and try again."
)

# In the docker dev stack, which CI also uses, the stylesheet is written by
# the cl-tailwind-reload container shortly after start-up rather than being
# part of the checkout, so a test run can get ahead of it by a few seconds.
STYLESHEET_WAIT_SECONDS = 60


def find_stylesheet() -> str | None:
    """Locates the compiled Tailwind stylesheet the v2 templates load.

    Resolves `settings.TAILWIND_CSS_PATH` through the staticfiles finders,
    as `{% tailwind_css %}` does, so callers see the same file the pages
    serve. Waits up to `STYLESHEET_WAIT_SECONDS` for the file to appear and
    returns None if it never does.
    """
    deadline = time.monotonic() + STYLESHEET_WAIT_SECONDS
    while True:
        path = finders.find(settings.TAILWIND_CSS_PATH)
        if isinstance(path, str):
            return path
        if time.monotonic() >= deadline:
            return None
        time.sleep(1)


def normalize_css(css: str) -> str:
    """Collapses the formatting differences between dev and production CSS.

    `npm run dev` pretty-prints the stylesheet while `npm run build`
    minifies it. Dropping all whitespace and the semicolon before a closing
    brace makes a rule compare equal whichever way it was built, so tests
    can assert on compact rules without caring which build they got.
    """
    return re.sub(r"\s+", "", css).replace(";}", "}")


class TailwindStylesheetTest(SimpleTestCase):
    """Checks the compiled Tailwind stylesheet for rules the templates rely on.

    Every expected string is the compact form of a rule that exists today.
    A Tailwind or PostCSS upgrade that changes the output fails these on
    purpose: update them together with a review of the generated CSS.
    """

    def _stylesheet(self) -> str:
        """Reads and normalizes the compiled stylesheet, or fails with a hint."""
        path = find_stylesheet()
        if path is None:
            self.fail(BUILD_HINT)
        with open(path, encoding="utf-8") as f:
            return normalize_css(f.read())

    def assertRuleIn(self, rule: str, css: str) -> None:
        """Asserts that a compact CSS fragment appears in the normalized CSS."""
        self.assertIn(normalize_css(rule), css, f"Rule missing: {rule}")

    def test_preflight_and_base_layer_compiled(self) -> None:
        """Did `@tailwind base` and the `@layer base` rules in input.css compile?"""
        css = self._stylesheet()
        # Preflight proves the base directive ran. The h1 rules prove that
        # `@apply font-cooper ... md:text-display-lg` resolved the fonts,
        # sizes and screens declared in tailwind.config.js.
        self.assertRuleIn("box-sizing:border-box", css)
        self.assertRuleIn("h1{font-family:Cooper Hewitt,sans-serif", css)
        self.assertRuleIn("@media (min-width:768px){h1{font-size:40px", css)

    def test_template_classes_are_generated(self) -> None:
        """Did classes used in the templates make it into the stylesheet?

        Guards the content globs in tailwind.config.js and the class
        extraction behind them. The escaped responsive variants are the
        selectors most sensitive to the PostCSS selector parser.
        """
        expected = [
            ".flex{display:flex}",
            ".hidden{display:none}",
            ".items-center{align-items:center}",
            ".rounded-2xl{border-radius:1rem}",
            r".sm\:grid-cols-2{grid-template-columns:repeat(2,minmax(0,1fr))}",
            r".lg\:grid-cols-4{grid-template-columns:repeat(4,minmax(0,1fr))}",
        ]
        css = self._stylesheet()
        for rule in expected:
            with self.subTest(rule=rule):
                self.assertRuleIn(rule, css)

    def test_theme_extensions_compiled(self) -> None:
        """Did the project's theme extensions survive the build?"""
        css = self._stylesheet()
        # The `xs` screen, the greyscale palette and the DM Mono family are
        # all defined in tailwind.config.js rather than shipped by Tailwind.
        self.assertRuleIn("@media (min-width:392px)", css)
        self.assertRuleIn(
            ".text-greyscale-900{--tw-text-opacity:1;color:rgb(28 24 20/", css
        )
        self.assertRuleIn("DM Mono", css)


@override_settings(WAFFLE_CACHE_PREFIX="test_tailwind_selenium_waffle")
@override_flag("use_new_design", active=True)
class TailwindStylesheetSeleniumTest(BaseSeleniumTest):
    """Loads a v2 page in a browser and checks that Tailwind styled it.

    `TailwindStylesheetTest` reads the file; this covers the rest of the
    chain a visitor depends on: `{% tailwind_css %}` emits the link, the
    static server finds the file, and the browser applies its rules.

    `WAFFLE_CACHE_PREFIX` isolates this class's `use_new_design` cache
    namespace from parallel test workers. Without it, the shared Redis
    cache key gets `CACHE_EMPTY` poisoned by any worker that calls
    `flag_is_active("use_new_design")` against a DB where this test
    class didn't enable the flag, flipping our render to v1. The setting
    must precede `@override_flag` so the override's own flush/save go
    through the prefixed key.
    """

    def setUp(self) -> None:
        super().setUp()
        # Without the file the browser gets a 404 for the stylesheet and
        # the assertions below fail with unhelpful computed values.
        if find_stylesheet() is None:
            self.fail(BUILD_HINT)

    @timeout_decorator.timeout(SELENIUM_TIMEOUT)
    def test_v2_page_is_styled(self) -> None:
        """Do the base layer and a utility class reach the rendered page?"""
        self.browser.get(f"{self.live_server_url}{reverse('help_home')}")

        # `@layer base` in input.css gives every h1 the Cooper Hewitt family
        # and a semibold weight. A computed font-family reports the declared
        # family whether or not the font file loaded, so this holds offline.
        heading = self.browser.find_element(By.TAG_NAME, "h1")
        self.assertIn(
            "Cooper Hewitt", heading.value_of_css_property("font-family")
        )
        self.assertEqual("600", heading.value_of_css_property("font-weight"))

        # A utility class written in the template's own markup.
        container = self.browser.find_element(By.CSS_SELECTOR, "main .flex")
        self.assertEqual("flex", container.value_of_css_property("display"))
