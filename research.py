"""research.py - STUDENT IMPLEMENTS.  The main script.   Guide: GUIDE.md, part 3.

Usage:  python research.py "survey about world model"
Result: reports/<slug>.md   reports/<slug>.sources.json   reports/<slug>.meta.json
"""
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

from agents import FINALIZER_PATH, REPORT_PATH, SOURCES_PATH, VALIDATOR_PATH, WORKDIR, build_lead_agent
from model import make_model
from sandbox import download, open_sandbox, upload

ROOT = Path(__file__).parent
REPORTS = ROOT / "reports"
VALIDATOR_SOURCE = ROOT / "check_citations.py"
FINALIZER_SOURCE = ROOT / "finalize_citations.py"   # provided: uploaded next to your validator


def slugify(topic):
    """Turn a topic into a safe file name: lower case, runs of non-word characters become one "-", max 60 chars,
    never empty (fall back to "topic"). The topic is user input: "../../x" must not escape reports/."""
    slug = re.sub(r"[^a-z0-9]+", "-", topic.strip().lower()).strip("-")
    return (slug or "topic")[:60]


def build_prompt(topic):
    """The user message sent to the lead agent."""
    return (
        f'Write a cited survey report on the topic: "{topic}".\n\n'
        "Follow your system prompt exactly: plan with write_todos, delegate every sub-question to the `researcher` "
        "subagent in parallel with full context, merge their notes into sources.json, write the report body, run "
        "the finalizer and your validator until it prints OK, and spot-check a few claims with `citation-checker` "
        "before you stop."
    )


def summarize(messages, elapsed, model_name):
    """Return {"model", "elapsed_s", "subagent_calls", "tool_calls": {name: count}, "tokens": {"input", "output"}}."""
    tool_calls = Counter()
    tokens = {"input": 0, "output": 0}
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
            if name:
                tool_calls[name] += 1
        usage = getattr(message, "usage_metadata", None) or {}
        tokens["input"] += usage.get("input_tokens", 0) or 0
        tokens["output"] += usage.get("output_tokens", 0) or 0
    return {
        "model": model_name,
        "elapsed_s": round(elapsed, 1),
        "subagent_calls": tool_calls.get("task", 0),
        "tool_calls": dict(tool_calls),
        "tokens": tokens,
    }


def save_outputs(backend, topic, messages, elapsed, model_name, reports_dir=REPORTS):
    """Download the report from the sandbox and write the three files into reports_dir. Return the report path."""
    files = download(backend, [REPORT_PATH, SOURCES_PATH])
    report = files.get(REPORT_PATH)
    raw_sources = files.get(SOURCES_PATH)
    if not report or not report.strip():
        raise RuntimeError(f"no (or empty) report at {REPORT_PATH}")
    if not raw_sources:
        raise RuntimeError(f"no sources.json at {SOURCES_PATH}")
    try:
        sources = json.loads(raw_sources)
    except ValueError as exc:
        raise RuntimeError(f"sources.json is not valid JSON: {exc}") from exc
    if not isinstance(sources, list) or not sources:
        raise RuntimeError("sources.json must be a non-empty JSON list")

    reports_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(topic)
    meta = {
        "topic": topic,
        **summarize(messages, elapsed, model_name),
        "n_sources": len(sources),
        "source_families": sorted({s.get("source") for s in sources if s.get("source")}),
    }
    report_path = reports_dir / f"{slug}.md"
    report_path.write_bytes(report)
    (reports_dir / f"{slug}.sources.json").write_text(json.dumps(sources, ensure_ascii=False, indent=2),
                                                      encoding="utf-8")
    (reports_dir / f"{slug}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


def main(topic):
    """Return the process exit code (0 ok, 1 failed run, 2 no topic)."""
    topic = topic.strip()
    if not topic:
        print("usage: python research.py \"<topic>\"", file=sys.stderr)
        return 2

    model = make_model()
    start = time.monotonic()
    with open_sandbox() as backend:  # always stopped/removed on exit, even on error
        backend.execute(f"mkdir -p {WORKDIR}/research/notes {WORKDIR}/report")
        upload(backend, {
            VALIDATOR_PATH: VALIDATOR_SOURCE.read_bytes(),
            FINALIZER_PATH: FINALIZER_SOURCE.read_bytes(),
        })
        agent = build_lead_agent(backend, model)
        try:
            result = agent.invoke({"messages": [{"role": "user", "content": build_prompt(topic)}]},
                                  config={"recursion_limit": 1000})
        except Exception as exc:
            print(f"FAILED: agent run raised {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        elapsed = time.monotonic() - start
        try:
            report_path = save_outputs(backend, topic, result["messages"], elapsed, str(model_name_of(model)))
        except RuntimeError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1

    print(f"saved {report_path}")
    return 0


def model_name_of(model):
    """Best-effort human-readable model name for meta.json (model objects vary by provider)."""
    return getattr(model, "model_name", None) or getattr(model, "model", None) or type(model).__name__


if __name__ == "__main__":
    sys.exit(main(" ".join(sys.argv[1:])))
