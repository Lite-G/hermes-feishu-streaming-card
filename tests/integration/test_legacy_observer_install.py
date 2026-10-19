"""Pinned official plugin enable and byte-exact legacy recovery, without Gateway.

CI must supply all three inputs. Local generic suites explicitly skip this module;
HFC_REQUIRE_LEGACY_PLUGIN_INSTALL=1 makes missing inputs a failure, never a pass.
The ordinary wheel is installed by the caller in one reusable private runtime.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


PIN = "d923a5eb487a17c0a68801bba9ef0f56c81040a8"

# Installed in the test runtime only. Do not change admission or force observe.
# Fail safely before any HFC transport/ownership operation can have side effects,
# recording attempted calls even when production code catches their exceptions.
SENSOR = r'''
import json, os, sys
from pathlib import Path
home = Path(os.environ.get("HERMES_HOME", os.environ.get("HOME", ".")))
def profile(frame, event, arg):
    if event != "call":
        return
    module = frame.f_globals.get("__name__", "") or ""
    name = frame.f_code.co_name
    if module.startswith("hermes_feishu_card") and name in {
        "acquire_runtime_control", "start_runtime_interaction_listener",
        "read_transport_root_secret", "post_event", "_post_event", "_post_json",
    }:
        with (home / "hfc-forbidden.jsonl").open("a") as out:
            out.write(json.dumps([module, name]) + "\n")
        raise RuntimeError("isolated test forbids HFC ownership/transport")
sys.setprofile(profile)
def audit(event, args):
    if event in {"socket.connect", "socket.bind"}:
        # Official CLI can probe its private HOME control socket. All probes are
        # denied, so it cannot contact a real service or bind a listener.
        raise PermissionError("offline official CLI integration")
sys.addaudithook(audit)
# Supply tools/project only; execute pinned upstream admission, resolver and
# configuration publication unchanged, as the PM-generation integration does.
provision = home / "hfc-pm-tools.json"
if provision.exists():
    tools = json.loads(provision.read_text())
    import pm.paths, pm._uv, pm.client, pm.install
    pm.paths.repo_root = lambda: Path(tools["project"])
    pm._uv._toolchain = lambda **kwargs: (Path(tools["uv"]), Path(tools["python"]))
    pm.client.sync_venv = lambda *args, **kwargs: pm.install.sync_venv(*args, **kwargs)
'''


@pytest.fixture(scope="module")
def official_runtime(tmp_path_factory):
    names = ("HFC_LEGACY_PLUGIN_SOURCE_ROOT", "HFC_LEGACY_PLUGIN_PYTHON", "HFC_TEST_WHEEL")
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        message = "ordinary-wheel official enable inputs missing: " + ", ".join(missing)
        if os.environ.get("HFC_REQUIRE_LEGACY_PLUGIN_INSTALL") == "1":
            pytest.fail(message)
        pytest.skip(message)
    source = Path(os.environ[names[0]]).resolve()
    python = Path(os.environ[names[1]]).absolute()
    wheel = Path(os.environ[names[2]]).resolve()
    result = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True, timeout=10)
    assert result.stdout.strip() == PIN
    result = subprocess.run(["git", "-C", str(source), "status", "--porcelain"],
                            capture_output=True, text=True, check=True, timeout=10)
    assert not result.stdout.strip(), "pinned fixture must be clean"
    assert wheel.is_file()
    probe = subprocess.run([str(python), "-I", "-c",
        "import json,sys,sysconfig,hermes_feishu_card as h;"
        "print(json.dumps([sys.prefix,sysconfig.get_paths()['purelib'],h.__file__]))"],
        capture_output=True, text=True, check=True, timeout=20)
    prefix, site, origin = map(Path, json.loads(probe.stdout))
    assert prefix in origin.parents, "HFC must come from ordinary runtime site-packages"
    assert "site-packages" in origin.parts
    import zipfile
    with zipfile.ZipFile(wheel) as archive:
        resources = [name for name in archive.namelist() if name.startswith("hermes_feishu_card/") and not name.endswith("/")]
        assert resources
        for name in resources:
            assert (site / name).read_bytes() == archive.read(name), name
    direct_url = next(site.glob("hermes_feishu_streaming_card-*.dist-info/direct_url.json"))
    provenance = json.loads(direct_url.read_text())
    assert not provenance.get("dir_info", {}).get("editable")
    assert provenance["archive_info"]["hashes"]["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert not (site / "sitecustomize.py").exists(), "do not replace caller customization"
    sensor = site / "sitecustomize.py"
    sensor.write_text(SENSOR)
    path_file = site / "hfc-official-fixture.pth"
    assert not path_file.exists()
    try:
        smoke = tmp_path_factory.mktemp("sensor-smoke")
        probe = subprocess.run([str(python), "-I", "-c",
            "from hermes_feishu_card.hermes_plugin_runtime import read_transport_root_secret\n"
            "try: read_transport_root_secret()\n"
            "except RuntimeError: pass\n"],
            cwd=smoke, env={"HOME": str(smoke), "HERMES_HOME": str(smoke)},
            capture_output=True, text=True, timeout=20)
        assert probe.returncode == 0, probe.stdout + probe.stderr
        assert "read_transport_root_secret" in (smoke / "hfc-forbidden.jsonl").read_text()
        yield source, python, prefix, path_file
    finally:
        sensor.unlink()
        path_file.unlink(missing_ok=True)


def _run(python, root, home, code, *args, expected=0):
    child = subprocess.run([str(python), "-I", "-c", code, str(root), *map(str, args)],
        cwd=home, env={"HOME": str(home), "HERMES_HOME": str(home),
                      "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=180)
    assert child.returncode == expected, child.stdout + child.stderr
    assert not (home / "hfc-forbidden.jsonl").exists(), (
        (home / "hfc-forbidden.jsonl").read_text() if (home / "hfc-forbidden.jsonl").exists() else "")
    return child


@pytest.mark.parametrize("command,migrate", [("restore", False), ("uninstall", True)])
def test_official_enable_legacy_install_repeat_doctor_exact_restore(official_runtime, tmp_path, command, migrate):
    source, python, prefix, path_file = official_runtime
    root, home = tmp_path / "hermes", tmp_path / "home"
    # Copy only immutable tracked source. No existing .venv, private state or
    # production HOME can enter this fixture.
    shutil.copytree(source, root, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    home.mkdir()
    original = (b"# exact comments and CRLF\r\nplugins:\r\n  enabled: []\r\n"
                b"  disabled: [keep-disabled]\r\n  isolation: in_process\r\n"
                b"platform_toolsets:\r\n  feishu: [terminal]\r\nother: keep\r\n_config_version: 50\r\n")
    (home / "config.yaml").write_bytes(original)
    (root / ".venv").symlink_to(prefix, target_is_directory=True)
    path_file.write_text(str(root) + "\n")
    uv = shutil.which("uv")
    assert uv, "pre-provisioned uv required for the actual PM transaction"
    project = home / "pm-project"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nname="official-enable-test"\nversion="1"\nrequires-python=">=3.11"\ndependencies=[]\n[tool.uv]\npackage=false\n')
    subprocess.run([uv, "lock", "--offline"], cwd=project, check=True, capture_output=True, timeout=30)
    (home / "hfc-pm-tools.json").write_text(json.dumps({"uv": uv, "python": str(python), "project": str(project)}))
    from hermes_feishu_card.install import legacy_observer
    assert legacy_observer.available(root)
    sources = json.loads((Path(__file__).parents[1] / "fixtures/hermes_upstream_sources.json").read_text())["latest_stable"]["sha256"]
    before = {name: (root / name).read_bytes() for name in sources}
    if migrate:
        _run(python, root, home, r'''
import sys
from pathlib import Path
from hermes_feishu_card.install.detect import detect_hermes
from hermes_feishu_card.install.recovery import install_decomposed
assert install_decomposed(detect_hermes(Path(sys.argv[1])))
''')
    output = _run(python, root, home, r'''
import sys,json
from pathlib import Path
from hermes_feishu_card import cli
from hermes_feishu_card.install import plugin,legacy_observer
from hermes_feishu_card.install.detect import detect_hermes
root=Path(sys.argv[1]);home=Path.home()
# Retain real child CLI diagnostics without replacing the official adapter.
import subprocess
original_run=subprocess.run
def logged_run(*args,**kwargs):
    result=original_run(*args,**kwargs)
    if "hermes_cli.main" in args[0]:
        print("official CLI:",result.returncode,result.stdout,result.stderr)
        print("official config:",(home/"config.yaml").read_text())
    return result
subprocess.run=logged_run
# The environment already supplies the exact ordinary HFC wheel. SDK
# provisioning is irrelevant: this test never imports a Feishu adapter.
cli._ensure_hermes_runtime_package=lambda *a,**k:None
cli._ensure_hermes_feishu_sdk=lambda *a,**k:None
assert cli.main(["install","--hermes-dir",str(root),"--hermes-home",str(home),"--yes"]) == 0
assert cli.main(["install","--hermes-dir",str(root),"--hermes-home",str(home),"--yes"]) == 0
# Run the public doctor command; missing SDK/runtime health is deliberately
# separate from installed source/observer admission and can yield exit 1.
import contextlib,io
config=home/"hfc.yaml"
config.write_text("server:\n  host: 127.0.0.1\n  port: 9\n")
report=io.StringIO()
with contextlib.redirect_stdout(report):
    result=cli.main(["doctor","--config",str(config),"--hermes-dir",str(root),"--hermes-home",str(home),"--json"])
assert result in (0,1)
report=json.loads(report.getvalue())
assert report["install_state"]["status"] == "installed"
assert report["install_state"]["legacy_observer"]["status"] == "active"
assert legacy_observer.observer_status(root,home=home)["status"] == "active"
print("official-enable-installed")
''')
    assert "official-enable-installed" in output.stdout
    installed = {name: (root / name).read_bytes() for name in before}
    assert installed != before
    # Public discovery and hook delivery: no private _scan/_load entrypoint,
    # synthetic observer-mode override or source HFC import is used.
    _run(python, root, home, r'''
import sys
from pathlib import Path
import hermes_cli.plugins as plugins
from hermes_feishu_card import hermes_plugin_runtime as runtime
plugins.discover_plugins()
rt=runtime.active_plugin_runtime()
assert rt is not None and rt.observe_only
values=dict(session_id="synthetic-session",turn_id="synthetic-turn",platform="feishu",
    api_request_id="synthetic-request",model="synthetic-model",provider="synthetic-provider",
    usage={"input_tokens":10,"output_tokens":80},api_duration=2.0)
plugins.invoke_hook("pre_llm_call",**values)
plugins.invoke_hook("post_api_request",**values)
assert rt.take_observed_rate("synthetic-session")[1] == 80
assert rt.runtime_activity_snapshot() == (0,True)
assert rt.runtime_interaction_listener_snapshot()["accepting"] is False
runtime.reset_production_plugin_runtime_for_tests()
''')
    _run(python, root, home, r'''
import sys
from hermes_feishu_card import cli
assert cli.main([sys.argv[2],"--hermes-dir",sys.argv[1],"--hermes-home",str(__import__("pathlib").Path.home()),"--yes"]) == 0
''', command)
    assert (home / "config.yaml").read_bytes() == original
    assert {name: (root / name).read_bytes() for name in before} == before
    assert not (root / ".hermes_feishu_card_observer.json").exists()
    assert not (root / ".hermes_feishu_card_manifest").exists()
