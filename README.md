# ridgitech-1c-ai

Архитектура AI-агента для аналитики данных 1С и базовые LangChain tools.

- [Картинка архитектуры](docs/agent/architecture.png)
- [Редактируемая схема SVG](docs/agent/architecture.svg)
- [Tools - отдельные Python-файлы и Markdown-инструкции](tools/)
- [Описание, подключение, стек и варианты LLM](docs/agent/tools.md)

Каждый tool содержит `.py` с типизированными аргументами и обработчиком,
а также `.md` с инструкцией для модели. [create_tools](tools/__init__.py) создаёт
вызываемые LangChain-инструменты для `bind_tools` и LangGraph `ToolNode`.

Для установки и проверки (Python 3.12+):

```bash
python3 -m pip install -e '.[test]'
python3 -m unittest discover -s tests -v
```

Полный набор материалов, учебный прототип и тесты сохранены в ветке
[draft/agent-prototype](https://github.com/Dez0117/ridgitech-1c-ai/tree/draft/agent-prototype).
