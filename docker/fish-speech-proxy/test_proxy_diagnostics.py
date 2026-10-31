import re
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("diagnose_validation_proxy.ps1")


class ProxyDiagnosticScriptTests(unittest.TestCase):
    def test_default_execution_is_dry_run(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("if (-not $Execute)", source)
        self.assertIn("No request sent", source)

    def test_only_validation_port_is_targeted(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("ValidateSet(18000, 18001, 18002)", source)
        self.assertIn('127.0.0.1:$Port/v1/audio/speech', source)
        self.assertNotIn("127.0.0.1:8000/", source)
        self.assertNotIn("127.0.0.1:8080/", source)

    def test_port_18002_uses_the_existing_request_path(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("18002", source)
        self.assertEqual(source.count('127.0.0.1:$Port/v1/audio/speech'), 1)
        for field in ("model", "voice", "input", "response_format"):
            self.assertIn(field, source)

    def test_dry_run_exits_before_http_client_creation(self):
        source = SCRIPT.read_text(encoding="utf-8")
        dry_run = source.index("if (-not $Execute)")
        dry_run_exit = source.index("exit 0", dry_run)
        client_assignment = re.search(r"(?m)^\s*\$client\s*=", source)
        self.assertIsNotNone(client_assignment)
        client_creation = client_assignment.start()
        send = source.index("$client.SendAsync(")
        self.assertLess(dry_run, dry_run_exit)
        self.assertLess(dry_run_exit, client_creation)
        self.assertLess(client_creation, send)

    def test_execute_path_contains_exactly_one_send_and_no_retry(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(source.count("$client.SendAsync("), 1)
        self.assertNotIn("Retry", source)
        self.assertNotIn("while (", source)

    def test_script_never_reads_or_prints_response_body(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("ResponseHeadersRead", source)
        self.assertNotIn("ReadAsString", source)
        self.assertNotIn("ReadAsByte", source)
        self.assertNotIn("Authorization", source)
        self.assertNotIn("FISH_API_KEY", source)

    def test_safe_output_fields_only(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for field in ("proxy_status", "upstream_status", "classification", "elapsed_ms"):
            self.assertIn(field, source)
        for forbidden in ("response_body", "headers =", "audio =", "credential"):
            self.assertNotIn(forbidden, source)

    def test_request_fields_match_proxy_schema(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for field in ("model", "input", "voice", "response_format"):
            self.assertIn(field, source)

    def test_powershell_51_loads_system_net_http(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("Add-Type -AssemblyName System.Net.Http", source)
        self.assertNotIn("::new(", source)

    def test_local_and_upstream_422_are_distinct(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("local-validation-failed", source)
        self.assertIn("upstream-validation-failed", source)


if __name__ == "__main__":
    unittest.main()
