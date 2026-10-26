"""Host-side, non-executable validators. Exit 0 alone is never functional success."""

from html.parser import HTMLParser
import json

from .contracts import Contract, canonical


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.elements = []

    def handle_starttag(self, tag, attrs):
        if tag not in {"br", "img", "hr", "meta", "link", "input", "source", "wbr"}:
            self.stack.append([tag, []])

    def handle_data(self, data):
        for _, fragments in self.stack:
            fragments.append(data)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                element = self.stack.pop(i)
                self.elements.append([tag, " ".join("".join(element[1]).split())])
                return


def content_lines(text):
    """Non-blank lines without trailing whitespace: wording and line order count, blank-line spacing doesn't."""
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def evaluate(contract: Contract, result: dict) -> dict:
    checks = []
    fixtures = contract.data["fixtures"]
    if not result.get("setup_ok"):
        return {"passed": False, "checks": [], "reason": "Setup did not complete"}
    outputs = result.get("outputs", [])
    for i, fixture in enumerate(fixtures):
        item = outputs[i] if i < len(outputs) else {}
        passed = False
        reason = "Missing output, failed process or truncated output"
        if item.get("name") == fixture["name"] and item.get("exit_code") == 0 and not item.get("truncated"):
            actual = item.get("content", "")
            expected = fixture["expected"]
            try:
                if fixture["validator"] == "text":
                    passed = actual.strip() == expected.strip()
                elif fixture["validator"] == "text_lines":
                    passed = content_lines(actual) == content_lines(expected)
                elif fixture["validator"] == "json":
                    passed = canonical(json.loads(actual)) == canonical(expected)
                else:
                    parser = Elements()
                    parser.feed(actual)
                    passed = all(element in parser.elements for element in expected)
                reason = "Expected functional output matched" if passed else "Functional output differs"
            except (ValueError, TypeError):
                reason = "Output could not be parsed"
        checks.append({"name": fixture["name"], "passed": passed, "reason": reason})
    return {"passed": bool(checks) and all(x["passed"] for x in checks), "checks": checks}
