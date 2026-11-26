"""Предыдущая схема и журнал изменений без строк базы и учётных данных."""

from datetime import datetime, timezone
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from tools import BackendError


def schema_diff(before, after):
    added, removed = after.keys() - before.keys(), before.keys() - after.keys()
    changed = {}
    for name in sorted(before.keys() & after.keys()):
        old, new = before[name], after[name]
        left, right = old["fields"], new["fields"]
        delta = {
            "fields_added": {field: right[field] for field in sorted(right.keys() - left.keys())},
            "fields_removed": {field: left[field] for field in sorted(left.keys() - right.keys())},
            "fields_changed": {field: {"before": left[field], "after": right[field]}
                               for field in sorted(left.keys() & right.keys()) if left[field] != right[field]},
        }
        if old["keys"] != new["keys"]:
            delta["keys_changed"] = {"before": old["keys"], "after": new["keys"]}
        if old.get("navigation", {}) != new.get("navigation", {}):
            delta["navigation_changed"] = {"before": old.get("navigation", {}), "after": new.get("navigation", {})}
        if any(delta.values()):
            changed[name] = delta
    return {"entities_added": {name: after[name] for name in sorted(added)},
            "entities_removed": {name: before[name] for name in sorted(removed)}, "entities_changed": changed}


class MetadataState:
    def __init__(self, settings):
        self.folder = settings.onec_metadata_state_dir
        identity = "\0".join((settings.onec_base_id, settings.odata_base_url, settings.odata_username))
        self.key = hashlib.sha256(identity.encode()).hexdigest()[:24]
        self.path = Path(self.folder) / f"metadata-{self.key}.json" if self.folder is not None else None

    def load(self):
        if self.path is None:
            return None
        try:
            if not self.path.exists():
                return None
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("format_version") != 1 or not isinstance(data.get("schema"), dict) or not isinstance(data.get("version"), str) or type(data.get("accepted")) is not bool:
                raise ValueError
            for name, entity in data["schema"].items():
                if not isinstance(name, str) or not isinstance(entity, dict) or not isinstance(entity.get("fields"), dict) or not isinstance(entity.get("keys"), list):
                    raise ValueError
                if any(not isinstance(key, str) or not isinstance(kind, str) for key, kind in entity["fields"].items()) or any(not isinstance(key, str) for key in entity["keys"]):
                    raise ValueError
            return data
        except (OSError, ValueError, TypeError):
            raise BackendError("METADATA_STATE_ERROR", "Не удалось прочитать сохранённую схему 1С; проверьте каталог состояния.") from None

    def record(self, schema, version, accepted, event):
        if self.path is None:
            return
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            document = {"format_version": 1, "saved_at": datetime.now(timezone.utc).isoformat(),
                        "schema": schema, "version": version, "accepted": accepted}
            # Сначала фиксируется diff: сбой до замены базовой схемы не потеряет запись об изменении.
            with self.path.with_suffix(".changes.jsonl").open("a", encoding="utf-8") as audit:
                audit.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                audit.flush()
                os.fsync(audit.fileno())
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as target:
                temporary = Path(target.name)
                json.dump(document, target, ensure_ascii=False, sort_keys=True)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise BackendError("METADATA_STATE_ERROR", "Не удалось сохранить схему и журнал изменений 1С; проверьте каталог состояния.") from None
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
