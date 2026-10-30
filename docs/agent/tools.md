# Базовые tools AI-агента

Агент работает как граф LangGraph. LangChain связывает локальную LLM с типизированными
инструментами. Модель понимает вопрос и составляет план, валидатор проверяет его,
а ToolNode обращается к API данных. Расчёты, пагинация, связи и доступ к штатному
OData 1С находятся в слое данных.

![Архитектура агента](architecture.png)

## Стек и модели

Python 3.12, FastAPI, LangGraph StateGraph, LangChain, Pydantic, HTTPX,
PostgreSQL для истории, аудита и checkpoints, Redis для кэша с TTL.
Локальный инференс - vLLM; для LLM-трейсов и оценки качества - self-hosted
Langfuse, для технической наблюдаемости - OpenTelemetry.

Варианты LLM: **Qwen3.8-27B** и **DeepSeek-V4-Flash**. Для Qwen используется
`Qwen/Qwen3.8-27B`; у DeepSeek рассматривается релиз
`deepseek-ai/DeepSeek-V4-Flash-0731`. Версии весов, шаблона чата и парсера tools
фиксируются вместе. Между моделями выбираем по качеству на вопросах про продажи,
поступления, взаиморасчёты, даты и уточнения, а также по задержке ответа.

## Устройство графа

1. **Контекст и каталог** - загрузить историю, права, выбранную базу, время и
   релевантные определения из каталога 27 опубликованных наборов.
2. **Планировщик** - выбрать метрику и инструменты, разрешить названия организаций
   и направлений, сформировать конкретные даты и план.
3. **Валидация** - проверить JSON Schema, доступы, обязательные параметры,
   версии определений, зависимости шагов и бюджет выполнения.
4. **Уточнение** - при нехватке контекста приостановить граф через interrupt;
   после ответа пользователя продолжить через resume с обновлённым контекстом.
5. **ToolNode** - выполнить разрешённые операции плана через адаптер API данных.
   Результаты подготовительных инструментов возвращаются планировщику.
6. **Проверка результата** - проверить полноту, валюту и единицы, период,
   применённые фильтры, источники и контрольные правила метрики.
7. **Ответ** - собрать число/таблицу и объяснение расчёта; передать результат в чат.

Состояние графа содержит сообщения, разрешённый контекст, найденные метрики,
недостающие параметры, план, результаты шагов, источники, предупреждения и счётчики
попыток. Checkpoints сохраняются в PostgreSQL. Пароли и большие выгрузки остаются
за пределами состояния и промпта. Ключ выполнения учитывает базу и запрос;
повторная доставка шага использует сохранённый результат по ключу идемпотентности.

## Общий контракт

Имена ниже - интерфейсы агента. Адаптер связывает их с методами API данных.
Для каждого инструмента предусмотрены Markdown-инструкция, JSON Schema аргументов
и обработчик LangChain. База, права, учётные данные, лимиты и deadline
поступают из доверенного контекста backend.

Успех возвращается как `ok + data + sources + warnings + stats`.
Для числовых результатов указываются единица, период/момент, организация,
фактические фильтры, версия определения и время получения данных.
Деньги передаются десятичными строками. Ошибка содержит `code`, `message`
и признак возможности повтора.

## Подключение Python-tools

```python
from tools import ToolContext, create_tools

context = ToolContext(
    request_id=request_id,
    base_id=base_id,
    allowed_metric_ids=frozenset(metric_ids),
    allowed_organization_ids=frozenset(organization_ids),
    allowed_entity_names=frozenset(entity_names),
)
agent_tools = create_tools(analytics_backend, context)

# agent_tools передаются в model.bind_tools(...) и ToolNode(...).
# Отдельный инструмент можно вызвать напрямую:
search = next(tool for tool in agent_tools if tool.name == "search_metrics")
result = await search.ainvoke({"query": "обороты продаж"})
```

Набор создаётся на каждый запрос. Клиент `analytics_backend` реализует
[AnalyticsBackend.call](../../tools/_runtime.py): принимает логическое имя операции,
аргументы, доверенный `ToolContext` и оставшееся время. Его адаптер связывает имена
с HTTP-методами API данных и возвращает `BackendResult` либо безопасный `BackendError`.
Доступы применяются и на стороне API. Для денежных значений сохраняются строки decimal.

`BackendResult` содержит `data`, `sources`, `warnings`, `stats`, `complete`.
Расчётный результат принимается при `complete=True` и непустых `sources`.
Подготовительные вызовы возвращают в `data` список кандидатов либо описание объекта.

Определение метрики в `data` содержит `metric_id`, `approval`, `semantic_version`,
`catalog_version`, `supported_tools`, `time_kind` (`period` или `as_of`),
`allowed_group_by`, `allowed_filters` и `required_filters`. API применяет обязательные
фильтры из определения независимо от переданных пользователем фильтров.

Период передаётся как `{"start": "2025-01-01", "end_exclusive": "2025-02-01"}`.
Момент `as_of` - ISO datetime с явной временной зоной. `limit` рейтинга - от 1 до 100.
Чужие поля, включая URL, базу и изменения прав, отклоняются схемой.

План содержит версии, метрику, организацию и до 8 шагов с конкретными аргументами.
`depends_on` задаёт порядок; ссылки на ещё не полученные значения вместо аргументов
в этот контракт не входят. Проверяются циклы, повторные шаги, права и возможности метрики.
Расчёт допускается только с аргументами принятого шага.

Повтор принятого плана возвращает прежний ID. Повтор и одновременный вызов одного шага
в пределах набора tools используют сохранённый результат. Для перезапусков worker
адаптер API использует долговременный ключ идемпотентности из request ID, операции
и нормализованных аргументов. При восстановлении графа принятый план регистрируется
в новом наборе tools перед продолжением расчётов.

## Отдельные файлы tools

| Tool | Инструкция | Python-обработчик |
|---|---|---|
| `search_metrics` | [search_metrics.md](../../tools/search_metrics.md) | [search_metrics.py](../../tools/search_metrics.py) |
| `get_metric_definition` | [get_metric_definition.md](../../tools/get_metric_definition.md) | [get_metric_definition.py](../../tools/get_metric_definition.py) |
| `get_entity_schema` | [get_entity_schema.md](../../tools/get_entity_schema.md) | [get_entity_schema.py](../../tools/get_entity_schema.py) |
| `resolve_dimension` | [resolve_dimension.md](../../tools/resolve_dimension.md) | [resolve_dimension.py](../../tools/resolve_dimension.py) |
| `submit_query_plan` | [submit_query_plan.md](../../tools/submit_query_plan.md) | [submit_query_plan.py](../../tools/submit_query_plan.py) |
| `get_sales_turnovers` | [get_sales_turnovers.md](../../tools/get_sales_turnovers.md) | [get_sales_turnovers.py](../../tools/get_sales_turnovers.py) |
| `get_top_counterparties` | [get_top_counterparties.md](../../tools/get_top_counterparties.md) | [get_top_counterparties.py](../../tools/get_top_counterparties.py) |
| `get_cash_receipts_by_direction` | [get_cash_receipts_by_direction.md](../../tools/get_cash_receipts_by_direction.md) | [get_cash_receipts_by_direction.py](../../tools/get_cash_receipts_by_direction.py) |
| `get_customer_balances` | [get_customer_balances.md](../../tools/get_customer_balances.md) | [get_customer_balances.py](../../tools/get_customer_balances.py) |

## Правила исполнения

- Подготовительные tools `search_metrics`, `get_metric_definition`, `get_entity_schema`
  и `resolve_dimension` доступны планировщику. `submit_query_plan` фиксирует план.
  Расчётные tools выполняются ToolNode только по принятому плану.
- Независимые шаги допускают ограниченный параллелизм; зависимые выполняются
  после получения входных результатов. Ограничения нагрузки действуют на базу.
- В API данных находятся пагинация, `$expand`, пакетный join, Decimal-агрегации
  и кэш полных промежуточных выгрузок с TTL. При отсутствии `$apply` суммы
  и группировки считаются сервисом данных.
- Относительные даты разрешаются по времени backend и правилам границ метрики.
  Остаток на момент и оборот за период имеют разные параметры.
- Пустая выборка сопровождается предупреждением. Лимит или незавершённая выгрузка
  завершаются понятной ошибкой с предложением сузить запрос.
- Исправления плана, повторы вызовов и генерация LLM расходуют общий бюджет.
  Уточнение сохраняет состояние; при resume доступы и бюджет проверяются заново.
- Ответ содержит точные значения из API, время, организацию, источники, предупреждения
  и объяснение. Сравнение периодов использует проверенные шаги и арифметику кода.
- Аудит фиксирует версии модели, промпта, схем tools, каталога и методологии,
  аргументы шагов, объём данных, время, ошибки и источники.

Основания: §1.3, ФТ-1.2, ФТ-2.1-2.3, ФТ-3.1-3.7 и ФТ-4.1-4.3 ТЗ.
Состав API данных - сообщение Григория.
Технические ориентиры: [LangGraph](https://docs.langchain.com/oss/python/langgraph/graph-api),
[checkpoints](https://docs.langchain.com/oss/python/langgraph/persistence),
[LangChain tools](https://docs.langchain.com/oss/python/langchain/tools),
[Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B),
[DeepSeek-V4-Flash-0731](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-0731).
