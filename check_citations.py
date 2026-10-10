"""check_citations.py - STUDENT IMPLEMENTS `check`.   Runs INSIDE the sandbox (standard library only).

research.py uploads this file to the sandbox and the lead agent runs it with the `execute` tool:
    python3 /tmp/work/research/check_citations.py [report.md] [sources.json]
It must exit 0 and print "OK: ..." when the report is consistent, else print each problem and exit 1.
"""
import json
import re
import sys

REPORT = "/tmp/work/report/report.md"
SOURCES = "/tmp/work/research/sources.json"

_REF_HEADING = re.compile(r"(?m)^##[ \t]+References[ \t]*$")
_CODE = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
_GROUP = re.compile(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\](?!\()")  # [3] [1, 2] [1-3]; not [3](link)
_REF_LINE = re.compile(r"(?m)^\[(\d+)\](.*)$")
_URL = re.compile(r"https?://\S+")
_PLACEHOLDER_HOSTS = ("example.com", "example.org", "example.net", "test.com", "localhost", "yourdomain.com")
_PLACEHOLDER_HOST_RE = re.compile(
    r"^https?://(www\.)?(example|sample|placeholder|yourdomain|foo|test)\d*\.(com|org|net|io|co)(/|$)", re.I
)
_ARXIV_ABS = re.compile(r"^https://arxiv\.org/abs/\d{4}\.\d{4,5}$")
_HF_PAPER = re.compile(r"^https://huggingface\.co/papers/\d{4}\.\d{4,5}$")


def _group_numbers(group):
    numbers = []
    for part in re.split(r"\s*,\s*", group):
        span = re.fullmatch(r"(\d+)\s*[–-]\s*(\d+)", part)
        if span:
            a, b = int(span.group(1)), int(span.group(2))
            numbers.extend(range(a, b + 1) if 0 <= b - a <= 200 else [a, b])
        else:
            numbers.append(int(part))
    return numbers


def check(report_text, sources):
    """Return a list of problem strings (empty list = OK). See GUIDE.md part 4 for the 6 rules enforced here."""
    problems = []
    if not sources:
        return ["no sources in sources.json"]

    by_n, seen_urls = {}, {}
    for entry in sources:
        n, url = entry.get("n"), entry.get("url")
        if not isinstance(n, int):
            problems.append(f"source n={n!r} is not an integer")
        else:
            by_n[n] = entry
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            problems.append(f"source n={n!r} has a bad url: {url!r}")
            continue
        if url in seen_urls:
            problems.append(f"duplicate url between sources {seen_urls[url]!r} and {n!r}: {url}")
        else:
            seen_urls[url] = n
        if any(host in url for host in _PLACEHOLDER_HOSTS) or _PLACEHOLDER_HOST_RE.match(url):
            problems.append(f"source n={n!r} uses a placeholder/fake domain: {url}")
        family = entry.get("source")
        if family == "arxiv" and not _ARXIV_ABS.match(url):
            problems.append(f"source n={n!r} is labeled 'arxiv' but url is not a real arxiv.org/abs/<id> link "
                            f"(fabricated id, wrong path, or mislabeled family): {url}")
        if family in ("hf-daily", "hf-search") and not _HF_PAPER.match(url):
            problems.append(f"source n={n!r} is labeled {family!r} but url is not a real "
                            f"huggingface.co/papers/<id> link (fabricated id or mislabeled family): {url}")

    headings = list(_REF_HEADING.finditer(report_text))
    if not headings:
        problems.append("report is missing the '## References' heading")
        return problems
    body = report_text[: headings[-1].start()]
    references = report_text[headings[-1].end() :]

    cited = set()
    for i, segment in enumerate(_CODE.split(body)):
        if i % 2:  # odd segments are code spans/blocks: never scanned
            continue
        for match in _GROUP.finditer(segment):
            cited.update(_group_numbers(match.group(1)))

    for n in sorted(cited - by_n.keys()):
        problems.append(f"[{n}] cited but missing from sources.json")
    for n in sorted(by_n.keys() - cited):
        problems.append(f"source [{n}] never cited")

    line_numbers = []
    for match in _REF_LINE.finditer(references):
        n, rest = int(match.group(1)), match.group(2)
        line_numbers.append(n)
        urls = _URL.findall(rest)
        if len(urls) != 1:
            problems.append(f"reference line [{n}] must contain exactly one URL, found {len(urls)}")
        elif n in by_n and urls[0] != by_n[n]["url"]:
            problems.append(f"reference line [{n}] URL {urls[0]!r} does not match sources.json url {by_n[n]['url']!r}")

    counts = {}
    for n in line_numbers:
        counts[n] = counts.get(n, 0) + 1
    for n, count in counts.items():
        if count > 1:
            problems.append(f"reference number [{n}] appears {count} times in References")
        if n not in by_n:
            problems.append(f"reference line [{n}] does not correspond to any source")
    for n in by_n:
        if n not in counts:
            problems.append(f"source [{n}] has no line in References")

    return problems


def main(argv):
    report_path = argv[1] if len(argv) > 1 else REPORT
    sources_path = argv[2] if len(argv) > 2 else SOURCES
    try:
        with open(report_path, encoding="utf-8") as f:
            report = f.read()
        with open(sources_path, encoding="utf-8") as f:
            sources = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"cannot read inputs: {exc}")
        return 1
    problems = check(report, sources)
    if problems:
        print("\n".join(problems))
        return 1
    print(f"OK: {len(sources)} sources, all citations resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
