import hashlib
import asyncio
import json
import re
import statistics
import subprocess
import time
from dataclasses import replace
from datetime import date, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from tools import BackendError, create_tools
from .fixture_backend import FixtureBackend
from .graph import AnalyticsAgent
from .models import AgentResult


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
    units: list[str] = Field(default_factory=list)
    source_rows: list[int] = Field(default_factory=list)
    source_kind: str | None = None
    no_calculations: bool = False
    message_patterns: list[str] = Field(default_factory=list)


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9_-]{1,80}$")
    question: str = Field(min_length=1, max_length=10000)
    category: str = "general"
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
    elif expected.status == "failed":
        checks["controlled_rejection"] = (result.error or {}).get("code") == "UNSUPPORTED_METRIC"
    if expected.tools:
        checks["tools"] = [row["tool"] for row in result.results] == expected.tools
    if expected.totals:
        checks["totals"] = [row["data"].get("total") for row in result.results] == expected.totals
    unmatched = list(result.results)
    for i, expectation in enumerate(expected.arguments):
        match = next((j for j, record in enumerate(unmatched) if contains(record, expectation)), None)
        checks[f"arguments_{i}"] = match is not None
        if match is not None:
            unmatched.pop(match)
    if expected.rows:
        rows = [row for record in result.results for row in record["data"].get("rows", [])]
        checks["rows"] = len(rows) == len(expected.rows) and all(contains(actual, wanted) for actual, wanted in zip(rows, expected.rows))
    if expected.comparison_delta is not None:
        checks["comparison"] = bool(result.comparisons) and result.comparisons[0]["delta"] == expected.comparison_delta
    if expected.warning_contains:
        checks["warning"] = any(expected.warning_contains in warning for warning in result.warnings)
    if expected.units:
        checks["units"] = [row["data"].get("unit") for row in result.results] == expected.units
    if expected.source_rows:
        checks["source_rows"] = [row.get("stats", {}).get("rows") for row in result.results] == expected.source_rows
    if expected.source_kind:
        checks["provenance"] = bool(result.results) and all(
            row.get("complete") is True and any(source.get("kind") == expected.source_kind
                and contains(source, {key: row["arguments"][key] for key in ["organization_id", "direction_id", "period"] if key in row["arguments"]})
                and source.get("entity") and source.get("catalog_version") and source.get("semantic_version")
                and source.get("mandatory_filters") == {"active": True, "movement_kind": "Поступление"}
                for source in row.get("sources", []) if isinstance(source, dict)) for row in result.results)
    if expected.no_calculations or expected.status == "needs_clarification":
        checks["no_calculations"] = not result.results and result.plan is None and not any(
            event.get("event") == "calculation" for event in result.trace) and bool(result.message.strip())
    for i, pattern in enumerate(expected.message_patterns):
        checks[f"message_{i}"] = re.search(pattern, result.message, flags=re.IGNORECASE) is not None
    if expected.status == "completed":
        checks["deadline"] = result.elapsed_seconds <= 60.5
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
    for directory in [Path(__file__).parent.parent / name for name in ["agent", "tools", "backend", "services"]]:
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


async def evaluate(model, cases, *, settings, output, provider_config, prompt=None, timeout=60, repeat=1, progress=None,
                   backend="fixture", onec_url="http://127.0.0.1:8011", onec_token="", allow_demo_metrics=False,
                   reference=None, snapshot=None):
    if backend not in {"fixture", "onec", "onec-snapshot"}:
        raise ValueError("Unknown evaluation backend")
    def make_backend(case=None):
        if backend == "fixture":
            return FixtureBackend(max_rows=case.max_rows if case else 1000)
        if backend == "onec-snapshot":
            if snapshot is None:
                raise ValueError("Для onec-snapshot нужен --snapshot с папкой захваченных данных.")
            from .onec_snapshot import OneCSnapshotBackend
            return OneCSnapshotBackend(snapshot)
        from backend.integrations.onec_data_backend import OneCDataBackend
        return OneCDataBackend(onec_url, onec_token)

    async def context_for(source, request_id):
        if backend == "fixture":
            return source.context(request_id, timeout_seconds=timeout)
        return await source.context(request_id, timeout_seconds=timeout, allow_demo_metrics=allow_demo_metrics)

    async def close(source):
        if backend != "fixture":
            await source.close()

    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Папка результатов уже содержит файлы; выберите новую --out.")
    output.mkdir(parents=True, exist_ok=True)
    first_backend = make_backend()
    agent = AnalyticsAgent(model, first_backend, settings=settings, prompt=prompt)
    try:
        toolset = create_tools(first_backend, await context_for(first_backend, "snapshot"))
        dataset_hash = (first_backend.fingerprint if backend != "onec" else
            hashlib.sha256(json.dumps(first_backend.bootstrap_info, sort_keys=True).encode()).hexdigest())
        if backend == "onec-snapshot" and reference and reference.get("snapshot_sha256") != dataset_hash:
            raise ValueError("Снимок не совпадает с источником golden set; выберите соответствующий набор.")
    finally:
        await close(first_backend)
    (output / "prompt.md").write_text(agent.prompt, encoding="utf-8")
    write_json(output / "tools.json", [convert_to_openai_tool(tool) for tool in toolset])
    (output / "cases.jsonl").write_text("".join(case.model_dump_json() + "\n" for case in cases), encoding="utf-8")
    write_json(output / "manifest.json", {"created_at": datetime.now(ZoneInfo("Europe/Moscow")).isoformat(),
        "provider": provider_config, "agent": settings.model_dump(), "timeout_seconds": timeout, "repeat": repeat,
        "backend": {"fixture": "synthetic", "onec": "onec_live", "onec-snapshot": "onec_snapshot"}[backend], "dataset_hash": dataset_hash,
        "dataset_hash_kind": "catalog_only_not_rows" if backend == "onec" else "records",
        "allow_demo_metrics": allow_demo_metrics, "reference": reference,
        "cases_hash": hashlib.sha256((output / "cases.jsonl").read_bytes()).hexdigest(),
        "code_hash": code_hash(), "git": git_info(),
        "packages": {name: version(name) for name in ["langgraph", "langchain-core", "langchain-openrouter", "pydantic"]}})
    records = []
    with (output / "results.jsonl").open("w", encoding="utf-8") as stream:
        for repetition in range(1, repeat + 1):
            for case in cases:
                source = make_backend(case)
                agent = AnalyticsAgent(model, source, settings=settings, prompt=prompt)
                started = time.monotonic()
                request_id = uuid4().hex
                try:
                    async with asyncio.timeout(timeout):
                        context = await context_for(source, request_id)
                        remaining = timeout - (time.monotonic() - started)
                        if remaining <= 0:
                            raise TimeoutError
                        result = await agent.arun(case.question, replace(context, timeout_seconds=remaining),
                            current_date=case.current_date, selected_organization_id=case.selected_organization_id, history=case.history)
                    result.elapsed_seconds = time.monotonic() - started
                except (BackendError, TimeoutError) as exc:
                    code = exc.code if isinstance(exc, BackendError) else "TIMEOUT"
                    message = exc.message if isinstance(exc, BackendError) else "Истекло время запроса, включая bootstrap."
                    result = AgentResult(status="failed", request_id=request_id, message=message,
                        error={"code": code, "message": message}, elapsed_seconds=time.monotonic() - started)
                finally:
                    await close(source)
                record = {"run_key": f"{case.id}#{repetition}", "case_id": case.id, "question": case.question,
                          "input": case.model_dump(mode="json", exclude={"expect"}),
                          "expected": case.expect.model_dump(mode="json"), "result": result.model_dump(mode="json"),
                          "category": case.category, "grade": grade(result, case.expect), "backend_calls": source.calls}
                records.append(record)
                stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                stream.flush()
                write_json(output / "summary.json", {**summarize(records), "planned_cases": len(cases) * repeat,
                                                     "complete": len(records) == len(cases) * repeat})
                if progress:
                    progress(record)
    by_category = {}
    for record in records:
        group = by_category.setdefault(record["category"], {"cases": 0, "passed": 0})
        group["cases"] += 1
        group["passed"] += int(record["grade"]["passed"])
    summary = {**summarize(records), "planned_cases": len(cases) * repeat, "complete": True, "by_category": by_category}
    write_json(output / "summary.json", summary)
    return summary


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
    return {"common_cases": len(common),
            "warnings": ["Данные живой 1С могут изменяться; хеш каталога не подтверждает неизменность записей."]
                if any(m.get("backend") == "onec_live" for m in [old_manifest, new_manifest]) else [],
            "before": summarize([old[key] for key in common]),
            "after": summarize([new[key] for key in common]),
            "improved": [key for key in common if not old[key]["grade"]["passed"] and new[key]["grade"]["passed"]],
            "regressed": [key for key in common if old[key]["grade"]["passed"] and not new[key]["grade"]["passed"]],
            "only_before": sorted(old.keys() - new.keys()), "only_after": sorted(new.keys() - old.keys())}
