import hashlib
import json
import statistics
import subprocess
from datetime import date, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from tools import create_tools
from .fixture_backend import FixtureBackend
from .graph import AnalyticsAgent


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["completed", "needs_clarification", "failed"]
    tools: list[str] = Field(default_factory=list)
    totals: list[str | None] = Field(default_factory=list)
    arguments: list[dict] = Field(default_factory=list)
    rows: list[dict] = Field(default_factory=list)
    comparison_delta: str | None = None
    error_code: str | None = None
    warning_contains: str | None = None


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9_-]{1,80}$")
    question: str = Field(min_length=1, max_length=10000)
    current_date: date = date(2026, 11, 11)
    selected_organization_id: str | None = None
    history: str = ""
    max_rows: int = Field(default=1000, ge=1)
    expect: Expected


def read_cases(path):
    cases = [EvalCase.model_validate_json(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
             if line.strip()]
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("Нужен непустой набор кейсов с уникальными id.")
    return cases


def contains(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and contains(actual[key], value)
                                               for key, value in expected.items())
    return actual == expected


def grade(result, expected):
    checks = {"status": result.status == expected.status}
    if expected.error_code:
        checks["error_code"] = (result.error or {}).get("code") == expected.error_code
    if expected.tools:
        checks["tools"] = [row["tool"] for row in result.results] == expected.tools
    if expected.totals:
        checks["totals"] = [row["data"].get("total") for row in result.results] == expected.totals
    for i, expectation in enumerate(expected.arguments):
        checks[f"arguments_{i}"] = any(contains(record, expectation) for record in result.results)
    if expected.rows:
        rows = [row for record in result.results for row in record["data"].get("rows", [])]
        checks["rows"] = len(rows) == len(expected.rows) and all(contains(actual, wanted) for actual, wanted in zip(rows, expected.rows))
    if expected.comparison_delta is not None:
        checks["comparison"] = bool(result.comparisons) and result.comparisons[0]["delta"] == expected.comparison_delta
    if expected.warning_contains:
        checks["warning"] = any(expected.warning_contains in warning for warning in result.warnings)
    if expected.status == "needs_clarification":
        checks["no_calculations"] = not result.results and bool(result.message.strip())
    return {"passed": all(checks.values()), "checks": checks}


def write_json(path, payload):
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def default_output():
    stamp = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y%m%d-%H%M%S")
    return Path("runs") / f"{stamp}-{uuid4().hex[:6]}"


def git_info():
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True))
        return {"commit": head, "dirty": dirty}
    except (OSError, subprocess.CalledProcessError):
        return None


def code_hash():
    digest = hashlib.sha256()
    for directory in [Path(__file__).parent, Path(__file__).parent.parent / "tools"]:
        for path in sorted(p for p in directory.rglob("*") if p.is_file() and p.suffix in {".py", ".md", ".json"}):
            digest.update(str(path.relative_to(directory.parent)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def summarize(records):
    elapsed = sorted(row["result"]["elapsed_seconds"] for row in records)
    known_costs = [row["result"]["cost_usd"] for row in records if row["result"]["cost_usd"] is not None]
    from decimal import Decimal
    return {"cases": len(records), "passed": sum(row["grade"]["passed"] for row in records),
        "pass_rate": sum(row["grade"]["passed"] for row in records) / len(records) if records else 0,
        "mean_seconds": statistics.mean(elapsed) if elapsed else 0,
        "p95_seconds": elapsed[max(0, (95 * len(elapsed) + 99) // 100 - 1)] if elapsed else 0,
        "input_tokens": sum(row["result"]["usage"].get("input_tokens", 0) for row in records),
        "output_tokens": sum(row["result"]["usage"].get("output_tokens", 0) for row in records),
        "cost_usd": str(sum((Decimal(value) for value in known_costs), Decimal(0))) if known_costs else None,
        "cost_coverage": len(known_costs) / len(records) if records else 0,
        "failed_cases": [row["run_key"] for row in records if not row["grade"]["passed"]]}


async def evaluate(model, cases, *, settings, output, provider_config, prompt=None, timeout=60, repeat=1, progress=None):
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Папка результатов уже содержит файлы; выберите новую --out.")
    output.mkdir(parents=True, exist_ok=True)
    first_backend = FixtureBackend()
    agent = AnalyticsAgent(model, first_backend, settings=settings, prompt=prompt)
    toolset = create_tools(first_backend, first_backend.context("snapshot"))
    (output / "prompt.md").write_text(agent.prompt, encoding="utf-8")
    write_json(output / "tools.json", [convert_to_openai_tool(tool) for tool in toolset])
    (output / "cases.jsonl").write_text("".join(case.model_dump_json() + "\n" for case in cases), encoding="utf-8")
    write_json(output / "manifest.json", {"created_at": datetime.now(ZoneInfo("Europe/Moscow")).isoformat(),
        "provider": provider_config, "agent": settings.model_dump(), "timeout_seconds": timeout, "repeat": repeat,
        "backend": "synthetic", "dataset_hash": first_backend.fingerprint,
        "cases_hash": hashlib.sha256((output / "cases.jsonl").read_bytes()).hexdigest(),
        "code_hash": code_hash(), "git": git_info(),
        "packages": {name: version(name) for name in ["langgraph", "langchain-core", "langchain-openrouter", "pydantic"]}})
    records = []
    with (output / "results.jsonl").open("w", encoding="utf-8") as stream:
        for repetition in range(1, repeat + 1):
            for case in cases:
                backend = FixtureBackend(max_rows=case.max_rows)
                agent = AnalyticsAgent(model, backend, settings=settings, prompt=prompt)
                result = await agent.arun(case.question, backend.context(uuid4().hex, timeout_seconds=timeout),
                    current_date=case.current_date, selected_organization_id=case.selected_organization_id, history=case.history)
                record = {"run_key": f"{case.id}#{repetition}", "case_id": case.id, "question": case.question,
                          "input": case.model_dump(mode="json", exclude={"expect"}),
                          "expected": case.expect.model_dump(mode="json"), "result": result.model_dump(mode="json"),
                          "grade": grade(result, case.expect), "backend_calls": backend.calls}
                records.append(record)
                stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                stream.flush()
                write_json(output / "summary.json", {**summarize(records), "planned_cases": len(cases) * repeat,
                                                     "complete": len(records) == len(cases) * repeat})
                if progress:
                    progress(record)
    return {**summarize(records), "planned_cases": len(cases) * repeat, "complete": True}


def compare_runs(before, after):
    def load(folder):
        return {row["run_key"]: row for row in [json.loads(line) for line in (Path(folder) / "results.jsonl").read_text().splitlines() if line]}
    old, new = load(before), load(after)
    common = sorted(old.keys() & new.keys())
    if not common:
        raise ValueError("В прогонах нет общих кейсов.")
    for key in common:
        if (old[key]["input"], old[key]["expected"]) != (new[key]["input"], new[key]["expected"]):
            raise ValueError("Изменились входы или эталоны; сравнивайте один и тот же набор кейсов.")
    old_manifest = json.loads((Path(before) / "manifest.json").read_text())
    new_manifest = json.loads((Path(after) / "manifest.json").read_text())
    if old_manifest["dataset_hash"] != new_manifest["dataset_hash"]:
        raise ValueError("В прогонах различаются тестовые данные.")
    return {"common_cases": len(common), "before": summarize([old[key] for key in common]),
            "after": summarize([new[key] for key in common]),
            "improved": [key for key in common if not old[key]["grade"]["passed"] and new[key]["grade"]["passed"]],
            "regressed": [key for key in common if old[key]["grade"]["passed"] and not new[key]["grade"]["passed"]],
            "only_before": sorted(old.keys() - new.keys()), "only_after": sorted(new.keys() - old.keys())}
