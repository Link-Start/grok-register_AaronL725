#!/usr/bin/env python3
from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, got {count}")
    return text.replace(old, new, 1)


def replace_between(text, start, end, replacement, label):
    i = text.find(start)
    if i < 0:
        raise RuntimeError(f"{label}: start not found")
    j = text.find(end, i)
    if j < 0:
        raise RuntimeError(f"{label}: end not found")
    return text[:i] + replacement + text[j:]

# 1) Shared registration flow boundaries and result states.
flow_path = ROOT / "registration_flow.py"
flow = flow_path.read_text(encoding="utf-8")
flow = replace_once(
    flow,
    "@dataclass\nclass BatchResult:\n    success_count: int = 0\n    fail_count: int = 0\n    processed_count: int = 0\n    cancelled: bool = False\n    results: list = field(default_factory=list)\n",
    "@dataclass\nclass RegistrationSettings:\n    count: int\n    enable_nsfw: bool = True\n    max_mail_retry: int = 3\n    max_slot_retry: int = 3\n    cleanup_interval: int = 5\n\n\n@dataclass\nclass BatchResult:\n    success_count: int = 0\n    fail_count: int = 0\n    processed_count: int = 0\n    registered_unsaved_count: int = 0\n    postprocess_warning_count: int = 0\n    cancelled: bool = False\n    results: list = field(default_factory=list)\n",
    "settings and batch states",
)
new_run_batch = r'''def _notify_observer(observer, result, account, output, callbacks):
    try:
        observer(result, account, output)
    except Exception as exc:
        callbacks.log(f"[Debug] observer 执行失败: {exc}")


def run_batch(count, callbacks, observer, ops, enable_nsfw=True, cleanup_interval=5,
              max_slot_retry=3, max_mail_retry=3):
    settings = RegistrationSettings(
        count=int(count),
        enable_nsfw=bool(enable_nsfw),
        cleanup_interval=int(cleanup_interval),
        max_slot_retry=int(max_slot_retry),
        max_mail_retry=int(max_mail_retry),
    )
    result = BatchResult()
    retry_count_for_slot = 0
    last_cleanup_success_count = 0
    try:
        ops.start_browser()
        callbacks.log("[*] 浏览器已启动")
        while result.processed_count < settings.count:
            if callbacks.cancelled():
                result.cancelled = True
                break
            callbacks.log(f"--- 开始第 {result.processed_count + 1}/{settings.count} 个账号 ---")
            account = None
            output = None
            try:
                account = register_one_account(
                    callbacks,
                    ops,
                    enable_nsfw=settings.enable_nsfw,
                    max_mail_retry=settings.max_mail_retry,
                )
                output = persist_account_result(account, callbacks, ops)
                result.results.append({"registration": account, "output": output})
                retry_count_for_slot = 0
                result.processed_count += 1
                if output.saved:
                    result.success_count += 1
                    callbacks.log(f"[+] 注册并保存成功: {account.email}")
                    if (
                        settings.cleanup_interval > 0
                        and result.success_count % settings.cleanup_interval == 0
                        and result.success_count != last_cleanup_success_count
                        and result.processed_count < settings.count
                    ):
                        ops.cleanup(f"已成功 {result.success_count} 个账号，执行定期清理")
                        last_cleanup_success_count = result.success_count
                else:
                    result.fail_count += 1
                    result.registered_unsaved_count += 1
                    callbacks.log(f"[-] 注册成功但持久化未完成: {account.email}")
                pool_warning = any(
                    state.get("enabled") and not state.get("ok")
                    for state in output.pools.values()
                )
                cpa_warning = bool(output.cpa and not output.cpa.get("ok") and not output.cpa.get("skipped"))
                if pool_warning or cpa_warning:
                    result.postprocess_warning_count += 1
            except ops.cancelled_exception:
                result.cancelled = True
                callbacks.log("[!] 注册被停止")
                break
            except ops.retry_exception as exc:
                retry_count_for_slot += 1
                if retry_count_for_slot <= settings.max_slot_retry:
                    callbacks.log(
                        f"[!] 当前账号流程卡住，重试第 {retry_count_for_slot}/{settings.max_slot_retry} 次: {exc}"
                    )
                else:
                    result.fail_count += 1
                    result.processed_count += 1
                    retry_count_for_slot = 0
                    callbacks.log(f"[-] 当前账号已达到最大重试次数，跳过: {exc}")
            except Exception as exc:
                result.fail_count += 1
                result.processed_count += 1
                retry_count_for_slot = 0
                callbacks.log(f"[-] 注册失败: {exc}")
            finally:
                _notify_observer(observer, result, account, output, callbacks)
                if callbacks.cancelled():
                    result.cancelled = True
                    break
                if result.processed_count < settings.count:
                    if ops.browser_missing():
                        ops.start_browser()
                    else:
                        ops.restart_browser()
                    ops.sleep(1)
    finally:
        ops.cleanup("任务结束")
    return result
'''
flow = replace_between(flow, "def run_batch(", "\n    return result\n", new_run_batch, "run_batch")
# remove the old trailing return consumed only through marker
if flow.endswith("\n    return result\n"):
    flow = flow[:-len("\n    return result\n")] + "\n"
ast.parse(flow)
flow_path.write_text(flow, encoding="utf-8")

# 2) Main config dependencies and retry-pending CLI.
main_path = ROOT / "grok_register_ttk.py"
main = main_path.read_text(encoding="utf-8")
cross_field = r'''    provider = cfg["email_provider"]
    if provider == "cloudflare" and not cfg["cloudflare_api_base"]:
        raise ConfigError("Cloudflare 模式需要配置 cloudflare_api_base")
    if provider == "cloudmail":
        missing = [
            key for key in ("cloudmail_api_base", "cloudmail_public_token", "cloudmail_domains")
            if not cfg[key]
        ]
        if missing:
            raise ConfigError("Cloud Mail 模式缺少必需配置: " + ", ".join(missing))
    if cfg["grok2api_auto_add_remote"]:
        missing = [
            key for key in ("grok2api_remote_base", "grok2api_remote_app_key")
            if not cfg[key]
        ]
        if missing:
            raise ConfigError("远端 token 入池缺少必需配置: " + ", ".join(missing))
    if cfg["cpa_copy_to_hotload"] and not cfg["cpa_hotload_dir"]:
        raise ConfigError("启用 CPA 热加载复制时必须配置 cpa_hotload_dir")

'''
main = replace_once(
    main,
    "    for key in path_keys:\n        value = cfg[key]\n        if value.startswith(\"~\"):\n            cfg[key] = os.path.expanduser(value)\n    return cfg\n",
    cross_field + "    for key in path_keys:\n        value = cfg[key]\n        if value.startswith(\"~\"):\n            cfg[key] = os.path.expanduser(value)\n    return cfg\n",
    "cross-field config validation",
)
retry_pending = r'''def retry_pending_file(pending_path, output_path=None, log_callback=None):
    logger = log_callback or (lambda message: None)
    pending_path = os.path.abspath(os.path.expanduser(str(pending_path)))
    if not os.path.isfile(pending_path):
        raise FileNotFoundError(f"pending 文件不存在: {pending_path}")
    suffix = ".pending.jsonl"
    if output_path:
        target_path = os.path.abspath(os.path.expanduser(str(output_path)))
    elif pending_path.endswith(suffix):
        target_path = pending_path[:-len(suffix)]
    else:
        target_path = pending_path + ".recovered.txt"
    unresolved = []
    restored = 0
    with open(pending_path, "r", encoding="utf-8") as handle:
        lines = handle.readlines()
    for line_number, raw_line in enumerate(lines, 1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
            if not isinstance(record, dict):
                raise ValueError("record must be a JSON object")
            email = str(record.get("email") or "").strip()
            password = str(record.get("password") or "")
            sso = str(record.get("sso") or "").strip()
            if not email or not sso:
                raise ValueError("record missing email or sso")
            _append_account_line(target_path, email, password, sso)
            restored += 1
            logger(f"[+] 已恢复 pending 账号: {email}")
        except Exception as exc:
            unresolved.append(raw_line if raw_line.endswith("\n") else raw_line + "\n")
            logger(f"[!] pending 第 {line_number} 行恢复失败: {exc}")
    directory = os.path.dirname(pending_path) or "."
    fd, temp_path = tempfile.mkstemp(prefix=".pending-retry-", suffix=".jsonl.tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.writelines(unresolved)
            handle.flush()
            os.fsync(handle.fileno())
        if unresolved:
            os.replace(temp_path, pending_path)
            temp_path = None
            try:
                os.chmod(pending_path, 0o600)
            except Exception:
                pass
        else:
            os.unlink(temp_path)
            temp_path = None
            os.unlink(pending_path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
    return {"restored": restored, "remaining": len(unresolved), "output_path": target_path}


'''
main = replace_once(main, "def run_registration_common(count, log_callback, cancel_callback, accounts_output_file, observer):", retry_pending + "def run_registration_common(count, log_callback, cancel_callback, accounts_output_file, observer):", "retry pending helper")
main = replace_once(
    main,
    "def main():\n    if len(sys.argv) > 1 and sys.argv[1].strip().lower() in (\"start\", \"cli\", \"--cli\"):\n",
    "def main():\n    if len(sys.argv) > 1 and sys.argv[1].strip().lower() == \"retry-pending\":\n        if len(sys.argv) < 3:\n            print(\"用法: python grok_register_ttk.py retry-pending <pending文件> [输出文件]\", file=sys.stderr)\n            return\n        try:\n            summary = retry_pending_file(\n                sys.argv[2],\n                output_path=sys.argv[3] if len(sys.argv) > 3 else None,\n                log_callback=cli_log,\n            )\n            cli_log(\n                f\"[*] pending 恢复完成: 已恢复 {summary['restored']} | 剩余 {summary['remaining']} | 输出 {summary['output_path']}\"\n            )\n        except Exception as exc:\n            log_exception(\"pending 恢复失败\", exc, cli_log)\n        return\n    if len(sys.argv) > 1 and sys.argv[1].strip().lower() in (\"start\", \"cli\", \"--cli\"):\n",
    "retry pending dispatch",
)
ast.parse(main)
main_path.write_text(main, encoding="utf-8")

# 3) CPA browser/proxy bridge cleanup boundary.
browser_path = ROOT / "cpa_xai" / "browser_confirm.py"
browser = browser_path.read_text(encoding="utf-8")
browser = replace_once(
    browser,
    "    except Exception:\n        if browser is not None:\n            close_standalone(browser)\n        elif proxy_bridge is not None:\n            try:\n                proxy_bridge.stop()\n            except Exception:\n                pass\n        raise\n",
    "    except Exception:\n        if browser is not None:\n            close_standalone(browser)\n        if proxy_bridge is not None:\n            try:\n                proxy_bridge.stop()\n            except Exception:\n                pass\n        raise\n",
    "proxy bridge cleanup",
)
ast.parse(browser)
browser_path.write_text(browser, encoding="utf-8")

# 4) Remote token pool tests aligned with ETag/legacy policy.
remote_tests = r'''import unittest
from unittest.mock import patch

import grok_register_ttk as app


class DummyResponse:
    def __init__(self, payload=None, status_code=200, reason="", headers=None, text=""):
        self._payload = payload or {}
        self.status_code = status_code
        self.reason = reason
        self.headers = headers or {}
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP Error {self.status_code}: {self.reason}")

    def json(self):
        return self._payload


class Grok2ApiRemotePoolTests(unittest.TestCase):
    def setUp(self):
        self.original_config = app.config.copy()

    def tearDown(self):
        app.config = self.original_config

    def _configure(self, **overrides):
        app.config.update({
            "grok2api_remote_base": "https://grok.example.com",
            "grok2api_remote_app_key": "app-secret",
            "grok2api_pool_name": "ssoBasic",
            "grok2api_allow_legacy_full_save": False,
            **overrides,
        })

    def test_remote_pool_falls_back_to_admin_api_prefix_when_root_tokens_add_is_404(self):
        self._configure()
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            if url == "https://grok.example.com/tokens/add":
                return DummyResponse(status_code=404)
            return DummyResponse({"status": "success", "count": 1})

        with patch.object(app, "http_post", side_effect=fake_post):
            ok = app.add_token_to_grok2api_remote_pool("sso=abc123", email="a@example.com")

        self.assertTrue(ok)
        self.assertEqual([url for url, _ in calls], [
            "https://grok.example.com/tokens/add",
            "https://grok.example.com/admin/api/tokens/add",
        ])
        self.assertEqual(calls[-1][1]["params"], {"app_key": "app-secret"})
        self.assertEqual(calls[-1][1]["json"], {
            "tokens": ["abc123"],
            "pool": "basic",
            "tags": ["auto-register"],
        })

    def test_remote_pool_does_not_duplicate_admin_api_prefix_when_base_already_points_to_admin_api(self):
        self._configure(
            grok2api_remote_base="https://grok.example.com/admin/api",
            grok2api_pool_name="ssoSuper",
        )
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return DummyResponse({"status": "success", "count": 1})

        with patch.object(app, "http_post", side_effect=fake_post):
            ok = app.add_token_to_grok2api_remote_pool("sso=super123", email="a@example.com")

        self.assertTrue(ok)
        self.assertEqual([url for url, _ in calls], [
            "https://grok.example.com/admin/api/tokens/add",
        ])
        self.assertEqual(calls[0][1]["json"]["pool"], "super")

    def test_remote_pool_full_save_fallback_requires_opt_in_and_uses_etag(self):
        self._configure(grok2api_allow_legacy_full_save=True)
        get_calls = []
        post_calls = []

        def fake_post(url, **kwargs):
            post_calls.append((url, kwargs))
            if url.endswith("/tokens/add"):
                return DummyResponse(status_code=404)
            if url == "https://grok.example.com/admin/api/tokens":
                return DummyResponse({"status": "success"})
            return DummyResponse(status_code=404)

        def fake_get(url, **kwargs):
            get_calls.append((url, kwargs))
            if url == "https://grok.example.com/admin/api/tokens":
                return DummyResponse(
                    {"tokens": {"ssoBasic": []}},
                    headers={"ETag": '"version-7"'},
                )
            return DummyResponse(status_code=404)

        with patch.object(app, "http_post", side_effect=fake_post), \
                patch.object(app, "http_get", side_effect=fake_get):
            ok = app.add_token_to_grok2api_remote_pool("sso=fallback123", email="a@example.com")

        self.assertTrue(ok)
        self.assertEqual([url for url, _ in get_calls], [
            "https://grok.example.com/tokens",
            "https://grok.example.com/admin/api/tokens",
        ])
        self.assertEqual(post_calls[-1][0], "https://grok.example.com/admin/api/tokens")
        self.assertEqual(post_calls[-1][1]["headers"]["If-Match"], '"version-7"')
        self.assertEqual(post_calls[-1][1]["json"], {
            "ssoBasic": [{"token": "fallback123", "tags": ["auto-register"], "note": "a@example.com"}],
        })

    def test_remote_pool_legacy_fallback_is_disabled_by_default(self):
        self._configure()
        with patch.object(app, "http_post", return_value=DummyResponse(status_code=404)), \
                patch.object(app, "http_get") as get_mock:
            with self.assertRaises(app.RemoteTokenCompatibilityError):
                app.add_token_to_grok2api_remote_pool("abc")
        get_mock.assert_not_called()

    def test_remote_pool_500_does_not_fallback(self):
        self._configure(grok2api_allow_legacy_full_save=True)
        with patch.object(app, "http_post", return_value=DummyResponse(status_code=500, text="boom")), \
                patch.object(app, "http_get") as get_mock:
            with self.assertRaises(app.RemoteTokenRequestError):
                app.add_token_to_grok2api_remote_pool("abc")
        get_mock.assert_not_called()

    def test_remote_pool_legacy_fallback_rejects_missing_etag(self):
        self._configure(grok2api_allow_legacy_full_save=True)

        def fake_post(url, **kwargs):
            if url.endswith("/tokens/add"):
                return DummyResponse(status_code=404)
            return DummyResponse({"status": "success"})

        def fake_get(url, **kwargs):
            if url.endswith("/tokens"):
                return DummyResponse({"tokens": {"ssoBasic": []}})
            return DummyResponse(status_code=404)

        with patch.object(app, "http_post", side_effect=fake_post), \
                patch.object(app, "http_get", side_effect=fake_get):
            with self.assertRaises(app.RemoteTokenCompatibilityError):
                app.add_token_to_grok2api_remote_pool("abc")


if __name__ == "__main__":
    unittest.main()
'''
remote_path = ROOT / "tests" / "test_grok2api_remote_pool.py"
ast.parse(remote_tests)
remote_path.write_text(remote_tests, encoding="utf-8")

# 5) Shared flow tests with fake operations only.
flow_tests = r'''import unittest

from registration_flow import (
    RegistrationCallbacks,
    RegistrationOperations,
    run_batch,
)


class Cancelled(Exception):
    pass


class Retryable(Exception):
    pass


class FakeOps:
    def __init__(self, save_ok=True, observer_events=None):
        self.events = []
        self.save_ok = save_ok
        self.observer_events = observer_events if observer_events is not None else []
        self.account_no = 0

    def operations(self):
        return RegistrationOperations(
            start_browser=lambda: self.events.append("start"),
            restart_browser=lambda: self.events.append("restart"),
            browser_missing=lambda: False,
            open_signup_page=lambda: self.events.append("open"),
            fill_email_and_submit=self._email,
            save_mail_credential=lambda email, token: True,
            fill_code_and_submit=lambda email, token: "123456",
            fill_profile_and_submit=lambda: {"given_name": "A", "family_name": "B", "password": "pw"},
            wait_for_sso_cookie=lambda: "sso-token",
            enable_nsfw=lambda sso: (True, "ok"),
            persist_account_line=self._persist,
            queue_unsaved_result=lambda payload, error: True,
            add_tokens=lambda sso, email: {
                "local": {"enabled": False, "ok": None, "error": None},
                "remote": {"enabled": False, "ok": None, "error": None},
            },
            export_cpa=lambda email, password, sso: {"ok": False, "skipped": True},
            cleanup=lambda reason: self.events.append(("cleanup", reason)),
            sleep=lambda seconds: self.events.append(("sleep", seconds)),
            cancelled_exception=Cancelled,
            retry_exception=Retryable,
        )

    def _email(self):
        self.account_no += 1
        return f"user{self.account_no}@example.com", "mail-token"

    def _persist(self, email, password, sso):
        if not self.save_ok:
            raise OSError("disk full")
        self.events.append(("persist", email))


class RegistrationFlowTests(unittest.TestCase):
    def callbacks(self, logs=None):
        logs = logs if logs is not None else []
        return RegistrationCallbacks(log=logs.append, cancelled=lambda: False)

    def test_start_failure_still_runs_cleanup(self):
        fake = FakeOps()
        ops = fake.operations()
        ops.start_browser = lambda: (_ for _ in ()).throw(RuntimeError("start failed"))
        with self.assertRaises(RuntimeError):
            run_batch(1, self.callbacks(), lambda *args: None, ops)
        self.assertEqual(fake.events, [("cleanup", "任务结束")])

    def test_last_account_does_not_restart_browser(self):
        fake = FakeOps()
        batch = run_batch(1, self.callbacks(), lambda *args: None, fake.operations())
        self.assertEqual(batch.success_count, 1)
        self.assertNotIn("restart", fake.events)
        self.assertEqual(fake.events[-1], ("cleanup", "任务结束"))

    def test_cleanup_interval_does_not_repeat_after_unsaved_result(self):
        fake = FakeOps(save_ok=True)
        ops = fake.operations()
        original_persist = ops.persist_account_line
        calls = {"count": 0}

        def persist(email, password, sso):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("disk full")
            original_persist(email, password, sso)

        ops.persist_account_line = persist
        batch = run_batch(2, self.callbacks(), lambda *args: None, ops, cleanup_interval=1)
        interval_cleanups = [event for event in fake.events if isinstance(event, tuple) and "已成功" in event[1]]
        self.assertEqual(len(interval_cleanups), 1)
        self.assertEqual(batch.success_count, 1)
        self.assertEqual(batch.registered_unsaved_count, 1)

    def test_observer_failure_is_logged_and_batch_continues(self):
        fake = FakeOps()
        logs = []

        def broken_observer(*args):
            raise RuntimeError("ui broke")

        batch = run_batch(1, self.callbacks(logs), broken_observer, fake.operations())
        self.assertEqual(batch.success_count, 1)
        self.assertTrue(any("observer 执行失败" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
'''
flow_test_path = ROOT / "tests" / "test_registration_flow.py"
ast.parse(flow_tests)
flow_test_path.write_text(flow_tests, encoding="utf-8")

for path in (flow_path, main_path, browser_path, remote_path, flow_test_path):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
print("final flow fixes applied")
