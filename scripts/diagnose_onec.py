"""Замеры доступности живой 1С и локального сервиса без сохранения данных базы."""

import argparse
import base64
from datetime import datetime
from getpass import getpass
import ipaddress
import json
import os
from pathlib import Path
import platform
import subprocess
import time
from urllib.parse import quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx
from dotenv import dotenv_values


CURL_FORMAT = json.dumps({
    "http_status": "%{http_code}", "remote_ip": "%{remote_ip}",
    "dns_s": "%{time_namelookup}", "tcp_at_s": "%{time_connect}",
    "tls_at_s": "%{time_appconnect}", "first_byte_at_s": "%{time_starttransfer}",
    "total_s": "%{time_total}",
})
CURL_ERRORS = {
    5: "Не разрешено имя прокси", 6: "Не разрешено имя сервера",
    7: "Не установлено TCP-соединение", 28: "Истекло время ожидания",
    35: "Ошибка установки TLS", 60: "Ошибка проверки сертификата",
}


def now():
    return datetime.now(ZoneInfo("Europe/Moscow")).isoformat(timespec="seconds")


def curl_probe(url, *, authorization, connect_timeout, request_timeout, resolve=None):
    command = [
        "curl", "--silent", "--show-error", "--connect-timeout", str(connect_timeout),
        "--max-time", str(request_timeout), "--output", os.devnull,
        "--write-out", CURL_FORMAT, "--config", "-",
    ]
    if resolve:
        command.extend(["--resolve", resolve])
    command.append(url)
    # Заголовок передаётся через stdin, без пароля в аргументах процесса и отчёте.
    config = f'header = "Authorization: Basic {authorization}"\n' if authorization else ""
    try:
        response = subprocess.run(command, input=config, capture_output=True, text=True,
                                  timeout=request_timeout + 3, check=False)
    except subprocess.TimeoutExpired:
        return {"error": "Превышено время работы curl", "curl_exit_code": None}
    try:
        result = json.loads(response.stdout)
        for key in ("dns_s", "tcp_at_s", "tls_at_s", "first_byte_at_s", "total_s"):
            result[key] = float(result[key])
    except (ValueError, TypeError):
        result = {"error": "curl не вернул ожидаемый формат замеров"}
    result["curl_exit_code"] = response.returncode
    if response.returncode:
        result["error"] = CURL_ERRORS.get(response.returncode, "Ошибка curl")
    if response.returncode == 28:
        if not result.get("tcp_at_s"):
            result["timeout_stage"] = "До завершения TCP: DNS, сеть или прокси"
        elif not result.get("tls_at_s"):
            result["timeout_stage"] = "До завершения TLS"
        elif not result.get("first_byte_at_s"):
            result["timeout_stage"] = "Ожидание HTTP-ответа"
        else:
            result["timeout_stage"] = "Получение тела ответа"
    return result


def local_probe(client, url, path, timeout):
    started = time.monotonic()
    try:
        response = client.get(url.rstrip("/") + path, timeout=timeout)
    except httpx.HTTPError as exc:
        return {"error_type": type(exc).__name__, "total_s": round(time.monotonic() - started, 3)}
    result = {"http_status": str(response.status_code), "total_s": round(time.monotonic() - started, 3)}
    try:
        body = response.json()
    except ValueError:
        return {**result, "body_is_json": False}
    if not isinstance(body, dict):
        return {**result, "body_is_object": False}
    result["request_id"] = response.headers.get("X-Request-ID")
    if path == "/health":
        result["service_version"] = body.get("version", "unknown")
    if path == "/openapi.json":
        schema = body.get("components", {}).get("schemas", {}).get("DataCall", {})
        result["supports_max_rows"] = "max_rows" in schema.get("properties", {})
        result["api_version"] = body.get("info", {}).get("version")
    elif path == "/v1/bootstrap" and response.is_success:
        result["organizations_count"] = len(body.get("organizations", []))
        result["directions_count"] = len(body.get("directions", []))
    if isinstance(body.get("error"), dict):
        result["service_error_code"] = body["error"].get("code")
        details = body["error"].get("details", {})
        if isinstance(details, dict):
            if details.get("stage") in ("connect", "tls", "read", "write", "pool", "network", "http", "deadline", "queue"):
                result["error_stage"] = details["stage"]
            if details.get("error_type") in ("ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "ConnectError", "DeadlineExceeded"):
                result["error_type"] = details["error_type"]
    if response.status_code == 422 and isinstance(body.get("detail"), list):
        result["invalid_fields"] = [item.get("loc") for item in body["detail"] if isinstance(item, dict)]
    return result


def httpx_probe(url, *, authorization, connect_timeout, request_timeout):
    started = time.monotonic()
    phases = []

    def trace(name, _info):
        # Сохраняются только названия этапов; заголовки и данные ответа не записываются.
        if name.startswith("connection.") or "receive_response_headers" in name:
            phases.append({"phase": name, "at_s": round(time.monotonic() - started, 3)})

    headers = {"Authorization": "Basic " + authorization} if authorization else {}
    timeout = httpx.Timeout(request_timeout, connect=connect_timeout)
    try:
        with httpx.Client(headers=headers, timeout=timeout) as client:
            response = client.get(url, extensions={"trace": trace})
        return {"http_status": str(response.status_code), "total_s": round(time.monotonic() - started, 3), "phases": phases}
    except httpx.HTTPError as exc:
        return {"error": type(exc).__name__, "total_s": round(time.monotonic() - started, 3), "phases": phases}


def git_value(*arguments):
    response = subprocess.run(["git", *arguments], capture_output=True, text=True, timeout=5, check=False)
    return response.stdout.strip() if response.returncode == 0 else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempts", type=int, default=5, help="Количество повторов, от 1 до 10")
    parser.add_argument("--interval", type=float, default=5, help="Пауза между повторами, секунды")
    parser.add_argument("--resolve-ip", help="Дополнительный замер $metadata с заданным IP и сохранением проверки HTTPS")
    parser.add_argument("--service-url", default=None)
    parser.add_argument("--connect-timeout", type=float, default=10)
    parser.add_argument("--request-timeout", type=float, default=25)
    parser.add_argument("--no-auth", action="store_true", help="Проверить сеть без авторизации; HTTP 401 ожидаем")
    args = parser.parse_args()
    if not 1 <= args.attempts <= 10 or not 0 <= args.interval <= 60:
        parser.error("Нужны 1–10 повторов и пауза 0–60 секунд")
    if not 0 < args.connect_timeout <= args.request_timeout <= 60:
        parser.error("Нужны 0 < connect-timeout <= request-timeout <= 60")
    values = dotenv_values(Path.cwd() / ".env")

    def setting(key, default=""):
        return os.environ.get(key) or values.get(key) or default

    base_url = setting("ODATA_BASE_URL", "https://176.108.255.199.sslip.io/Itmo/odata/standard.odata").rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error("ODATA_BASE_URL должен быть HTTPS-адресом без учётных данных и параметров")
    service_url = args.service_url or setting("ONEC_DATA_URL", "http://127.0.0.1:8011")
    service = urlsplit(service_url)
    if service.scheme not in {"http", "https"} or not service.hostname or service.username or service.password or service.query or service.fragment:
        parser.error("Адрес сервиса должен быть HTTP(S)-адресом без учётных данных и параметров")
    resolve = None
    if args.resolve_ip:
        try:
            address = ipaddress.ip_address(args.resolve_ip)
        except ValueError:
            parser.error("resolve-ip должен содержать IP-адрес")
        address_text = f"[{address}]" if address.version == 6 else str(address)
        resolve = f"{parsed.hostname}:{parsed.port or 443}:{address_text}"
    authorization = ""
    if not args.no_auth:
        password = setting("ODATA_PASSWORD") or getpass("Пароль OData (не попадёт в отчёт): ")
        if not password:
            parser.error("Пароль OData не введён")
        authorization = base64.b64encode(f"{setting('ODATA_USERNAME', 'odata')}:{password}".encode()).decode()
    folder = Path.cwd() / "runs" / "diagnostics"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"connection-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.jsonl"
    results = []
    with path.open("x", encoding="utf-8") as report:
        def emit(event, **payload):
            record = {"timestamp": now(), "event": event, **payload}
            line = json.dumps(record, ensure_ascii=False)
            print(line, flush=True)
            report.write(line + "\n")
            report.flush()

        emit("environment", python=platform.python_version(), base_url=base_url,
             service_url=service_url, authenticated=bool(authorization), attempts=args.attempts,
             git_commit=git_value("rev-parse", "--short", "HEAD"), git_branch=git_value("branch", "--show-current"),
             connect_timeout_s=args.connect_timeout, request_timeout_s=args.request_timeout,
             proxy_variables_present=[name for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy") if os.environ.get(name)],
             timing_note="dns_s и поля *_at_s — секунды от начала запроса; каждый curl создаёт новое соединение")
        probes = [("metadata", base_url + "/$metadata", None)]
        if resolve:
            probes.append(("metadata_fixed_ip", base_url + "/$metadata", resolve))
        query = urlencode({"$format": "json", "$select": "Ref_Key", "$top": 1}, quote_via=quote)
        probes.append(("organization_one_row", base_url + "/" + quote("Catalog_Организации") + "?" + query, None))
        for attempt in range(1, args.attempts + 1):
            for label, url, fixed_ip in probes:
                result = curl_probe(url, authorization=authorization, connect_timeout=args.connect_timeout,
                                    request_timeout=args.request_timeout, resolve=fixed_ip)
                emit("upstream", attempt=attempt, target=label, **result)
                results.append(result)
                time.sleep(0.6)
            result = httpx_probe(base_url + "/$metadata", authorization=authorization,
                                 connect_timeout=args.connect_timeout, request_timeout=args.request_timeout)
            emit("upstream", attempt=attempt, target="metadata_httpx", **result)
            results.append(result)
            if attempt < args.attempts:
                time.sleep(args.interval)
        token = "" if args.no_auth else setting("ONEC_DATA_TOKEN")
        endpoints = (("health", "/health", 5),) if args.no_auth else (
            ("health", "/health", 5), ("contract", "/openapi.json", 5), ("bootstrap", "/v1/bootstrap", 65))
        with httpx.Client(headers={"X-Internal-Token": token} if token else {}) as client:
            for label, endpoint, timeout in endpoints:
                emit("local_service", target=label, **local_probe(client, service_url, endpoint, timeout))
        if args.no_auth:
            emit("skipped", target="bootstrap", reason="Режим no-auth: обращения через сервисную учётную запись отключены")
        emit("summary", upstream_requests=len(results), network_failures=sum(bool(item.get("error")) for item in results),
             upstream_http_statuses=sorted({item.get("http_status", "unknown") for item in results}), report_path=str(path))
    print(f"Отчёт сохранён: {path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Замеры остановлены; завершённые записи сохранены.")
        raise SystemExit(130)
