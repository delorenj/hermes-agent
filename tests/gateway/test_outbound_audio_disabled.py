"""HOLOC-26: executable fail-closed proofs; no secrets or live runtime involved."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from plugins.platforms.telegram.outbound_audio_config import audio_extra, both_enabled

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "plugins/platforms/telegram/ops"


def test_real_gateway_and_adapter_import_without_fcntl_while_disabled(tmp_path):
    code = """
import builtins
original = builtins.__import__
def without_fcntl(name, *args, **kwargs):
    if name == 'fcntl':
        raise ImportError('intentionally unavailable')
    return original(name, *args, **kwargs)
builtins.__import__ = without_fcntl
from gateway.run import GatewayRunner
from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.outbound_audio_drain import main
adapter = TelegramAdapter(PlatformConfig(extra={}))
assert adapter._outbound_audio_spool is None
assert main([]) == 0
print('real disabled imports: ok; fcntl absent')
"""
    env = {**os.environ, "HERMES_HOME": str(tmp_path / "isolated-home")}
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert "real disabled imports: ok" in result.stdout
    assert not (tmp_path / "isolated-home" / "spool").exists()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("script", ["outbound-audio-drain-runner.sh", "outbound-audio-drain.sh"])
@pytest.mark.parametrize("config,expected", [
    ("{}", 0),
    ("telegram: {extra: {outbound_audio_spool: {enabled: false}, outbound_audio_drain: {enabled: true}}}", 0),
    ("telegram: {extra: {outbound_audio_spool: {enabled: true}, outbound_audio_drain: {enabled: false}}}", 0),
    ('telegram: {extra: {outbound_audio_spool: {enabled: "true"}, outbound_audio_drain: {enabled: true}}}', 0),
    ("telegram: {extra: []}", 2),
    ("telegram: [", 2),
])
def test_actual_executable_refuses_before_missing_op_or_queue_access(tmp_path, config, expected, script):
    home, bindir = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    bindir.mkdir()
    (home / "config.yaml").write_text(config)
    queue = home / "spool/outbound_audio"
    queue.mkdir(parents=True)
    sentinel = queue / "untouched.json"
    sentinel.write_bytes(b"do-not-consume")
    for name in ("bash", "dirname"):
        (bindir / name).symlink_to(shutil.which(name))
    python = bindir / "python3"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    assert shutil.which("op", path=str(bindir)) is None
    result = subprocess.run([str(OPS / script), "holocene-pm"],
                            env={"PATH": str(bindir), "HERMES_HOME": str(home),
                                 "HOME": str(tmp_path), "TMPDIR": str(tmp_path)},
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == expected, result.stderr
    assert sentinel.read_bytes() == b"do-not-consume"
    assert sorted(p.name for p in queue.iterdir()) == ["untouched.json"]


def test_conflicting_yaml_locations_cannot_override_a_pin_off():
    with pytest.raises(ValueError, match="conflicting"):
        audio_extra({
            "telegram": {"extra": {"outbound_audio_spool": {"enabled": False}}},
            "platforms": {"telegram": {"extra": {"outbound_audio_spool": {"enabled": True}}}},
        })
    assert not both_enabled({"outbound_audio_spool": {"enabled": True}})
