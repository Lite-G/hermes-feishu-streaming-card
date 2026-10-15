"""Hermes v0.22.1+ split turn mixins (HFC #428), fully offline.

Hermes 297552a9d6 moved the completion/agent:end seam into
GatewayTurnPersistenceMixin, TurnRunner._status_callback_sync into
GatewayTurnProgressMixin and the approval body into _render_approval_sync.
The split support is a fallback: earlier layouts must render byte-for-byte as
before, and every unverified split shape must fail closed.
"""
import asyncio
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from hermes_feishu_card import cli, hook_runtime
from hermes_feishu_card.install import decomposed, decomposed_ownership, patcher, recovery
from hermes_feishu_card.install import split_turn_contracts
from hermes_feishu_card.install.detect import detect_hermes

FIXTURES = Path(__file__).parents[1] / "fixtures"
SPLIT = FIXTURES / "hermes_decomposed_split"
SINGLE = FIXTURES / "hermes_decomposed"
SPLIT_TARGETS = set(patcher.DECOMPOSED_SPLIT_TURN_TARGETS)


def _copy(fixture, root):
    shutil.copytree(fixture, root, ignore=shutil.ignore_patterns("__pycache__"))
    (root / "VERSION").write_text("0.20.0\n")
    return root


@pytest.fixture
def hermes(tmp_path):
    return _copy(SPLIT, tmp_path / "hermes")


@pytest.fixture
def single(tmp_path):
    return _copy(SINGLE, tmp_path / "single")


def sources(root):
    return {name: (root / name).read_bytes() for name in decomposed.SOURCE_TARGETS
            if (root / name).exists()}


def _markers(rendered):
    joined = b"\n".join(rendered.values()).decode()
    return sorted(line.strip() for line in joined.splitlines()
                  if line.strip().startswith("# HERMES_FEISHU_CARD_") and line.strip().endswith("BEGIN"))


def test_split_cli_install_doctor_repeat_remove_roundtrip(hermes, monkeypatch):
    monkeypatch.setattr(cli, "_ensure_hermes_runtime_package", lambda detection: None)
    monkeypatch.setattr(cli, "_ensure_hermes_feishu_sdk", lambda detection: None)
    before = sources(hermes)
    detection = detect_hermes(hermes)
    assert detection.supported and detection.compatibility == "full", detection.reason
    assert detection.capability_locations["completion_return"] == ("gateway/run_turn_persistence.py",)
    assert detection.capability_locations["status_callback"] == ("gateway/run_turn_progress.py",)
    assert detection.capability_locations["approval_callback"] == ("gateway/run_turn_runner.py",)
    assert cli.main(["install", "--hermes-dir", str(hermes), "--yes"]) == 0
    installed = sources(hermes)
    assert {n for n in before if before[n] != installed[n]} == set(patcher.DECOMPOSED_TARGETS) | SPLIT_TARGETS
    assert cli.main(["install", "--hermes-dir", str(hermes), "--yes"]) == 0
    assert installed == sources(hermes)
    detection = detect_hermes(hermes)
    assert cli._doctor_hermes_report(detection)["compatibility"] == "full"
    assert cli._diagnose_install_state(detection)["status"] == "installed"
    for name, raw in installed.items():
        compile(raw, name, "exec")
    assert cli.main(["uninstall", "--hermes-dir", str(hermes), "--yes"]) == 0
    assert sources(hermes) == before
    assert not list(hermes.rglob("*.hermes_feishu_card.bak"))
    assert recovery.plan_recovery(detect_hermes(hermes)).state == "clean"


def test_split_layout_keeps_every_hook_group_of_the_single_class_layout(hermes, single):
    assert _markers(decomposed_ownership.render(sources(hermes))) == \
        _markers(decomposed_ownership.render(sources(single)))


def test_single_class_layout_output_is_independent_of_split_fallback(single, monkeypatch):
    """Earlier layouts must not change by a single byte because of the fallback."""
    expected = decomposed_ownership.render(sources(single))
    detection = detect_hermes(single)
    for name in ("find_split_completion_delivery", "find_split_status_callback",
                 "find_delegated_approval_renderer"):
        monkeypatch.setattr(split_turn_contracts, name, lambda *a, **kw: None)
    for name in ("split_completion_caller_verified", "split_completion_verified",
                 "split_status_verified", "split_status_runner_verified"):
        monkeypatch.setattr(split_turn_contracts, name, lambda *a, **kw: False)
    assert decomposed_ownership.render(sources(single)) == expected
    disabled = detect_hermes(single)
    assert (disabled.compatibility, disabled.capability_locations) == \
        (detection.compatibility, detection.capability_locations)


@pytest.mark.parametrize("target,old,new", [
    # completion owner drift
    ("gateway/run_turn_persistence.py", 'emit("agent:end"', 'emit("agent:other"'),
    ("gateway/run_turn_persistence.py", "class GatewayTurnPersistenceMixin:", "class OtherMixin:"),
    ("gateway/run_turn_persistence.py", "(self, hook_ctx, agent_result, response)", "(self, hook_ctx, response, agent_result)"),
    ("gateway/run_turn_persistence.py", "async def _hmwa_deliver_turn_response(", "def _hmwa_deliver_turn_response("),
    # completion caller drift
    ("gateway/run_turn.py", "class GatewayTurnMixin(GatewayTurnPersistenceMixin):", "class GatewayTurnMixin:"),
    ("gateway/run_turn.py", "import GatewayTurnPersistenceMixin\n", "import GatewayTurnPersistenceMixin as GatewayTurnPersistenceMixin\n"),
    ("gateway/run_turn.py", "from gateway.run_turn_persistence import", "from gateway.other_persistence import"),
    ("gateway/run_turn.py", "agent_messages, response, _footer_line", "agent_messages, \"wrong\", _footer_line"),
    ("gateway/run_turn.py", "        await self._hmwa_post_turn_hooks(hook_ctx, agent_result, response)\n", ""),
    ("gateway/run_turn.py", "    async def _run_agent(self, event, source):",
     "    async def _hmwa_post_turn_hooks(self, hook_ctx, agent_result, response):\n"
     "        await self.hooks.emit(\"agent:end\", {})\n\n    async def _run_agent(self, event, source):"),
    # approval delegation drift
    ("gateway/run_turn_runner.py", "        self._render_approval_sync(approval_data)\n", "        self._render_approval_sync(dict(approval_data))\n"),
    ("gateway/run_turn_runner.py", "    def _render_approval_sync(self, approval_data):\n        ctx = self._ctx",
     "    def _render_approval_sync(self, approval_data):\n        ctx = self._other"),
    ("gateway/run_turn_runner.py", "    def _render_approval_sync(self, approval_data):",
     "    def _bypass(self, approval_data):\n        self._render_approval_sync(approval_data)\n\n"
     "    def _render_approval_sync(self, approval_data):"),
    ("gateway/run_turn_runner.py", "def _render_approval_sync(self, approval_data):", "def _render_approval_sync(self, data):"),
])
def test_split_contract_drift_fails_closed(hermes, target, old, new):
    path = hermes / target
    content = path.read_text()
    assert old in content
    path.write_text(content.replace(old, new, 1))
    detection = detect_hermes(hermes)
    assert not detection.supported, detection.capability_locations
    if target != "gateway/run_turn_runner.py":
        with pytest.raises(ValueError):
            decomposed.install(detection)


@pytest.mark.parametrize("target,old,new", [
    ("gateway/run_turn_runner.py", "class TurnRunner(GatewayTurnProgressMixin):", "class TurnRunner:"),
    ("gateway/run_turn_runner.py", "from gateway.run_turn_progress import", "from gateway.other_progress import"),
    ("gateway/run_turn_progress.py", "ctx = self._ctx", "ctx = self._other"),
    ("gateway/run_turn_progress.py", "(self, event_type, message)", "(self, message, event_type)"),
])
def test_unverified_split_status_stays_optional_and_unpatched(hermes, target, old, new):
    path = hermes / target
    path.write_text(path.read_text().replace(old, new, 1))
    detection = detect_hermes(hermes)
    assert detection.supported and detection.compatibility == "partial", detection.reason
    assert not detection.capabilities["status_callback"]
    before = sources(hermes)
    decomposed.install(detection)
    assert (hermes / "gateway/run_turn_progress.py").read_bytes() == before["gateway/run_turn_progress.py"]
    decomposed.restore(detect_hermes(hermes))
    assert sources(hermes) == before


def test_split_status_callback_must_not_also_live_on_turn_runner(hermes):
    path = hermes / "gateway/run_turn_runner.py"
    path.write_text(path.read_text().replace(
        "    def _setup_stream_consumer(",
        "    def _status_callback_sync(self, event_type, message):\n        ctx = self._ctx\n\n"
        "    def _setup_stream_consumer(", 1))
    assert not split_turn_contracts.split_status_verified(
        {name: raw.decode() for name, raw in sources(hermes).items()})


def _execute(source, target, name):
    namespace = {}
    exec(patcher.apply_gateway_fragment(source, target), namespace)
    return namespace[name]


@pytest.mark.parametrize("choice", ["once", ""])
def test_delegated_approval_hook_runs_inside_renderer_and_keeps_wrapper_order(monkeypatch, choice):
    """The hook replaces only native rendering; generation check and viewer
    registration in the wrapper still run, in Hermes' own order."""
    calls = []
    monkeypatch.setattr(hook_runtime, "request_approval_choice_from_hermes_locals",
                        lambda *a, **kw: calls.append("card") or choice)
    monkeypatch.setattr(hook_runtime, "resolve_approval_choice",
                        lambda data, key, picked: calls.append(("resolve", key, picked)) or 1)
    import sys
    stub = SimpleNamespace(GatewayTurnProgressMixin=type("GatewayTurnProgressMixin", (), {}))
    monkeypatch.setitem(sys.modules, "gateway", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "gateway.run_turn_progress", stub)
    runner_cls = _execute((SPLIT / "gateway/run_turn_runner.py").read_text(),
                          "gateway/run_turn_runner.py", "TurnRunner")
    ctx = SimpleNamespace(source=SimpleNamespace(platform="feishu"), event_message_id="fixture-message",
                          _loop_for_step=None, _run_still_current=lambda: True,
                          _status_chat_id="fixture-chat", session_key="fixture-session",
                          send_approval=lambda *a: calls.append(("native",) + a))
    runner = runner_cls(SimpleNamespace(), ctx)
    runner._approval_owner = SimpleNamespace(
        check_approval_generation=lambda: calls.append("check"),
        register_approval=lambda key, data: calls.append(("register", key)))
    runner._approval_notify_sync({"command": "echo fixture", "request_id": "fixture-request"})
    if choice:
        assert calls == ["check", "card", ("resolve", "fixture-session", "once"), ("register", "fixture-session")]
    else:
        assert calls == ["check", "card", ("native", "fixture-session",
                                           {"command": "echo fixture", "request_id": "fixture-request"}),
                         ("register", "fixture-session")]


def test_split_status_hook_executes_in_progress_mixin(monkeypatch):
    seen, native = [], []
    monkeypatch.setattr(hook_runtime, "handle_status_from_hermes_locals",
                        lambda payload, *, event_type, message: seen.append((payload["chat_id"], message)) or False)
    mixin = _execute((SPLIT / "gateway/run_turn_progress.py").read_text(),
                     "gateway/run_turn_progress.py", "GatewayTurnProgressMixin")
    holder = mixin()
    holder._ctx = SimpleNamespace(source=SimpleNamespace(platform="feishu"), event_message_id="fixture-message",
                                  _loop_for_step=None, _run_still_current=lambda: True,
                                  _status_chat_id="fixture-chat", status_queue=SimpleNamespace(put=native.append))
    holder._status_callback_sync("status", "Compacting context")
    assert seen == [("fixture-chat", "Compacting context")]
    assert native == [("status", "Compacting context")]


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
async def test_split_completion_executes_in_persistence_mixin_with_caller_timing(monkeypatch, accepted):
    events = []
    async def post_turn(*args):
        events.append("agent:end")
    async def completed(payload, *, event_name):
        events.append((event_name, payload["answer"], payload["duration"]))
        return accepted
    monkeypatch.setattr(hook_runtime, "emit_from_hermes_locals", lambda *a, **kw: False)
    monkeypatch.setattr(hook_runtime, "can_stage_exact_base_completion", lambda payload: False)
    monkeypatch.setattr(hook_runtime, "build_event", lambda *a, **kw: {"data": {}})
    monkeypatch.setattr(hook_runtime, "emit_from_hermes_locals_async", completed)
    import sys
    mixin = _execute((SPLIT / "gateway/run_turn_persistence.py").read_text(),
                     "gateway/run_turn_persistence.py", "GatewayTurnPersistenceMixin")
    monkeypatch.setitem(sys.modules, "gateway", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "gateway.run_turn_persistence",
                        SimpleNamespace(GatewayTurnPersistenceMixin=mixin))
    turn = _execute((SPLIT / "gateway/run_turn.py").read_text(), "gateway/run_turn.py", "GatewayTurnMixin")()
    turn.hooks = SimpleNamespace(emit=post_turn)
    source = SimpleNamespace(platform="feishu")
    event = SimpleNamespace(source=source, message_id="fixture-message", reply_to_message_id="fixture-parent")
    result = await turn._handle_message_with_agent(event, source, "fixture-session", 1)
    assert result == (None if accepted else "answer")
    assert events == ["agent:end", ("message.completed", "answer", 1.25)]


def test_upgrade_into_split_layout_requires_explicit_acceptance_and_restores_new_source(single):
    upgraded = sources(single)
    decomposed.install(detect_hermes(single))
    for name in ("gateway/run_turn.py", "gateway/run_turn_runner.py", *SPLIT_TARGETS):
        (single / name).write_bytes((SPLIT / name).read_bytes())
        upgraded[name] = (SPLIT / name).read_bytes()
    detection = detect_hermes(single)
    assert detection.supported, detection.reason
    plan = recovery.plan_recovery(detection)
    assert plan.state == "stale_unpatched" and not plan.executable
    with pytest.raises(ValueError, match="accept-hermes-upgrade"):
        decomposed.install(detection)
    approved = recovery.plan_recovery(detection, accept_hermes_upgrade=True)
    recovery.execute_recovery(detection, expected_fingerprint=approved.fingerprint, accept_hermes_upgrade=True)
    assert recovery.plan_recovery(detect_hermes(single)).state == "installed"
    assert all((single / (name + decomposed.BACKUP_SUFFIX)).exists() for name in SPLIT_TARGETS)
    decomposed.restore(detect_hermes(single))
    assert sources(single) == upgraded


@pytest.mark.parametrize("damage", ["marker", "backup"])
def test_unowned_split_file_with_hfc_evidence_is_refused(single, damage):
    decomposed.install(detect_hermes(single))
    for name in ("gateway/run_turn.py", "gateway/run_turn_runner.py", *SPLIT_TARGETS):
        (single / name).write_bytes((SPLIT / name).read_bytes())
    target = single / "gateway/run_turn_progress.py"
    if damage == "marker":
        target.write_bytes(target.read_bytes() + b"# HERMES_FEISHU_CARD_STATUS_PATCH_BEGIN\n")
    else:
        (single / ("gateway/run_turn_progress.py" + decomposed.BACKUP_SUFFIX)).write_bytes(target.read_bytes())
    before = sources(single)
    detection = detect_hermes(single)
    plan = recovery.plan_recovery(detection, accept_hermes_upgrade=True)
    assert not plan.executable
    with pytest.raises(ValueError):
        recovery.execute_recovery(detection, expected_fingerprint=plan.fingerprint, accept_hermes_upgrade=True)
    assert sources(single) == before
