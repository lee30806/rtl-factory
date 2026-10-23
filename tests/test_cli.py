"""rtlf 실행기: 명령 찾기, 목록, 대상 모듈로 넘기기."""
import importlib.util
import subprocess
import sys

from pipeline import cli
from test_pipeline import ROOT


def test_every_command_target_exists():
    for kind, target, _ in cli.COMMANDS.values():
        if kind == "module":
            assert importlib.util.find_spec(target) is not None, target
        else:
            assert (ROOT / target).is_file(), target


def test_resolve_group_command_and_single_commands():
    assert cli.resolve(["spec", "check", "a.md", "-o", "x"]) == (("spec", "check"), ["a.md", "-o", "x"])
    assert cli.resolve(["port", "status", "--run-dir", "d"]) == (("port", ""), ["status", "--run-dir", "d"])
    assert cli.resolve(["run", "s.md"]) == (("", "run"), ["s.md"])
    assert cli.resolve(["spec", "nope"])[0] is None
    assert cli.resolve(["nope"])[0] is None


def test_usage_and_unknown_command(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "spec check" in out and "port" in out and "gateway" not in out
    assert cli.main(["spec"]) == 0
    out = capsys.readouterr().out
    assert "spec l1" in out and "approve" not in out
    assert cli.main(["spec", "nope"]) == 2
    assert "모르는 명령" in capsys.readouterr().err


def test_dispatch_passes_args_and_exit_code(tmp_path):
    """하위 프로세스로 실제 모듈을 부른다 (runpy가 sys.argv와 __main__을 바꾸므로)."""
    spec = ROOT / "blocks" / "axis_crc_append" / "block_spec.md"
    r = subprocess.run([sys.executable, "-m", "pipeline", "spec", "check", str(spec), "-o", str(tmp_path)],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "review.md").is_file()
    r = subprocess.run([sys.executable, "-m", "pipeline", "spec", "check"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 2 and "usage" in r.stderr   # argparse 오류가 그대로 나옴
