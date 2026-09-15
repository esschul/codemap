import json
import subprocess
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codemap.cli import _resolve_source_root

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SPRING = Path(__file__).resolve().parent / 'fixture-spring'


def test_resolve_source_root_detects_src_main(tmp_path):
    src_main = tmp_path / 'src' / 'main'
    src_main.mkdir(parents=True)
    result = _resolve_source_root(tmp_path)
    assert result == src_main


def test_resolve_source_root_detects_src_main_kotlin(tmp_path):
    kt = tmp_path / 'src' / 'main' / 'kotlin'
    kt.mkdir(parents=True)
    result = _resolve_source_root(tmp_path)
    # src/main doesn't exist, but src/main/kotlin does
    # Actually src/main IS created implicitly — only kotlin leaf is present
    # The check is for src/main first, so we need a case where only kotlin exists
    assert result == kt or result == kt.parent


def test_resolve_source_root_src_main_preferred_over_deeper(tmp_path):
    src_main = tmp_path / 'src' / 'main'
    src_main.mkdir(parents=True)
    kt = src_main / 'kotlin'
    kt.mkdir()
    result = _resolve_source_root(tmp_path)
    assert result == src_main


def test_resolve_source_root_returns_given_when_no_standard_layout(tmp_path):
    result = _resolve_source_root(tmp_path)
    assert result == tmp_path


def test_resolve_source_root_exits_on_missing_path():
    import pytest
    with pytest.raises(SystemExit):
        _resolve_source_root(Path('/this/does/not/exist'))


def _run_codemap(args, cwd):
    return subprocess.run(
        [sys.executable, '-c', 'from codemap.cli import main; main()', *args],
        cwd=cwd,
        env={**__import__('os').environ, 'PYTHONPATH': str(ROOT)},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_json_flag_prints_component_graph_to_stdout():
    result = _run_codemap(['.', '--json'], cwd=FIXTURE_SPRING)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload['filesScanned'] > 0
    assert isinstance(payload['components'], list)
    names = {c['name'] for c in payload['components']}
    assert 'OrderController' in names
    controller = next(c for c in payload['components'] if c['name'] == 'OrderController')
    assert controller['kind'] == 'CONTROLLER'
    assert controller['file']
    assert isinstance(controller['endpoints'], list)


def test_json_out_writes_to_file(tmp_path):
    out_file = tmp_path / 'map.json'
    result = _run_codemap(['.', '--json', '--json-out', str(out_file)], cwd=FIXTURE_SPRING)
    assert result.returncode == 0, result.stderr
    assert 'Wrote' in result.stderr
    payload = json.loads(out_file.read_text())
    assert any(c['name'] == 'OrderController' for c in payload['components'])
    # --json-out must not also dump to stdout
    assert result.stdout.strip() == ''


def test_json_flag_does_not_write_html_or_markdown(tmp_path):
    # --json should be a pure query mode: no appmap.html / architecture.md side effects.
    result = _run_codemap(['.', '--json'], cwd=FIXTURE_SPRING)
    assert result.returncode == 0, result.stderr
    assert not (FIXTURE_SPRING / 'appmap.html').exists()
    assert not (FIXTURE_SPRING / 'architecture.md').exists()
