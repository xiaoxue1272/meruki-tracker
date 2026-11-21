"""构建流水线的门禁与发布配置。

这里测的是 ``.github/workflows/*.yml`` 本身，因为这两处都出过「绿着的坏」：
release 步骤一个附件都没挂上、job 却依然 success；以及「CI 红了还能发 Release」。
YAML 写错了通常要等推 tag 才炸，代价是重打一次 tag 再等三平台构建，
所以在单测里把结构和小段 shell 逻辑都钉住。

``verify`` 那段 bash 是真的拿去跑的——用一个假 ``curl`` 顶替网络，
把「有成功记录 / 失败 / 没记录 / 还在跑 / 接口异常」几种情况都走一遍。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".github" / "workflows" / "build.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _git_bash_candidates() -> list[str]:
    """Git for Windows 的 bash.exe 常见安装位置，按优先级排列。"""
    roots = [
        # Windows 的环境变量名不区分大小写，用大写写法满足 lint。
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
    ]
    return [os.path.join(root, "Git", "bin", "bash.exe") for root in roots]


def _load_pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _bash() -> str | None:
    """找一个真正能跑 workflow 脚本的 bash。

    Windows 上 ``bash`` 往往先解析到 ``C:\\Windows\\System32\\bash.exe``，那是 WSL
    启动器，没装发行版时会直接报错退出——而 Actions 的 ``shell: bash`` 用的是 Git
    Bash。这里按 Git Bash 的安装位置找，找不到就跳过依赖它的用例。
    """
    if os.name != "nt":
        return shutil.which("bash")

    for candidate in _git_bash_candidates():
        if os.path.isfile(candidate):
            return candidate
    return None


BASH = _bash()

needs_bash = pytest.mark.skipif(BASH is None, reason="找不到可用的 bash（Windows 上需要 Git Bash）")

# 假 curl 是靠改 PATH 顶替真可执行文件的；Git Bash 会把 Windows 式 PATH 转成
# POSIX 形式，转换细节不受我们控制。而 verify job 本身只跑在 ubuntu-latest 上，
# 在 macOS 上验过同一段 shell 就够了，不必在 Windows 上赌路径转换。
needs_posix = pytest.mark.skipif(
    os.name == "nt", reason="verify job 只在 ubuntu 上跑；Windows 的 PATH 转换会让假 curl 不可靠"
)


def _load(path: Path) -> dict:
    """读 workflow；顺带绕开 PyYAML 把裸 ``on:`` 读成 True 的历史包袱。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if True in data:
        data["on"] = data.pop(True)
    return data


@pytest.fixture(scope="module")
def build() -> dict:
    return _load(BUILD)


@pytest.fixture(scope="module")
def ci() -> dict:
    return _load(CI)


class TestTriggers:
    def test_build_only_runs_for_version_tags(self, build: dict):
        # 产物构建很贵，普通 push / 手动触发都不该跑。
        assert build["on"] == {"push": {"tags": ["v*"]}}

    def test_ci_still_runs_on_branches_and_prs(self, ci: dict):
        assert ci["on"]["push"]["branches"] == ["**"]
        assert "pull_request" in ci["on"]

    def test_ci_no_longer_exposes_workflow_call(self, ci: dict):
        # build 改成查 CI 记录之后，复用入口就没用了；留着只会让人以为还在跑测试。
        assert "workflow_call" not in ci["on"]


class TestJobChain:
    def test_build_waits_for_verify(self, build: dict):
        assert build["jobs"]["build"]["needs"] == "verify"

    def test_release_waits_for_both(self, build: dict):
        assert build["jobs"]["release"]["needs"] == ["verify", "build"]

    def test_verify_does_not_run_the_test_suite_itself(self, build: dict):
        # 用户要求 Build 里不再跑测试：verify 只能是查记录，不能再 uses: ci.yml。
        verify = build["jobs"]["verify"]
        assert "uses" not in verify
        assert [s["name"] for s in verify["steps"]] == ["Require a green CI run for this commit"]

    def test_verify_has_a_timeout(self, build: dict):
        # 等 CI 记录是循环等待，没有 timeout 会一直占着 runner。
        assert build["jobs"]["verify"]["timeout-minutes"] > 0

    def test_workflow_grants_actions_read(self, build: dict):
        # workflow 级 permissions 一旦显式声明，没列出的权限就变 none。
        # verify 要 GET /actions/workflows/ci.yml/runs，少了 actions: read 会 403。
        perms = build["permissions"]
        assert perms.get("actions") == "read", perms
        assert perms.get("contents") == "write", perms


class TestBashResolution:
    """Windows 上 `bash` 会先撞到 C:\\Windows\\System32\\bash.exe（WSL 启动器）。

    没装发行版时它直接报「Windows Subsystem for Linux has no installed
    distributions.」并退出，用例就会红在一片和被测逻辑无关的输出上。所以必须显式
    去找 Git Bash——Actions 的 `shell: bash` 也是这么做的。
    """

    def test_windows_prefers_git_bash_over_the_wsl_stub(self, monkeypatch):
        monkeypatch.setattr(os, "name", "nt")
        wanted = _git_bash_candidates()[0]
        monkeypatch.setattr(os.path, "isfile", lambda path: path == wanted)

        resolved = _bash()

        assert resolved == wanted
        assert "System32" not in resolved

    def test_windows_without_git_bash_returns_none(self, monkeypatch):
        # 找不到就该跳过，而不是退回 WSL 那个假 bash 让用例红掉。
        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setattr(os.path, "isfile", lambda path: False)

        assert _bash() is None

    def test_posix_uses_which(self, monkeypatch):
        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/local/bin/{name}")

        assert _bash() == "/usr/local/bin/bash"


class TestReleaseAssets:
    def test_no_display_name_syntax_in_files(self, build: dict):
        # action-gh-release@v3 的 files 只认路径或 glob；带 # 的整串会被当 glob 匹配，
        # 结果是 Release 建出来但一个附件都没有（踩过，见文档集 08 §9.60）。
        files = _release_files(build)
        assert files, "release 必须挂附件"
        assert not [f for f in files if "#" in f]

    def test_unmatched_files_must_fail_the_job(self, build: dict):
        # 默认 false 会静默放行，只在日志里打一行 🤔，job 依然绿。
        assert _release_step(build)["with"]["fail_on_unmatched_files"] is True

    def test_each_asset_matches_its_matrix_entry(self, build: dict):
        assets = _matrix_assets(build)
        assert len(assets) == 3
        files = _release_files(build)
        assert files == [f"artifacts/{a}/{a}" for a in assets]

    def test_upload_path_uses_the_renamed_asset(self, build: dict):
        # 三个平台的可执行文件都叫 meruki-tracker，靠改名保证附件名唯一。
        upload = _step(build, "build", "Upload artifact")
        assert upload["with"]["path"] == "dist/${{ matrix.asset }}"
        assert upload["with"]["if-no-files-found"] == "error"

    def test_rename_runs_between_smoke_test_and_upload(self, build: dict):
        names = [s.get("name") for s in build["jobs"]["build"]["steps"]]
        assert names.index("Smoke test binary") < names.index("Rename to release asset name")
        assert names.index("Rename to release asset name") < names.index("Upload artifact")

    @needs_bash
    def test_rename_moves_the_windows_exe_too(self, build: dict, tmp_path: Path):
        # 真跑那段脚本：Linux/macOS 产出 meruki-tracker、Windows 产出 .exe 后缀，
        # 两种都要能改成 matrix.asset 的名字，否则附件就传不上去。
        # 只在对应平台上构造该平台的真实产物名——Windows 上不存在无扩展名的产物，
        # 硬造一个反而会踩 MSYS 的 .exe 后缀解析，测出来的不是流水线的行为。
        script = _step(build, "build", "Rename to release asset name")["run"]

        if os.name == "nt":
            cases = [("meruki-tracker.exe", "meruki-tracker-windows.exe")]
        else:
            cases = [
                ("meruki-tracker", "meruki-tracker-linux"),
                ("meruki-tracker", "meruki-tracker-macos-arm64"),
            ]
        assert len(cases) >= 1

        for produced, asset in cases:
            workdir = tmp_path / asset
            (workdir / "dist").mkdir(parents=True)
            (workdir / "dist" / produced).write_text("binary")
            runnable = script.replace("${{ matrix.asset }}", asset)

            proc = subprocess.run(
                [BASH, "-c", runnable], cwd=workdir, capture_output=True, text=True
            )
            assert proc.returncode == 0, f"{produced} → {asset} 失败：{proc.stdout}{proc.stderr}"
            assert sorted(p.name for p in (workdir / "dist").iterdir()) == [asset], (
                f"{produced} 应该被改名为 {asset}"
            )

    def test_rename_does_not_leave_the_old_name_behind(self, build: dict):
        # 旧名残留两个文件，upload 的 path 又是指定单一文件，会让人以为传了俩。
        script = _step(build, "build", "Rename to release asset name")["run"]
        assert script.count("mv ") == 1


class TestGateLogic:
    """把 verify 里的 bash 抠出来真跑，用假 curl 顶替 GitHub API。"""

    def _run(self, build: dict, tmp_path: Path, mode: str, *, second: str = "") -> tuple[int, str]:
        script = _step(build, "verify", "Require a green CI run for this commit")["run"]
        # 真跑要等 20 秒/轮、总共 10 分钟，测试里压成没人会等的值。
        script = script.replace("sleep 20", "sleep 0").replace("+ 600 )", "+ 3 )")

        bindir = tmp_path / "bin"
        bindir.mkdir()
        counter = tmp_path / "count"
        counter.write_text("0")
        fake = bindir / "curl"
        fake.write_text(_FAKE_CURL % {"mode": mode, "second": second or mode})
        fake.chmod(0o755)

        env = {
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "GH_TOKEN": "x",
            "REPO": "owner/repo",
            "SHA": "abc123def456789",
        }
        proc = subprocess.run(
            [BASH, "-c", script], capture_output=True, text=True, env=env, timeout=120
        )
        return proc.returncode, proc.stdout + proc.stderr

    @needs_posix
    def test_green_record_lets_the_build_through(self, build: dict, tmp_path: Path):
        rc, out = self._run(build, tmp_path, "success")
        assert rc == 0, out
        assert "成功" in out

    @needs_posix
    def test_red_record_blocks_the_build(self, build: dict, tmp_path: Path):
        rc, out = self._run(build, tmp_path, "failed")
        assert rc != 0, out
        assert "没通过" in out

    @needs_posix
    def test_missing_record_blocks_the_build(self, build: dict, tmp_path: Path):
        # 从没推过分支就直接打 tag 的情况：不能发 Release。
        rc, out = self._run(build, tmp_path, "none")
        assert rc != 0, out
        assert "没有任何 CI 记录" in out

    @needs_posix
    def test_api_error_blocks_the_build(self, build: dict, tmp_path: Path):
        # 宁可失败也不要「读不到就当通过」。
        rc, out = self._run(build, tmp_path, "bad")
        assert rc != 0, out
        assert "返回异常" in out

    @needs_posix
    def test_pending_run_is_waited_for_then_accepted(self, build: dict, tmp_path: Path):
        # tag 和分支几乎同时推出去时，CI 可能还在跑，应该等一会儿而不是直接拒绝。
        rc, out = self._run(build, tmp_path, "pending", second="success")
        assert rc == 0, out
        assert out.count("commit") >= 2, out

    @needs_posix
    def test_pending_forever_gives_up(self, build: dict, tmp_path: Path):
        rc, out = self._run(build, tmp_path, "pending", second="pending")
        assert rc != 0, out
        assert "还没跑完" in out

    def test_the_gate_only_looks_at_this_commit(self, build: dict):
        # 拿别的 commit 的成功记录放行等于没校验。
        script = _step(build, "verify", "Require a green CI run for this commit")["run"]
        assert "head_sha=$SHA" in script
        assert "ci.yml" in script


# 假 curl：第一次按 $1 模式回，第二次（若有）按第二个模式回。
# 计数器用文件，因为 shell 函数的返回值没法跨进程带状态。
_FAKE_CURL = """#!/bin/sh
counter="$(dirname "$0")/../count"
n=$(( $(cat "$counter") + 1 ))
echo "$n" > "$counter"
mode="%(mode)s"
if [ "$n" -gt 1 ]; then mode="%(second)s"; fi
case "$mode" in
  success)  printf '%%s' '{"workflow_runs":[{"status":"completed","conclusion":"success"}]}' ;;
  failed)   printf '%%s' '{"workflow_runs":[{"status":"completed","conclusion":"failure"}]}' ;;
  pending)  printf '%%s' '{"workflow_runs":[{"status":"in_progress","conclusion":null}]}' ;;
  none)     printf '%%s' '{"workflow_runs":[]}' ;;
  bad)      printf '%%s' '{"message":"Bad credentials"}' ;;
esac
"""


def _release_step(build: dict) -> dict:
    return build["jobs"]["release"]["steps"][-1]


def _release_files(build: dict) -> list[str]:
    raw = _release_step(build)["with"]["files"]
    return [line.strip() for line in raw.strip().splitlines() if line.strip()]


def _matrix_assets(build: dict) -> list[str]:
    return [m["asset"] for m in build["jobs"]["build"]["strategy"]["matrix"]["include"]]


def _step(build: dict, job: str, name: str) -> dict:
    for step in build["jobs"][job]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"{job} 里没有名为 {name} 的步骤")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])


class TestConsoleScripts:
    """控制台入口的名字是用户每天敲的东西，改名时最容易漏。"""

    def test_short_alias_is_mt_not_the_old_mw(self):
        # `mw` 是 meruki-watch 时代的遗留；项目改名后简写跟着改成 `mt`。
        # 两个都留着会让人分不清哪个是正经入口。
        scripts = _load_pyproject()["project"]["scripts"]
        assert "mt" in scripts, scripts
        assert "mw" not in scripts, scripts

    def test_both_entry_points_point_at_the_same_main(self):
        scripts = _load_pyproject()["project"]["scripts"]
        assert scripts["meruki-tracker"] == "meruki_tracker.__main__:main"
        assert scripts["mt"] == scripts["meruki-tracker"]

    def test_docs_do_not_advertise_the_old_alias(self):
        # README / CONTEXT 是对外说明，写着旧命令就会教错人。
        for name in ("README.md", "CONTEXT.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            assert "简写 `mw`" not in text, name
            assert "简写 `mt`" in text, name
