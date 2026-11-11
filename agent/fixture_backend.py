import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path

from tools import BackendError, BackendResult, ToolContext


DATA_PATH = Path(__file__).parent / "fixtures" / "data.json"


def money(value):
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")


def words(value):
    return set(re.findall(r"\w+", value.casefold().replace("ё", "е")))


class FixtureBackend:
    """Synthetic analytics API for agent runs; all sums are Decimal."""

    def __init__(self, path=DATA_PATH, *, max_rows=1000, page_size=2):
        if max_rows < 1 or page_size < 1:
            raise ValueError("Fixture row and page limits must be positive")
        raw = Path(path).read_bytes()
        self.data = json.loads(raw)
        for table in ["sales", "receipts", "balances"]:
            for row in self.data[table]:
                value = row["amount"]
                if type(value) is not str or not re.fullmatch(r"-?\d{1,28}(\.\d{1,6})?", value, flags=re.ASCII):
                    raise ValueError("Суммы в фикстуре должны быть точными десятичными строками.")
        self.fingerprint = hashlib.sha256(raw).hexdigest()
        self.metrics = {item["metric_id"]: item for item in self.data["metrics"]}
        self.entities = {item["entity_name"] for item in self.data["metrics"]}
        self.max_rows, self.page_size = max_rows, page_size
        self.calls = []

    def context(self, request_id, *, timeout_seconds=60, max_calls=24):
        return ToolContext(request_id=request_id, base_id="synthetic",
            allowed_metric_ids=frozenset(self.metrics), allowed_organization_ids=frozenset({"demo-org"}),
            allowed_entity_names=frozenset(self.entities), timeout_seconds=timeout_seconds, max_calls=max_calls)

    def _aggregate(self, rows, group_by):
        totals = {}
        with localcontext() as ctx:
            ctx.prec = 50
            for row in rows:
                key = tuple(row[field] for field in group_by)
                totals[key] = totals.get(key, Decimal(0)) + Decimal(row["amount"])
            result = [{**dict(zip(group_by, key)), "value": money(value)} for key, value in sorted(totals.items())]
            return result, money(sum(totals.values(), Decimal(0))) if rows else None

    def _period_rows(self, table, args):
        period = args["period"]
        rows = [row for row in self.data[table] if row["organization_id"] == args["organization_id"]
                and row["posted"] is True and row["currency"] == "RUB"
                and period["start"] <= row["date"] < period["end_exclusive"]]
        if "direction_id" in args:
            rows = [row for row in rows if row["direction_id"] == args["direction_id"]]
        for key, value in args.get("filters", {}).items():
            rows = [row for row in rows if type(row.get(key)) is type(value) and row.get(key) == value]
        return rows

    def _balance_rows(self, args):
        point = datetime.fromisoformat(args["as_of"])
        available = [row for row in self.data["balances"] if row["organization_id"] == args["organization_id"]
                     and datetime.fromisoformat(row["as_of"]) <= point and row["currency"] == "RUB"]
        latest = max((datetime.fromisoformat(row["as_of"]) for row in available), default=None)
        return [row for row in available if datetime.fromisoformat(row["as_of"]) == latest
                and Decimal(row["amount"]) > 0 and
                (not args.get("counterparty_ids") or row["counterparty_id"] in args["counterparty_ids"])]

    async def call(self, operation, arguments, *, context, timeout):
        if context.base_id != "synthetic":
            raise BackendError("ACCESS_DENIED", "Тестовый backend относится к базе synthetic.")
        self.calls.append({"operation": operation, "arguments": arguments})
        if operation == "search_metrics":
            query = words(arguments["query"])
            candidates = []
            for metric in self.metrics.values():
                score = len(query & words(" ".join([metric["name"], *metric["synonyms"]])))
                if score:
                    candidates.append({"metric_id": metric["metric_id"], "name": metric["name"],
                                       "description": metric["description"], "approval": metric["approval"], "score": score})
            return BackendResult(data=sorted(candidates, key=lambda m: (-m["score"], m["metric_id"])), complete=True)
        if operation == "get_metric_definition":
            if arguments["metric_id"] not in self.metrics:
                raise BackendError("METRIC_NOT_FOUND", "Метрика отсутствует в синтетическом каталоге.")
            return BackendResult(data=self.metrics[arguments["metric_id"]], complete=True)
        if operation == "get_entity_schema":
            entity = arguments["entity_name"]
            return BackendResult(data={"entity_name": entity,
                "fields": {"amount": "Edm.Decimal", "date": "Edm.DateTime", "organization_id": "Edm.String",
                           "direction_id": "Edm.String", "counterparty_id": "Edm.String"},
                "functions": ["Balance"] if entity == "DemoBalances" else [],
                "expand_paths": [], "catalog_version": "synthetic-v1"}, complete=True)
        if operation == "resolve_dimension":
            table = {"organization": "organizations", "counterparty": "counterparties", "direction": "directions"}[arguments["dimension"]]
            query = arguments["query"].casefold().replace("ё", "е")
            candidates = [item for item in self.data[table] if any(query in label.casefold().replace("ё", "е")
                for label in [item["id"], item["name"], *item.get("synonyms", [])])]
            return BackendResult(data=candidates, complete=True)
        if arguments.get("organization_id") not in context.allowed_organization_ids:
            raise BackendError("ACCESS_DENIED", "Организация недоступна.")
        if operation in {"get_sales_turnovers", "get_top_counterparties"}:
            rows = self._period_rows("sales", arguments)
            groups = ["counterparty_id"] if operation == "get_top_counterparties" else arguments.get("group_by", [])
        elif operation == "get_cash_receipts_by_direction":
            rows = self._period_rows("receipts", arguments)
            groups = []
        elif operation == "get_customer_balances":
            rows = self._balance_rows(arguments)
            groups = arguments.get("group_by", [])
        else:
            raise BackendError("UNSUPPORTED_OPERATION", "Операция не поддерживается тестовым backend.")
        if len(rows) > self.max_rows:
            raise BackendError("ROW_LIMIT", "Превышен лимит выгрузки; сузьте период или фильтры.")
        grouped, total = self._aggregate(rows, groups)
        if operation == "get_top_counterparties":
            names = {item["id"]: item["name"] for item in self.data["counterparties"]}
            grouped.sort(key=lambda row: (-Decimal(row["value"]), row["counterparty_id"]))
            grouped = [{**row, "counterparty_name": names[row["counterparty_id"]]} for row in grouped[:arguments["limit"]]]
        source = {"entity": self.metrics[arguments["metric_id"]]["entity_name"], "kind": "synthetic",
                  "period": arguments.get("period"), "as_of": arguments.get("as_of"),
                  "organization_id": arguments["organization_id"], "filters": arguments.get("filters", {}),
                  "mandatory_filters": self.metrics[arguments["metric_id"]]["required_filters"]}
        return BackendResult(data={"rows": grouped, "total": total, "unit": "RUB"},
            sources=[source], warnings=["Синтетические данные: результат не является отчётностью заказчика."],
            stats={"rows": len(rows), "pages": max(1, (len(rows) + self.page_size - 1) // self.page_size)}, complete=True)
