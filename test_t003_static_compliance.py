"""Static compliance tests for the "today" HTML page.

Validates that the HTML file:
  - exists and is encoded as UTF-8
  - maps weekdays (getDay() 0-6) to Chinese weekday strings
  - uses dynamic Date logic instead of hardcoded dates
  - has no external network dependencies
  - contains no hardcoded dead dates
"""

import os
import re

# Directory containing this test file
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# All HTML files in the project root that must comply
HTML_FILES = [
    os.path.join(BASE_DIR, "today.html"),
    os.path.join(BASE_DIR, "today_weekday.html"),
    os.path.join(BASE_DIR, "weekday.html"),
]

# Canonical Chinese weekday names, indexed by getDay() (0 = Sunday)
CHINESE_WEEKDAYS = [
    "星期日",
    "星期一",
    "星期二",
    "星期三",
    "星期四",
    "星期五",
    "星期六",
]


def load_content(path):
    """Load an HTML file and return its content as a UTF-8 decoded string."""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def test_file_exists_and_utf8():
    for path in HTML_FILES:
        assert os.path.exists(path), f"HTML file missing: {path}"
        content = load_content(path)
        # HTML must declare UTF-8 charset
        assert re.search(
            r'charset\s*=\s*["\']?utf-8["\']?', content, re.IGNORECASE
        ), f"{path} does not declare UTF-8 encoding"


def test_chinese_weekday_mapping():
    for path in HTML_FILES:
        content = load_content(path)
        # A weekdays array mapping getDay() 0-6 to Chinese must exist
        match = re.search(r"weekdays\s*=\s*\[([^\]]*)\]", content)
        assert match, f"{path} does not define a weekdays array"
        array_text = match.group(1)
        for weekday in CHINESE_WEEKDAYS:
            assert weekday in array_text, f"{path} is missing Chinese weekday: {weekday}"


def test_dynamic_date_logic():
    for path in HTML_FILES:
        content = load_content(path)
        # Must construct the current date dynamically
        assert "new Date()" in content, f"{path} does not use new Date()"
        # Must use the Date API to compute the current weekday/date
        assert "getDay" in content, f"{path} does not call getDay()"
        assert "getFullYear" in content, f"{path} does not call getFullYear()"
        assert "getMonth" in content, f"{path} does not call getMonth()"
        assert "getDate" in content, f"{path} does not call getDate()"


def test_no_external_network_dependencies():
    for path in HTML_FILES:
        content = load_content(path)
        # No http(s) URLs anywhere in the file
        assert "http://" not in content and "https://" not in content, (
            f"{path} references an external network URL"
        )
        # No external scripts or stylesheets
        assert re.search(r"<script[^>]*\bsrc\s*=", content) is None, (
            f"{path} loads an external script"
        )
        assert re.search(r"<link[^>]*\bhref\s*=", content) is None, (
            f"{path} loads an external stylesheet"
        )


def test_no_hardcoded_dead_dates():
    for path in HTML_FILES:
        content = load_content(path)
        # No hardcoded 4-digit year literals (e.g. 2025)
        years = re.findall(r"\b(?:19|20)\d{2}\b", content)
        assert not years, f"{path} contains hardcoded year literal(s): {years}"
        # No hardcoded date strings such as 2025-01-01 or 2025年1月1日
        assert re.search(r"\b(?:19|20)\d{2}\s*[-/年.]", content) is None, (
            f"{path} contains a hardcoded date string"
        )


def main():
    """Run all compliance tests (works with or without pytest)."""
    tests = [
        test_file_exists_and_utf8,
        test_chinese_weekday_mapping,
        test_dynamic_date_logic,
        test_no_external_network_dependencies,
        test_no_hardcoded_dead_dates,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print("All static compliance tests passed.")


if __name__ == "__main__":
    main()
