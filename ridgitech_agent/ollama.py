import json
from urllib.error import URLError
from urllib.request import Request, urlopen

from pydantic import ValidationError

from .agent import ModelTurn, ToolCall
from .models import AgentError


class OllamaModel:
    """Optional local model adapter; the model name is a backend setting."""

    def __init__(self, model: str):
        self.model = model

    def complete(self, messages, tools, *, timeout):
        request = Request("http://127.0.0.1:11434/api/chat", headers={"Content-Type": "application/json"},
            data=json.dumps({"model": self.model, "messages": messages, "tools": tools,
                             "stream": False}, ensure_ascii=False).encode())
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise AgentError("MODEL_PROTOCOL", "Ответ модели превышает допустимый размер.")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("done") is not True:
                raise AgentError("MODEL_PROTOCOL", "Модель не завершила ответ.")
            message = payload["message"]
            if not isinstance(message, dict) or not (message.get("content") or message.get("tool_calls")):
                raise AgentError("MODEL_PROTOCOL", "Модель вернула пустой ответ.")
            return ModelTurn(content=message.get("content", ""), tool_calls=[
                ToolCall(name=c["function"]["name"], arguments=c["function"]["arguments"])
                for c in message.get("tool_calls", [])])
        except URLError:
            raise AgentError("MODEL_UNAVAILABLE", "Локальная модель Ollama недоступна или не поддерживает tools.")
        except (ValueError, KeyError, TypeError, ValidationError):
            raise AgentError("MODEL_PROTOCOL", "Некорректный формат ответа модели.")
