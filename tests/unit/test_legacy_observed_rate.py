import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
from types import SimpleNamespace

import pytest
import yaml

from hermes_feishu_card import hook_runtime, hermes_plugin_runtime as runtime_module
from hermes_feishu_card.events import SidecarEvent
from hermes_feishu_card.render import render_card
from hermes_feishu_card.session import CardSession
from hermes_feishu_card.observed_rate import ObservedRates
from hermes_feishu_card.install import legacy_observer as install, plugin


def observation(**changes):
    return dict(session_id="synthetic-session", turn_id="synthetic-turn", platform="feishu",
                api_request_id="synthetic-request", model="synthetic-model", provider="synthetic-provider",
                usage={"input_tokens": 10, "output_tokens": 80}, api_duration=2.0, **changes)


@pytest.fixture
def runtime(monkeypatch):
    posted = []
    runtime = runtime_module.PluginRuntime(post=lambda *a: posted.append(a), observe_only=True)
    monkeypatch.setattr(hook_runtime, "_plugin_runtime", lambda: runtime)
    yield runtime, posted
    runtime.close()


def complete(runtime, **changes):
    values = {"response": "A synthetic answer long enough for measured tokens. " * 8,
              "agent_result": {"session_id": "synthetic-session", "output_tokens": 80}}
    values.update(changes)
    return hook_runtime._event_data("message.completed", values, None, None)


def test_legacy_completion_renders_rate_with_no_plugin_events(runtime):
    rt, posted = runtime
    values = observation()
    rt.handle_pre_llm_call(**values)
    rt.handle_post_api_request(**values)
    rt.handle_post_api_request(**values)  # duplicate counted once
    for name in ("handle_post_llm_call", "handle_on_session_end", "handle_pre_tool_call",
                 "handle_post_tool_call", "handle_pre_approval_request", "handle_subagent_start"):
        getattr(rt, name)(**values)
    data = complete(rt)
    assert data["rate"] == {"output_tps": 40.0, "basis": "request"}
    session = CardSession(conversation_id="synthetic-c", message_id="synthetic-m", chat_id="synthetic-chat")
    assert session.apply(SidecarEvent.from_dict({
        "schema_version": "1", "event": "message.completed", "conversation_id": "synthetic-c",
        "message_id": "synthetic-m", "chat_id": "synthetic-chat", "platform": "feishu",
        "sequence": 1, "created_at": 1.0, "data": data,
    }))
    assert "~40 tok/s" in json.dumps(render_card(session, footer_fields=["output_rate"]))
    assert "rate" not in complete(rt)
    # A duplicate pre/post pair after consumption cannot revive the observation.
    rt.handle_pre_llm_call(**values)
    rt.handle_post_api_request(**values)
    assert "rate" not in complete(rt)
    assert posted == []
    assert rt.runtime_activity_snapshot() == (0, True)
    assert not rt.start_runtime_interaction_listener(b"s" * 32)


@pytest.mark.parametrize("case", ["session", "model", "provider", "absent", "broken"])
def test_unknown_rate_is_hidden_without_blocking_completion(runtime, monkeypatch, case):
    rt, _ = runtime
    values = observation()
    rt.handle_pre_llm_call(**values)
    rt.handle_post_api_request(**values)
    changes = {}
    if case in ("model", "provider"):
        rt.handle_post_api_request(**{**values, "api_request_id": "second", case: "different"})
        changes["agent_result"] = {"session_id": "synthetic-session", "output_tokens": 160}
    elif case in ("estimate", "clipped", "mismatch", "session"):
        changes["agent_result"] = {"session_id": "other" if case == "session" else "synthetic-session",
                                   "output_tokens": {"estimate": 0, "clipped": 99999, "mismatch": 81, "session": 80}[case]}
    elif case == "absent":
        monkeypatch.setattr(hook_runtime, "_plugin_runtime", lambda: None)
    elif case == "broken":
        monkeypatch.setattr(rt, "take_observed_rate", lambda *_: 1 / 0)
    data = complete(rt, **changes)
    assert data["answer"] and "rate" not in data


def test_ttl_capacity_ambiguity_and_late_observations():
    clock = [0.0]
    cache = ObservedRates(now=lambda: clock[0], capacity=2, ttl=10)
    first = observation()
    cache.start(first)
    cache.observe(first)
    second = {**first, "session_id": "second"}
    third = {**first, "session_id": "overflow"}
    for values in (second, third):
        cache.start(values)
        cache.observe(values)
    assert cache.take("overflow") is None
    assert cache.take("second")[1] == 80
    clock[0] = 10
    cache.observe(first)
    assert cache.take("synthetic-session") is None
    cache.start(first)
    cache.observe(first)
    overlap = {**first, "turn_id": "overlap"}
    cache.start(overlap)
    cache.observe(overlap)
    assert cache.take("synthetic-session") is None


def test_capacity_rejection_cannot_borrow_same_session_previous_rate():
    cache = ObservedRates(capacity=1)
    values = observation()
    cache.start(values)
    cache.observe(values)
    cache.start({**values, "turn_id": "new-turn"})
    assert cache.take("synthetic-session") is None


def test_footer_estimate_does_not_disqualify_exact_observations(runtime):
    rt, _ = runtime
    rt.handle_pre_llm_call(**observation())
    rt.handle_post_api_request(**observation())
    data = complete(rt, response="x" * 320, agent_result={"session_id": "synthetic-session"})
    assert data["tokens"]["output_tokens"] == 80
    assert data["rate"]["output_tps"] == 40.0


@pytest.fixture
def installed(tmp_path, monkeypatch):
    root, home = tmp_path / "checkout", tmp_path / "home"
    root.mkdir()
    home.mkdir()
    original = b"# retain comments and CRLF\r\nplugins: {}\r\nother: value\r\n"
    (home / "config.yaml").write_bytes(original)
    binding = plugin.HermesRuntimeBinding(root, Path(sys.executable), Path(sys.executable),
        "sha256:" + "a" * 64, home, home / "config.yaml", root, root)
    monkeypatch.setattr(plugin, "resolve_runtime_binding", lambda **_: binding)
    monkeypatch.setattr(plugin, "probe_plugin_entrypoint", lambda _: SimpleNamespace(status="verified"))
    calls = []
    def enable(_):
        calls.append("enable")
        config, _ = plugin._expected_enabled_config(yaml.safe_load(binding.config_path.read_bytes()))
        binding.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        return 0
    monkeypatch.setattr(plugin, "_run_official_enable", enable)
    return binding, original, calls


@pytest.mark.parametrize("command", ["restore", "uninstall", "rollback"])
def test_config_install_reinstall_and_exact_restore(installed, command):
    binding, original, calls = installed
    registration = install.prepare(binding)
    assert registration is not None
    marker = (binding.checkout_root / install.MARKER).read_bytes()
    assert install.prepare(binding) is None
    assert calls == ["enable"]
    assert (binding.checkout_root / install.MARKER).read_bytes() == marker
    registration.finish()
    if command == "rollback":
        registration.rollback()
    else:
        install.restore(binding.checkout_root)
    assert binding.config_path.read_bytes() == original
    assert not (binding.checkout_root / install.MARKER).exists()
    assert list((binding.hermes_home / ".hermes_feishu_card" / "install").iterdir()) == []


def test_marker_failure_rolls_back_config(installed, monkeypatch):
    binding, original, _ = installed
    write = plugin._write_private_new
    def fail(path, data):
        if path.name == install.MARKER:
            raise OSError("synthetic marker failure")
        return write(path, data)
    monkeypatch.setattr(plugin, "_write_private_new", fail)
    with pytest.raises(OSError, match="synthetic marker"):
        install.prepare(binding)
    assert binding.config_path.read_bytes() == original


def test_config_drift_refuses_restore(installed):
    binding, _, _ = installed
    install.prepare(binding)
    binding.config_path.write_bytes(b"other: changed\n")
    with pytest.raises(ValueError, match="changed"):
        install.restore(binding.checkout_root)
    assert binding.config_path.read_bytes() == b"other: changed\n"


@pytest.mark.parametrize("command", ["restore", "uninstall", "failure", "late_failure"])
def test_cli_source_and_plugin_transaction(installed, monkeypatch, command):
    from hermes_feishu_card import cli
    from hermes_feishu_card.install import recovery
    from hermes_feishu_card.install.detect import detect_hermes
    binding, original, calls = installed
    root = binding.checkout_root
    fixture = Path(__file__).parents[1] / "fixtures" / "hermes_decomposed"
    shutil.copytree(fixture, root, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    (root / "VERSION").write_text("0.22.2\n")
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*.py")}
    monkeypatch.setattr(cli, "resolve_runtime_binding", lambda **_: binding)
    monkeypatch.setattr(cli, "_ensure_hermes_runtime_package", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ensure_hermes_feishu_sdk", lambda *a, **k: None)
    # Eligibility source contract has its own upstream test; exercise the actual
    # source transaction here using the repository's hermetic decomposed fixture.
    monkeypatch.setattr(install, "available", lambda _: True)
    if command == "late_failure":
        from hermes_feishu_card.install import file_transaction
        write = file_transaction._atomic_write_text_portable
        failed = []
        def late_fail(path, *a, **k):
            if path.name == ".hermes_feishu_card_manifest" and not failed:
                failed.append(True)
                raise OSError("synthetic late source failure")
            return write(path, *a, **k)
        monkeypatch.setattr(recovery, "_secure_dirfd_transactions_supported", lambda: False)
        monkeypatch.setattr(file_transaction, "_atomic_write_text_portable", late_fail)
        assert cli.main(["install", "--hermes-dir", str(root), "--yes"]) == 1
        assert failed
    elif command == "failure":
        def fail(*a, **k):
            raise ValueError("synthetic source install failure")
        monkeypatch.setattr(recovery, "install_decomposed", fail)
        assert cli.main(["install", "--hermes-dir", str(root), "--yes"]) == 1
    else:
        assert cli.main(["install", "--hermes-dir", str(root), "--yes"]) == 0
        assert cli.main(["install", "--hermes-dir", str(root), "--yes"]) == 0
        assert calls == ["enable"]
        assert cli._diagnose_install_state(detect_hermes(root))["status"] == "installed"
        assert cli._repair_install_state(detect_hermes(root)) == []
        result = cli.main([command, "--hermes-dir", str(root), "--yes"])
        if os.name == "nt":
            # Existing recovery gate deliberately requires POSIX dirfd. Preserve
            # its refusal, restored config and retry evidence on Windows.
            assert result == 1
            assert binding.config_path.read_bytes() == original
            assert (root / install.MARKER).exists()
            return
        assert result == 0
    assert binding.config_path.read_bytes() == original
    assert before == {p: (root / p).read_bytes() for p in before}
    assert not (root / install.MARKER).exists()


def test_enable_journal_failure_restores_preimage(installed, monkeypatch):
    binding, original, _ = installed
    monkeypatch.setattr(plugin, "_rewrite_private_journal", lambda *a, **k: (_ for _ in ()).throw(OSError("journal")))
    with pytest.raises(OSError, match="journal"):
        install.prepare(binding)
    assert binding.config_path.read_bytes() == original


def test_existing_v4_install_gains_observer_without_replacing_source_preimages(installed, monkeypatch, capsys):
    from hermes_feishu_card import cli
    from hermes_feishu_card.install import recovery
    from hermes_feishu_card.install.detect import detect_hermes
    binding, _, calls = installed
    root = binding.checkout_root
    shutil.copytree(Path(__file__).parents[1] / "fixtures" / "hermes_decomposed", root,
                    dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    (root / "VERSION").write_text("0.22.2\n")
    recovery.install_decomposed(detect_hermes(root))
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    monkeypatch.setattr(cli, "resolve_runtime_binding", lambda **_: binding)
    monkeypatch.setattr(cli, "_ensure_hermes_runtime_package", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ensure_hermes_feishu_sdk", lambda *a, **k: None)
    monkeypatch.setattr(install, "available", lambda _: True)
    assert cli.main(["install", "--hermes-dir", str(root), "--yes"]) == 0
    assert "gateway.restart_required" in capsys.readouterr().out
    assert calls == ["enable"]
    assert before == {p: (root / p).read_bytes() for p in before}


@pytest.mark.parametrize("case", ["home", "unknown_version", "uninspectable"])
def test_invalid_marker_cannot_admit_observer(installed, monkeypatch, case):
    binding, _, _ = installed
    install.prepare(binding)
    from hermes_feishu_card import instance_binding
    monkeypatch.setattr(instance_binding, "gateway_process_binding", lambda: (
        binding.checkout_root, binding.hermes_home / "other" if case == "home" else binding.hermes_home))
    if case == "unknown_version":
        marker = binding.checkout_root / install.MARKER
        value = json.loads(marker.read_bytes())
        value["protocol"] = "unknown-version"
        marker.write_text(json.dumps(value))
    if case == "uninspectable":
        monkeypatch.setattr(install, "_read", lambda _: (_ for _ in ()).throw(PermissionError("synthetic")))
    assert install.process_observer_mode() == "disabled"


@pytest.mark.parametrize("isolation", [None, "host"])
def test_real_marker_bootstrap_never_starts_control_or_listener(installed, monkeypatch, isolation):
    binding, _, _ = installed
    if isolation:
        binding.config_path.write_text("plugins:\n  isolation: host\n", encoding="utf-8")
    install.prepare(binding)
    (binding.checkout_root / ".hermes_feishu_card_manifest").write_text('{"manifest_version":2}')
    from hermes_feishu_card import instance_binding
    monkeypatch.setattr(instance_binding, "gateway_process_binding", lambda: (binding.checkout_root, binding.hermes_home))
    def forbidden(*a, **k):
        pytest.fail("observe-only must not start runtime ownership or transport")
    monkeypatch.setattr(runtime_module, "acquire_runtime_control", forbidden)
    monkeypatch.setattr(runtime_module, "read_transport_root_secret", forbidden)
    hooks = {}
    ctx = SimpleNamespace(register_hook=lambda name, fn: hooks.update({name: fn}))
    runtime_module.reset_production_plugin_runtime_for_tests()
    try:
        runtime_module.bootstrap_plugin_runtime(ctx)
        assert set(hooks) == set(runtime_module.OFFICIAL_HOOKS)
        rt = runtime_module.active_plugin_runtime()
        if isolation:
            assert rt is None
        else:
            assert rt.observe_only
            hooks["pre_llm_call"](**observation())
            hooks["post_api_request"](**observation())
            assert rt.take_observed_rate("synthetic-session")[1] == 80
    finally:
        runtime_module.reset_production_plugin_runtime_for_tests()


def test_real_v0222_plugin_manager_load(tmp_path):
    configured = os.environ.get("HFC_LEGACY_PLUGIN_SOURCE_ROOT")
    if not configured:
        pytest.skip("set HFC_LEGACY_PLUGIN_SOURCE_ROOT to the read-only Hermes v0.22.2 checkout")
    root = Path(configured).resolve()
    assert root.is_dir()
    commit = subprocess.check_output(
        ["git", "-c", "safe.directory=" + root.as_posix(), "rev-parse", "HEAD"],
        cwd=root, text=True, timeout=10,
    ).strip()
    assert commit.startswith("d923a5eb48"), "fixture must be the pinned Hermes v0.22.2 commit"
    assert install.available(root)
    code = r'''
import sys, os
from pathlib import Path
sys.path[:0] = [sys.argv[2], sys.argv[1]]
from hermes_cli.plugins import PluginManager
from hermes_feishu_card import hermes_plugin_runtime as runtime
from hermes_feishu_card.install import legacy_observer
# The loader/entrypoint/registration are real. The install fact is exercised
# separately above, without mutating this upstream checkout.
legacy_observer.process_observer_mode = lambda: "observe"
runtime.acquire_runtime_control = lambda **kw: (_ for _ in ()).throw(AssertionError("hello"))
manager = PluginManager(scope_key=os.environ["HERMES_HOME"])
manifests = [m for m in manager._scan_entry_points() if m.name == "hermes-feishu-card"]
assert len(manifests) == 1
manager._load_plugin(manifests[0])
assert manager._plugins["hermes-feishu-card"].enabled
assert set(manager._hooks) == set(runtime.OFFICIAL_HOOKS)
values = dict(session_id="synthetic-session", turn_id="synthetic-turn", platform="feishu",
              api_request_id="synthetic-api", model="model", provider="provider",
              usage={"input_tokens": 10, "output_tokens": 80}, api_duration=2.0)
for name in ("pre_llm_call", "post_api_request"):
    for callback in manager._hooks[name]:
        callback(**values)
assert runtime.active_plugin_runtime().take_observed_rate("synthetic-session")[1] == 80
runtime.reset_production_plugin_runtime_for_tests()
'''
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  enabled: [hermes-feishu-card]\n  isolation: in_process\n")
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "HERMES_HOME": str(home),
           "PYTHONDONTWRITEBYTECODE": "1"}
    child = subprocess.run([sys.executable, "-c", code, str(Path.cwd()), str(root)],
                           env=env, capture_output=True, text=True, timeout=60)
    assert child.returncode == 0, child.stdout + child.stderr


@pytest.mark.parametrize("legacy_counts", [(300, 700), (0, 99999)])
def test_cached_agent_consecutive_turns_use_own_observations(runtime, legacy_counts):
    rt, _ = runtime
    for index, legacy_count in enumerate(legacy_counts):
        values = {**observation(), "turn_id": f"turn-{index}",
                  "usage": {"input_tokens": 10, "output_tokens": 80 + index * 40}}
        rt.handle_pre_llm_call(**values)
        rt.handle_post_api_request(**values)
        result = {"session_id": "synthetic-session", "output_tokens": legacy_count}
        data = complete(rt, response="x" * 1000, agent_result=result)
        expected = hook_runtime._completion_tokens({"agent_result": result}, "x" * 1000)
        assert data["tokens"] == expected  # Footer semantics are unchanged.
        assert data["rate"]["output_tps"] == 40 + index * 20
        session = CardSession(conversation_id="c", message_id="m", chat_id="chat")
        session.apply(SidecarEvent.from_dict({
            "schema_version": "1", "event": "message.completed", "conversation_id": "c",
            "message_id": "m", "chat_id": "chat", "platform": "feishu",
            "sequence": 1, "created_at": 1.0, "data": data}))
        assert f"~{40 + index * 20} tok/s" in json.dumps(render_card(session, footer_fields=["output_rate"]))


def test_consumed_turns_do_not_exhaust_capacity():
    cache = ObservedRates()
    for index in range(600):
        values = {**observation(), "turn_id": f"turn-{index}"}
        cache.start(values)
        cache.observe(values)
        assert cache.take("synthetic-session")[0]["output_tps"] == 40
        assert len(cache.entries) <= 256


@pytest.mark.parametrize("change", [{"usage": None}, {"usage": {"input_tokens": 10}},
    {"moa_references": ["reference"]}, {"api_duration": -1}, {"usage": {"output_tokens": 19}}])
def test_invalid_observations_remain_hidden(runtime, change):
    rt, _ = runtime
    values = {**observation(), **change}
    rt.handle_pre_llm_call(**values)
    rt.handle_post_api_request(**values)
    assert "rate" not in complete(rt)


@pytest.mark.parametrize("failure", ["root", "home"])
def test_unlocatable_observer_preserves_hybrid_bootstrap(tmp_path, monkeypatch, failure):
    from hermes_feishu_card import instance_binding
    root = tmp_path / "checkout"
    source = root / "hermes_cli" / "plugins.py"
    source.parent.mkdir(parents=True)
    if failure != "root":
        source.write_text("")
    (root / ".hermes_feishu_card_manifest").write_text('{"manifest_version":3}')
    monkeypatch.setattr(instance_binding, "gateway_process_binding", lambda: None)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", SimpleNamespace(__file__=str(source)))
    def unavailable():
        raise OSError("synthetic HOME failure")
    monkeypatch.setitem(sys.modules, "hermes_constants", SimpleNamespace(get_process_hermes_home=unavailable))
    assert install.process_observer_mode() == "hybrid"
    monkeypatch.setattr(runtime_module, "read_transport_root_secret", lambda: b"s" * 32)
    monkeypatch.setattr(runtime_module.PluginRuntime, "start_runtime_interaction_listener", lambda *a: True)
    monkeypatch.setattr(runtime_module, "acquire_runtime_control", lambda **k: SimpleNamespace(close=lambda: None))
    ctx = SimpleNamespace(register_hook=lambda *a: None)
    runtime_module.reset_production_plugin_runtime_for_tests()
    try:
        runtime_module.bootstrap_plugin_runtime(ctx)
        rt = runtime_module.active_plugin_runtime()
        assert rt is not None and not rt.observe_only
    finally:
        runtime_module.reset_production_plugin_runtime_for_tests()


def test_observer_diagnostics_are_read_only(installed):
    binding, _, _ = installed
    root = binding.checkout_root
    assert install.observer_status(root)["status"] == "not_installed"
    install.prepare(binding)
    manifest = root / ".hermes_feishu_card_manifest"
    manifest.write_text('{"manifest_version":2}')
    for expected in ("active", "disabled"):
        if expected == "disabled":
            manifest.write_text('{"manifest_version":3}')
        before = {p: p.read_bytes() for p in root.parent.rglob("*") if p.is_file()}
        status = install.observer_status(root, home=binding.hermes_home)
        assert status["status"] == expected and status["reason"]
        assert before == {p: p.read_bytes() for p in before}


@pytest.mark.parametrize("consumer", ["legacy", "v3"])
@pytest.mark.parametrize("failure", ["official", "journal", "none"])
def test_shared_plugin_transaction_portable_rollback(installed, monkeypatch, consumer, failure):
    binding, original, _ = installed
    # Both installers use this same public config transaction boundary.
    if consumer == "v3":
        (binding.checkout_root / ".hermes_feishu_card_manifest").write_text('{"manifest_version":3}')
    monkeypatch.delattr(plugin.os, "fchmod", raising=False)
    if failure == "official":
        def fail(_):
            binding.config_path.write_bytes(b"plugins: {}\n")
            raise OSError("official failure")
        monkeypatch.setattr(plugin, "_run_official_enable", fail)
    elif failure == "journal":
        monkeypatch.setattr(plugin, "_rewrite_private_journal", lambda *a, **k: (_ for _ in ()).throw(OSError("journal")))
    if consumer == "legacy":
        execute = lambda: install.prepare(binding)
    else:
        preimage = plugin.prepare_plugin_config(binding)
        execute = lambda: plugin.enable_plugin(binding, preimage)
    if failure != "none":
        with pytest.raises((OSError, plugin.PluginConfigRefused)):
            execute()
    else:
        result = execute()
        if consumer == "legacy":
            result.rollback()
        else:
            plugin.restore_plugin_config(binding, preimage, result)
    assert binding.config_path.read_bytes() == original
