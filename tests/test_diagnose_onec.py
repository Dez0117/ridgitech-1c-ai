from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import diagnose_onec


class DiagnoseOneCTests(unittest.TestCase):
    def test_no_auth_does_not_use_internal_token_or_call_bootstrap(self):
        for no_auth in (True, False):
            with self.subTest(no_auth=no_auth), TemporaryDirectory() as folder:
                arguments = ["diagnose_onec", "--attempts", "1", "--interval", "0", *(["--no-auth"] if no_auth else [])]
                output = io.StringIO()
                values = {"ODATA_PASSWORD": "sensitive-password", "ONEC_DATA_TOKEN": "sensitive-token"}
                with (
                    patch("sys.argv", arguments), patch.dict(os.environ, {}, clear=True),
                    patch.object(diagnose_onec.Path, "cwd", return_value=Path(folder)),
                    patch.object(diagnose_onec, "dotenv_values", return_value=values),
                    patch.object(diagnose_onec, "getpass") as prompt,
                    patch.object(diagnose_onec, "git_value", return_value="test"),
                    patch.object(diagnose_onec.time, "sleep"),
                    patch.object(diagnose_onec, "curl_probe", return_value={"http_status": "200"}) as direct,
                    patch.object(diagnose_onec, "httpx_probe", return_value={"http_status": "200"}) as python,
                    patch.object(diagnose_onec.httpx, "Client") as client,
                    patch.object(diagnose_onec, "local_probe", return_value={"http_status": "200"}) as local,
                    redirect_stdout(output),
                ):
                    diagnose_onec.main()
                paths = [call.args[2] for call in local.call_args_list]
                self.assertEqual(paths, ["/health"] if no_auth else ["/health", "/openapi.json", "/v1/bootstrap"])
                self.assertEqual(client.call_args.kwargs["headers"], {} if no_auth else {"X-Internal-Token": "sensitive-token"})
                self.assertTrue(all(bool(call.kwargs["authorization"]) is not no_auth for call in direct.call_args_list))
                self.assertEqual(bool(python.call_args.kwargs["authorization"]), not no_auth)
                prompt.assert_not_called()
                report = next((Path(folder) / "runs" / "diagnostics").glob("connection-*.jsonl"))
                records = [json.loads(line) for line in report.read_text().splitlines()]
                self.assertTrue(any(item["event"] == "summary" for item in records))
                self.assertNotIn("sensitive-password", output.getvalue() + report.read_text())
                self.assertNotIn("sensitive-token", output.getvalue() + report.read_text())
