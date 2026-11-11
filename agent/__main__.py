import argparse
import asyncio
import json
import sys
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic import ValidationError

from .evaluation import compare_runs, default_output, evaluate, read_cases
from .fixture_backend import FixtureBackend
from .graph import AnalyticsAgent
from .models import AgentSettings
from .providers import create_qwen_model


def parser():
    result = argparse.ArgumentParser(description="LangGraph-агент: Qwen через OpenRouter и синтетический backend.")
    commands = result.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Один вопрос")
    run.add_argument("question")
    run.add_argument("--organization", default=None, help="Выбранная в backend организация, например demo-org")
    run.add_argument("--today", type=date.fromisoformat, default=None)
    run.add_argument("--history", type=Path, help="UTF-8 файл с историей диалога")
    evaluation = commands.add_parser("eval", help="Прогон JSONL-набора с проверкой эталонов")
    evaluation.add_argument("--cases", type=Path, default=Path("evals/cases.jsonl"))
    evaluation.add_argument("--out", type=Path)
    evaluation.add_argument("--limit", type=int)
    evaluation.add_argument("--repeat", type=int, default=1)
    compare = commands.add_parser("compare", help="Сравнение двух прогонов")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    for command in [run, evaluation]:
        command.add_argument("--model", default=None)
        command.add_argument("--prompt", type=Path)
        command.add_argument("--reasoning", choices=["none", "minimal", "low", "medium", "high"], default="none")
        command.add_argument("--temperature", type=float, default=0.2)
        command.add_argument("--max-tokens", type=int, default=4096)
        command.add_argument("--max-turns", type=int, default=8)
        command.add_argument("--max-parallel", type=int, default=2)
        command.add_argument("--seed", type=int, default=42)
        command.add_argument("--timeout", type=float, default=60)
    return result


async def execute(args):
    if args.command == "compare":
        return compare_runs(args.before, args.after)
    if not 0 < args.timeout <= 60 or not 0 <= args.temperature <= 2 or not 128 <= args.max_tokens <= 32768:
        raise ValueError("Проверьте timeout (0..60], temperature [0..2] и max-tokens [128..32768].")
    settings = AgentSettings(max_model_turns=args.max_turns, max_parallel_steps=args.max_parallel)
    prompt = args.prompt.read_text(encoding="utf-8") if args.prompt else None
    cases = None
    if args.command == "eval":
        if not 1 <= args.repeat <= 10 or (args.limit is not None and args.limit < 1):
            raise ValueError("repeat должен быть от 1 до 10, limit должен быть положительным.")
        cases = read_cases(args.cases)
        cases = cases[:args.limit] if args.limit else cases
    model = create_qwen_model(model=args.model, temperature=args.temperature, max_tokens=args.max_tokens,
                              reasoning=args.reasoning, seed=args.seed)
    if args.command == "run":
        backend = FixtureBackend()
        agent = AnalyticsAgent(model, backend, settings=settings, prompt=prompt)
        answer = await agent.arun(args.question, backend.context(uuid4().hex, timeout_seconds=args.timeout),
            current_date=args.today or datetime.now(ZoneInfo("Europe/Moscow")).date(),
            selected_organization_id=args.organization,
            history=args.history.read_text(encoding="utf-8") if args.history else "",
            on_event=lambda stage: print(stage, file=sys.stderr))
        return answer.model_dump(mode="json")
    output = args.out or default_output()
    config = {"name": "openrouter", "model": model.model_name, "temperature": args.temperature,
              "max_tokens": args.max_tokens, "reasoning": args.reasoning, "seed": args.seed}
    summary = await evaluate(model, cases, settings=settings, output=output, provider_config=config,
        prompt=prompt, timeout=args.timeout, repeat=args.repeat,
        progress=lambda row: print(f"{row['run_key']}: {'PASS' if row['grade']['passed'] else 'FAIL'}", file=sys.stderr))
    return {"output": str(output), **summary}


def main():
    load_dotenv(Path.cwd() / ".env", override=False)
    args = parser().parse_args()
    try:
        payload = asyncio.run(execute(args))
    except (ValueError, OSError, ValidationError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        raise SystemExit(130)
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    if args.command == "eval" and payload["passed"] != payload["cases"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
