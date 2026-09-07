import json
import os
import runpy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-web"


def load_web(extra=""):
    config = """\
[web]
address=127.0.0.1
port=9443

[tls]
certificate=/nonexistent/test.crt
private_key=/nonexistent/test.key

[auth]
access_mode=administrator
session_idle_seconds=1800

[health]
refresh_interval_seconds=30
collector_timeout_seconds=120
""" + extra
    tmp = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
    try:
        tmp.write(config)
        tmp.close()
        with mock.patch.dict(
            os.environ, {"SHAREDLVMTHIN_WEB_CONFIG": tmp.name}, clear=False
        ):
            return runpy.run_path(str(WEB), run_name="sharedlvmthin_web_test")
    finally:
        os.unlink(tmp.name)


class WebConfigurationTests(unittest.TestCase):
    def test_session_idle_is_loaded_from_configuration(self):
        web = load_web("\n[override]\nunused=1\n")
        self.assertEqual(web["IDLE"], 1800)

    def test_custom_session_and_health_values_are_loaded(self):
        config = """\
[web]
address=127.0.0.1
port=9443
[tls]
certificate=/test.crt
private_key=/test.key
[auth]
access_mode=administrator
session_idle_seconds=73
[health]
refresh_interval_seconds=7
collector_timeout_seconds=11
"""
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            f.write(config)
            name = f.name
        try:
            with mock.patch.dict(
                os.environ, {"SHAREDLVMTHIN_WEB_CONFIG": name}, clear=False
            ):
                web = runpy.run_path(str(WEB), run_name="sharedlvmthin_web_test")
            self.assertEqual(web["IDLE"], 73)
            self.assertEqual(web["HEALTH_REFRESH"], 7)
            self.assertEqual(web["COLLECTOR_TIMEOUT"], 11)
        finally:
            os.unlink(name)

    def test_invalid_session_idle_is_rejected(self):
        config = """\
[web]
address=127.0.0.1
port=9443
[tls]
certificate=/test.crt
private_key=/test.key
[auth]
access_mode=administrator
session_idle_seconds=0
"""
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            f.write(config)
            name = f.name
        try:
            with mock.patch.dict(
                os.environ, {"SHAREDLVMTHIN_WEB_CONFIG": name}, clear=False
            ):
                with self.assertRaisesRegex(RuntimeError, "between 60 and 86400"):
                    runpy.run_path(str(WEB), run_name="sharedlvmthin_web_test")
        finally:
            os.unlink(name)

    def test_login_cookie_has_no_fixed_max_age(self):
        web = load_web()
        cookie = web["session_cookie"]("token")
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn("Max-Age", cookie)

    def test_authenticated_activity_slides_server_idle_timeout(self):
        web = load_web()
        web["sessions"]["token"] = {
            "user": "admin@pve", "auth": "pve-rbac", "seen": 100.0
        }
        headers = {"Cookie": "SLTSESSION=token"}
        with mock.patch.object(web["time"], "monotonic", return_value=150.0):
            token, session = web["session_from"](headers)
        self.assertEqual(token, "token")
        self.assertEqual(session["user"], "admin@pve")
        self.assertEqual(web["sessions"]["token"]["seen"], 150.0)

        with mock.patch.object(
            web["time"], "monotonic", return_value=150.0 + web["IDLE"] + 1
        ):
            token, session = web["session_from"](headers)
        self.assertIsNone(token)
        self.assertIsNone(session)

    def test_health_failure_does_not_remove_authenticated_session(self):
        web = load_web()
        web["sessions"]["token"] = {
            "user": "admin@pve", "auth": "pve-rbac", "seen": 100.0
        }
        runner = mock.Mock(
            return_value=SimpleNamespace(
                returncode=1, stdout="", stderr="collector failed"
            )
        )
        cache = web["HealthCache"]("collector", 30, 10, runner=runner)
        self.assertFalse(cache.refresh())
        self.assertIn("token", web["sessions"])

    def test_administrator_authorization_uses_effective_pve_permissions(self):
        web = load_web()
        globals_ = web["authorized"].__globals__
        globals_["ACCESS_MODE"] = "administrator"
        globals_["pve"] = mock.Mock(
            return_value={"/": {name: 1 for name in web["REQUIRED"]}}
        )
        allowed, reason = web["authorized"]("admin@pve", "ticket")
        self.assertTrue(allowed)
        self.assertEqual(reason, "pve-rbac")

    def test_group_authorization_requires_actual_membership(self):
        web = load_web()
        globals_ = web["authorized"].__globals__
        globals_["ACCESS_MODE"] = "group"
        globals_["ACCESS_GROUP"] = "storage-auditors"
        globals_["pve"] = mock.Mock(
            return_value=[
                {"groupid": "storage-auditors", "users": "alice@pve,bob@pam"}
            ]
        )
        self.assertTrue(web["authorized"]("alice@pve", "ticket")[0])
        self.assertFalse(web["authorized"]("mallory@pve", "ticket")[0])


class HealthCacheTests(unittest.TestCase):
    def setUp(self):
        self.web = load_web()
        self.HealthCache = self.web["HealthCache"]

    def test_successful_refresh_returns_cached_json_and_metadata(self):
        runner = mock.Mock(
            return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps({"result": "PASS"}), stderr=""
            )
        )
        cache = self.HealthCache("collector", 30, 10, runner=runner)
        self.assertTrue(cache.refresh())
        snapshot = cache.snapshot()
        self.assertEqual(snapshot["result"], "PASS")
        self.assertIsNotNone(snapshot["cache"]["generated_at"])
        self.assertIsNotNone(snapshot["cache"]["last_success"])
        self.assertIsNone(snapshot["cache"]["last_error"])

    def test_failed_refresh_preserves_last_good_snapshot(self):
        results = [
            SimpleNamespace(
                returncode=0, stdout=json.dumps({"result": "PASS", "value": 1}), stderr=""
            ),
            SimpleNamespace(returncode=1, stdout="", stderr="backend unavailable"),
        ]
        cache = self.HealthCache("collector", 30, 10, runner=lambda *a, **k: results.pop(0))
        self.assertTrue(cache.refresh())
        self.assertFalse(cache.refresh())
        snapshot = cache.snapshot()
        self.assertEqual(snapshot["value"], 1)
        self.assertEqual(snapshot["cache"]["last_error"], "backend unavailable")

    def test_only_one_refresh_can_run(self):
        entered = threading.Event()
        release = threading.Event()

        def runner(*args, **kwargs):
            entered.set()
            release.wait(2)
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"result": "PASS"}), stderr=""
            )

        cache = self.HealthCache("collector", 30, 10, runner=runner)
        worker = threading.Thread(target=cache.refresh)
        worker.start()
        self.assertTrue(entered.wait(1))
        self.assertFalse(cache.refresh())
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_cached_monitoring_preserves_100_500_1000_pool_snapshots(self):
        for count in (100, 500, 1000):
            payload = {
                "result": "PASS",
                "storages": [{
                    "id": "scale-test",
                    "pools": [
                        {
                            "name": f"sltp-{900000 + i}",
                            "owned": True,
                            "data_percent": float(i % 80),
                            "metadata_percent": float(i % 40),
                        }
                        for i in range(count)
                    ],
                }],
            }
            runner = mock.Mock(
                return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps(payload), stderr=""
                )
            )
            cache = self.HealthCache("collector", 30, 10, runner=runner)
            self.assertTrue(cache.refresh())
            for _ in range(10):
                snapshot = cache.snapshot()
                self.assertEqual(
                    len(snapshot["storages"][0]["pools"]), count
                )
            self.assertEqual(
                runner.call_count, 1,
                "dashboard reads must not rerun the collector",
            )

            cache.runner = mock.Mock(
                return_value=SimpleNamespace(
                    returncode=1, stdout="", stderr="scale backend unavailable"
                )
            )
            self.assertFalse(cache.refresh())
            snapshot = cache.snapshot()
            self.assertEqual(len(snapshot["storages"][0]["pools"]), count)
            self.assertEqual(
                snapshot["cache"]["last_error"], "scale backend unavailable"
            )


if __name__ == "__main__":
    unittest.main()
