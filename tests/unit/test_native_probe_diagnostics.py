"""Source refusal diagnostics identify failures without exposing subprocess data."""
from __future__ import annotations

import subprocess
import tarfile

import pytest

from hermes_feishu_card.install import native_hooks


@pytest.mark.parametrize("failure,kind", [
    (subprocess.TimeoutExpired("PRIVATE_COMMAND", 3, output=b"PRIVATE_OUTPUT"), "timeout"),
    (subprocess.CalledProcessError(1, "PRIVATE_COMMAND", stderr=b"PRIVATE_SECRET"), "command_failed"),
    (FileNotFoundError("PRIVATE_PATH"), "unavailable"),
], ids=["timeout", "command", "missing"])
def test_git_head_failure_retains_closed_result_and_redacted_cause(tmp_path, monkeypatch, caplog, failure, kind):
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(native_hooks, "_run_trusted_git", fail)
    assert native_hooks._git_head(tmp_path) == ""
    assert f"stage=git_head kind={kind}" in caplog.text
    assert "PRIVATE_" not in caplog.text


def test_git_status_failure_is_not_confused_with_commit_lookup(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(native_hooks, "_git_head", lambda root: native_hooks.FIXED_TAG_COMMIT)
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("PRIVATE_COMMAND", 5)
    monkeypatch.setattr(native_hooks, "_run_trusted_git", fail)
    assert native_hooks._git_source_state(tmp_path) == (native_hooks.FIXED_TAG_COMMIT, False)
    assert "stage=git_status kind=timeout" in caplog.text
    assert "PRIVATE_" not in caplog.text


@pytest.mark.parametrize("failure,kind", [
    (subprocess.TimeoutExpired("PRIVATE_COMMAND", 30), "timeout"),
    (tarfile.ReadError("PRIVATE_ARCHIVE"), "invalid_archive"),
    (ValueError("PRIVATE_EVIDENCE"), "invalid_evidence"),
], ids=["timeout", "archive", "evidence"])
def test_snapshot_failure_keeps_public_contract_and_all_capabilities_closed(tmp_path, monkeypatch, caplog, failure, kind):
    monkeypatch.setattr(native_hooks, "_git_source_state", lambda root: (native_hooks.FIXED_TAG_COMMIT, True))
    monkeypatch.setattr(native_hooks, "verify_provenance_slices", lambda *args, **kwargs: True)
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(native_hooks, "_build_trusted_source_snapshot", fail)
    result = native_hooks.probe_native_hook_capabilities(
        tmp_path, expected_commit=native_hooks.FIXED_TAG_COMMIT,
        runtime_python=tmp_path / "never-executed-python",
    )
    assert result.reason_code == "source_commit_mismatch"
    assert not result.capabilities.available
    assert result.source_commit == native_hooks.FIXED_TAG_COMMIT
    assert f"stage=snapshot kind={kind}" in caplog.text
    assert "PRIVATE_" not in caplog.text


def test_unknown_diagnostic_input_cannot_be_logged(caplog):
    native_hooks._log_source_probe_failure("PRIVATE_STAGE", ValueError("PRIVATE_BODY"))
    assert "stage=unknown kind=invalid_evidence" in caplog.text
    assert "PRIVATE_" not in caplog.text
