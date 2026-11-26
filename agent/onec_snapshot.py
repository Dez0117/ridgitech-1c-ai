"""Воспроизведение ограниченной выгрузки OData через сервис данных 1С и HTTP-адаптер."""

import hashlib
import json
import re
from pathlib import Path

import httpx

from backend.integrations.onec_data_backend import OneCDataBackend
from services.onec_data.client import ODataReader
from services.onec_data.config import DataSettings
from services.onec_data.main import create_app
from services.onec_data.models import CATALOGS, MOVEMENTS
from services.onec_data.service import OneCDataService


class SnapshotUpstream:
    def __init__(self, folder):
        folder = Path(folder)
        self.data = json.loads((folder / "snapshot.json").read_text(encoding="utf-8"))
        self.metadata = (folder / "metadata.xml").read_bytes()
        if hashlib.sha256(self.metadata).hexdigest() != self.data["metadata_sha256"]:
            raise ValueError("Snapshot metadata hash mismatch")
        self.fingerprint = hashlib.sha256(json.dumps(self.data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.methods = []

    def __call__(self, request):
        if request.method != "GET" or request.url.host != "snapshot.invalid":
            raise ValueError("Only local snapshot GETs are permitted")
        self.methods.append(request.method)
        entity = request.url.path.rsplit("/", 1)[-1]
        if entity == "$metadata":
            return httpx.Response(200, content=self.metadata, headers={"DataServiceVersion": "3.0"})
        params = request.url.params
        predicate = params.get("$filter", "")
        if entity in {CATALOGS["organization"], CATALOGS["direction"]}:
            if predicate != "DeletionMark eq false":
                raise ValueError("Unexpected catalog predicate")
            kind = next(kind for kind, name in CATALOGS.items() if name == entity)
            rows = [{"Ref_Key": row["id"], "Description": row["name"], "DeletionMark": False}
                    for row in self.data["dimensions"][kind]]
        elif entity == MOVEMENTS:
            match = re.fullmatch(
                r"Period ge datetime'([^']+)' and Period lt datetime'([^']+)' "
                r"and Организация_Key eq guid'([^']+)' and НаправлениеДеятельности_Key eq guid'([^']+)' "
                r"and Active eq true and ВидДвиженияДС eq 'Поступление'", predicate)
            if not match:
                raise ValueError("Unexpected movement predicate; snapshot replay cannot invent a query result")
            start, end, organization, direction = match.groups()
            if not any(left <= start[:10] < end[:10] <= right for left, right in self.data["periods"]):
                raise ValueError("Query is outside captured periods")
            rows = [row for row in self.data["records"] if start <= row["Period"] < end
                    and row["Организация_Key"] == organization and row["НаправлениеДеятельности_Key"] == direction
                    and row["Active"] is True and row["ВидДвиженияДС"] == "Поступление"]
        else:
            raise ValueError("Entity is not captured")
        rows = sorted(rows, key=lambda row: tuple(row[key] for key in params["$orderby"].split(",")))
        skip, top = int(params["$skip"]), int(params["$top"])
        fields = params["$select"].split(",")
        return httpx.Response(200, json={"value": [{key: row[key] for key in fields} for row in rows[skip:skip + top]]})


class OneCSnapshotBackend(OneCDataBackend):
    def __init__(self, folder):
        self.upstream = SnapshotUpstream(folder)
        self.fingerprint = self.upstream.fingerprint
        settings = DataSettings(_env_file=None, odata_base_url="https://snapshot.invalid/odata",
            odata_username="snapshot", odata_password="unused-local-transport", onec_data_token="",
            onec_base_id=self.upstream.data["base_id"], odata_requests_per_second=50, onec_organization_ids="",
            onec_metadata_refresh_seconds=0, onec_metadata_state_dir=None)
        self.reader = ODataReader(settings, transport=httpx.MockTransport(self.upstream))
        service = OneCDataService(self.reader)
        app = create_app(service)
        app.state.service = service
        super().__init__("http://data.snapshot", transport=httpx.ASGITransport(app=app))

    async def close(self):
        await super().close()
        await self.reader.close()
