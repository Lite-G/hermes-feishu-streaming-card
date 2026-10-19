"""Legacy plugin config ownership, separate from the existing source manifest.

The companion record never patches Hermes or edits its configuration. Configuration
changes and exact restoration belong to the official plugin admission adapter.
"""
from __future__ import annotations

from dataclasses import dataclass
import ast
import json
from pathlib import Path
import re
import stat
import sys
from typing import Any

from . import plugin
from .manifest import validate_install_manifest

MARKER = ".hermes_feishu_card_observer.json"
PROTOCOL = "hfc-legacy-observer-v1"


def available(root: Path) -> bool:
    raw = _read(root / "agent" / "turn_response_intake.py")
    if raw is None:
        return False
    tree = ast.parse(raw)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_invoke_hook"
             and n.args and isinstance(n.args[0], ast.Constant)
             and n.args[0].value == "post_api_request"]
    required = {"session_id", "turn_id", "platform", "api_request_id", "usage",
                "model", "provider", "response_model", "started_at", "ended_at",
                "first_chunk_at", "api_duration", "moa_references"}
    return len(calls) == 1 and required <= {k.arg for k in calls[0].keywords}


def _read(path: Path) -> bytes | None:
    try:
        meta = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(meta.st_mode) or meta.st_size > 64 * 1024:
        raise ValueError("legacy observer record is unsafe")
    value = path.read_bytes()
    after = path.lstat()
    if (meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise ValueError("legacy observer record changed")
    return value


def _decode(raw: bytes) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(raw)
    if (type(value) is not dict or set(value) != {
        "protocol", "home", "python_identity", "plugin_config"
    } or value["protocol"] != PROTOCOL or type(value["home"]) is not str
            or not Path(value["home"]).is_absolute()):
        raise ValueError("legacy observer record is invalid")
    config = value["plugin_config"]
    if type(config) is not dict or set(config) != {
        "enabled_before", "added_by_hfc", "pre_sha256", "post_sha256",
        "backup_sha256", "config_backup_id"
    }:
        raise ValueError("legacy observer config ownership is invalid")
    if (any(type(config[k]) is not bool for k in ("enabled_before", "added_by_hfc"))
            or any(type(config[k]) is not str or not re.fullmatch(r"[0-9a-f]{64}", config[k])
                   for k in ("pre_sha256", "post_sha256", "backup_sha256"))
            or type(config["config_backup_id"]) is not str
            or not re.fullmatch(r"hfc-config-preimage-[0-9a-f]{32}", config["config_backup_id"])
            or type(value["python_identity"]) is not str
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["python_identity"])):
        raise ValueError("legacy observer config ownership is invalid")
    return value


def _preimage(home: Path, ownership: plugin.PluginOwnership) -> plugin.PluginConfigPreimage:
    state = home / ".hermes_feishu_card" / "install"
    key = ownership.config_backup_id
    return plugin.PluginConfigPreimage(
        state, state / (key + ".yaml"), state / (key + ".json"), key,
        ownership.pre_sha256, ownership.backup_sha256, ownership.enabled_before,
    )


def inspect(root: Path, *, home: Path | None = None) -> tuple[
    dict[str, Any], bytes, plugin.PluginConfigPreimage, plugin.PluginOwnership, bytes
] | None:
    """Read-only config/evidence check; a missing companion means pre-migration."""
    raw = _read(root / MARKER)
    if raw is None:
        return None
    record = _decode(raw)
    selected = Path(record["home"])
    for directory in (selected, selected / ".hermes_feishu_card",
                      selected / ".hermes_feishu_card" / "install"):
        plugin._require_directory(directory, "legacy observer evidence directory")
    if home is not None and selected != Path(home).resolve():
        raise ValueError("legacy observer HOME binding changed")
    ownership = plugin.PluginOwnership(**record["plugin_config"])
    preimage = _preimage(selected, ownership)
    if plugin._sha256(plugin._read_private_backup(preimage)) != ownership.pre_sha256:
        raise ValueError("legacy observer preimage mismatch")
    journal = json.loads(_read(preimage.journal_path) or b"null")
    if journal != {"protocol": "hfc-plugin-config-preimage-v1", "phase": "installed",
                   **ownership.sanitized()}:
        raise ValueError("legacy observer journal mismatch")
    config = plugin._require_regular_file(selected / "config.yaml", "Hermes config").read_bytes()
    if plugin._sha256(config) not in (ownership.pre_sha256, ownership.post_sha256):
        raise ValueError("Hermes config changed since legacy observer install")
    return record, raw, preimage, ownership, config


@dataclass
class Registration:
    binding: plugin.HermesRuntimeBinding

    def finish(self) -> None:
        pass  # All fallible config/marker work precedes the source transaction.

    def rollback(self) -> None:
        restore(self.binding.checkout_root, home=self.binding.hermes_home)


def prepare(binding: plugin.HermesRuntimeBinding) -> Registration | None:
    existing = inspect(binding.checkout_root, home=binding.hermes_home)
    if existing is not None:
        if existing[0]["python_identity"] != binding.python_identity:
            raise ValueError("legacy observer runtime binding changed")
        if plugin._sha256(existing[4]) != existing[3].post_sha256:
            raise ValueError("legacy observer restore incomplete; run restore first")
        return None
    probe = plugin.probe_plugin_entrypoint(binding)
    if probe.status != "verified":
        raise ValueError("legacy observer entry point is not verified")
    preimage = plugin.prepare_plugin_config(binding)
    ownership = None
    marker = binding.checkout_root / MARKER
    raw = None
    try:
        ownership = plugin.enable_plugin(binding, preimage)
        plugin.mark_plugin_config_installed(preimage, ownership)
        raw = (json.dumps({"protocol": PROTOCOL, "home": str(binding.hermes_home),
                           "python_identity": binding.python_identity,
                           "plugin_config": ownership.sanitized()}, sort_keys=True) + "\n").encode()
        plugin._write_private_new(marker, raw)
        return Registration(binding)
    except BaseException:
        if ownership is not None:
            plugin.restore_plugin_config(binding, preimage, ownership)
        # Retain the preimage if enable failed after an uncertain config write.
        if plugin._sha256(plugin._read_bound_config(binding)) == preimage.pre_sha256:
            if raw is not None and _read(marker) == raw:
                marker.unlink()
            plugin.cleanup_plugin_config_preimage(preimage)
        raise


def restore(root: Path, *, home: Path | None = None) -> None:
    evidence = inspect(root, home=home)
    if evidence is None:
        return
    record, raw, preimage, ownership, config = evidence
    binding = plugin.resolve_runtime_binding(
        checkout_root=root, hermes_home=record["home"], profile_id=None)
    if binding.python_identity != record["python_identity"]:
        raise ValueError("legacy observer runtime binding changed")
    if plugin._sha256(config) != ownership.pre_sha256:
        plugin.restore_plugin_config(binding, preimage, ownership)
    if _read(root / MARKER) != raw:
        raise ValueError("legacy observer record changed before cleanup")
    # Remove the admission marker before its evidence; a leftover preimage is inert.
    (root / MARKER).unlink()
    plugin.cleanup_plugin_config_preimage(preimage)


def observer_status(root: Path, *, home: Path | None = None) -> dict[str, str]:
    """Read-only installed admission state, not proof of a running plugin."""
    try:
        (root / MARKER).lstat()
    except OSError:
        # Do not disable unrelated Hybrid installations without marker evidence.
        return {"status": "not_installed", "reason": "marker_not_located"}
    try:
        evidence = inspect(root, home=home)
        if evidence is None:
            return {"status": "disabled", "reason": "marker_changed"}
        _, _, _, ownership, config = evidence
        if plugin._sha256(config) != ownership.post_sha256:
            return {"status": "disabled", "reason": "restore_incomplete"}
        manifest = json.loads(_read(root / ".hermes_feishu_card_manifest") or b"null")
        if type(manifest) is not dict or validate_install_manifest(manifest) not in (1, 2, 4):
            return {"status": "disabled", "reason": "legacy_manifest_invalid"}
        settings = plugin._load_config_mapping(config).get("plugins", {})
        if not isinstance(settings, dict):
            return {"status": "disabled", "reason": "plugin_config_invalid"}
        if (settings.get("isolation") == "host" or plugin.PLUGIN_KEY not in settings.get("enabled", [])
                or plugin.PLUGIN_KEY in settings.get("disabled", [])):
            return {"status": "disabled", "reason": "plugin_disabled_or_isolated"}
        return {"status": "active", "reason": "verified_install_admission"}
    except Exception:
        return {"status": "disabled", "reason": "observer_evidence_invalid"}


def process_observer_mode() -> str:
    """Only a positively located observer marker can disable Hybrid admission."""
    from ..instance_binding import gateway_process_binding
    binding = gateway_process_binding()
    if binding is None:
        manager_source = getattr(sys.modules.get("hermes_cli.plugins"), "__file__", None)
        resolver = getattr(sys.modules.get("hermes_constants"), "get_process_hermes_home", None)
        if not manager_source:
            return "hybrid"
        try:
            root = Path(manager_source).resolve(strict=True).parent.parent
        except Exception:
            return "hybrid"
        try:
            if not callable(resolver):
                raise ValueError("process HOME unavailable")
            home = Path(resolver()).resolve(strict=True)
        except Exception:
            return "hybrid" if observer_status(root)["status"] == "not_installed" else "disabled"
        binding = root, home
    root, home = binding
    status = observer_status(root, home=home)["status"]
    return {"active": "observe", "disabled": "disabled", "not_installed": "hybrid"}[status]
