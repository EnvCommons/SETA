import asyncio
import base64
import functools
import io
import json
import random
import re
import shlex
import tarfile

import pytest
from openreward.api.sandboxes.client import AsyncSandboxesAPI
from openreward.api.sandboxes.types import RunResult

import seta
from seta import EmptyInput, SETAEnv, dockerfile_to_bash

TASK_ID = 999_999
WEIGHTS = {"test_output_file_exists": 0.4, "test_output_contents": 0.6}
# Captured before the fixture stubs asyncio.sleep (the grader's retry backoff), so
# the fake can still yield to the event loop and let concurrent calls interleave.
_yield_to_loop = asyncio.sleep
REFERENCE_NAMES = ("test_outputs.py", "solution.sh", "run-tests.sh", "weights.json", "task.yaml", "draft_spec.md")


def run_async(test):
    """Run an async test function to completion on a fresh event loop."""
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return wrapper


class FakeSandbox(AsyncSandboxesAPI):
    """In-memory sandbox. Only ``run`` is faked: ``check_run``/``download`` are the
    SDK's own implementations, so they fail the same way on a non-zero exit."""

    def __init__(self, *, pytest_writes_report=True, raise_on=None, pytest_seconds=0):
        self.files: dict[str, bytes] = {}
        # Last content written to each path, including paths deleted since.
        self.ever_written: dict[str, bytes] = {}
        self.commands: list[str] = []
        self.files_at_pytest: list[set[str]] = []
        self.pytest_writes_report = pytest_writes_report
        self.raise_on = raise_on
        # Wall-clock seconds the pytest run takes. Past the run's timeout the
        # sandbox kills it and answers rc 124, which the SDK flags as timed_out.
        self.pytest_seconds = pytest_seconds
        self.report = {"tests": [
            {"nodeid": "tests/test_outputs.py::test_output_file_exists", "outcome": "passed"},
            {"nodeid": "tests/test_outputs.py::test_output_contents", "outcome": "failed"},
        ]}

    def __del__(self):
        pass

    def _ensure_alive(self):
        pass

    async def start(self):
        pass

    async def stop(self):
        pass

    def _write(self, path, data):
        self.files[path] = data
        self.ever_written[path] = data

    def _rm(self, root):
        for path in list(self.files):
            if path == root or path.startswith(root.rstrip("/") + "/"):
                del self.files[path]

    async def run(self, cmd, timeout=300, max_bytes=50_000, sanitise=True):
        self.commands.append(cmd)
        await _yield_to_loop(0)
        if self.raise_on and self.raise_on in cmd:
            raise RuntimeError("sandbox connection lost")
        output, rc = "", 0
        if m := re.fullmatch(r"mkdir -p (\S+) && : > (\S+)", cmd):
            self._write(m[2], b"")
        elif m := re.fullmatch(r"printf '%s' '([A-Za-z0-9+/=]*)' >> (\S+)", cmd):
            self._write(m[2], self.files[m[2]] + m[1].encode())
        elif m := re.match(r"set -o pipefail; base64 -d (\S+) \| tar -xzmf - -C (\S+) ", cmd):
            payload = base64.b64decode(self.files.pop(m[1]))
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
                for member in tar.getmembers():
                    if member.isfile():
                        self._write(f"{shlex.split(m[2])[0]}/{member.name}", tar.extractfile(member).read())
        elif m := re.fullmatch(r"echo '([A-Za-z0-9+/=]*)' \| base64 -d > (\S+)", cmd):
            self._write(shlex.split(m[2])[0], base64.b64decode(m[1]))
        elif "--json-report-file=" in cmd:
            self.files_at_pytest.append(set(self.files))
            report_path = re.search(r"--json-report-file=(\S+)", cmd)[1]
            if timeout is not None and self.pytest_seconds > timeout:
                return RunResult(output="", return_code=124, timed_out=True)
            if self.pytest_writes_report:
                self._write(report_path, json.dumps(self.report).encode())
            else:
                output, rc = "/usr/bin/python3.12: No module named pytest", 1
        elif m := re.fullmatch(r"test -f (\S+)", cmd):
            rc = 0 if m[1] in self.files else 1
        elif m := re.fullmatch(r"base64 (\S+)", cmd):
            path = shlex.split(m[1])[0]
            if path in self.files:
                output = base64.b64encode(self.files[path]).decode()
            else:
                output, rc = f"base64: {path}: No such file or directory", 1
        elif cmd.startswith("rm -rf "):
            for path in shlex.split(cmd)[2:]:
                self._rm(path)
        return RunResult(output=output, return_code=rc)


@pytest.fixture
def task_dir(tmp_path, monkeypatch):
    root = tmp_path / "Dataset"
    task = root / str(TASK_ID)
    (task / "tests").mkdir(parents=True)
    (task / "config").mkdir()
    (task / "Dockerfile").write_text("FROM ubuntu:24.04\nWORKDIR /app\nCOPY users.csv /app/users.csv\nCOPY config/ /app/config/\n")
    (task / "users.csv").write_text("name\nalice\n")
    (task / "config" / "app.conf").write_text("port=80\n")
    (task / "tests" / "test_outputs.py").write_text("def test_output_file_exists():\n    pass\n")
    for name in ("solution.sh", "run-tests.sh", "task.yaml", "draft_spec.md", "docker-compose.yaml"):
        (task / name).write_text(f"reference {name}\n")
    (task / "weights.json").write_text(json.dumps(WEIGHTS))

    monkeypatch.setattr(seta, "DATASET_DIR", root, raising=False)
    monkeypatch.setitem(seta.TASKS, TASK_ID, {
        "task_id": TASK_ID, "instruction": "Do the thing.", "difficulty": "easy",
        "category": "software-engineering", "tags": [], "weights": WEIGHTS,
    })
    monkeypatch.setattr(seta, "TASK_IMAGES", {})

    async def no_sleep(_):
        pass
    monkeypatch.setattr(seta.asyncio, "sleep", no_sleep)
    return task


def make_env(sandbox):
    env = SETAEnv(task_spec={"task_id": TASK_ID}, secrets={"api_key": "test-key"})
    env.sandbox = sandbox
    return env


def test_memory_heavy_tasks_get_a_larger_sandbox(task_dir):
    assert make_env(FakeSandbox()).sandbox_settings.machine_size == "0.5:1"
    big = SETAEnv(task_spec={"task_id": 1106}, secrets={"api_key": "test-key"})
    assert big.sandbox_settings.machine_size == "1:4"
    assert set(seta.MACHINE_SIZE_OVERRIDES) <= set(seta.TASKS)


def test_agent_sandbox_does_not_mount_task_data(task_dir):
    env = make_env(FakeSandbox())
    assert env.sandbox_settings.bucket_config is None


def test_dockerfile_copy_reads_from_staging_dir():
    script = dockerfile_to_bash("FROM x\nCOPY data/ /data/in/\n", 0)
    assert "cp -r /tmp/seta-task-files/data/ /data/in/" in script
    assert "/orwd_data" not in script


@run_async
async def test_setup_stages_task_inputs_but_no_reference_files(task_dir):
    sandbox = FakeSandbox()
    env = make_env(sandbox)
    await env.setup()

    assert "/tmp/seta-task-files/users.csv" in sandbox.ever_written
    assert "/tmp/seta-task-files/config/app.conf" in sandbox.ever_written
    assert b"cp -r /tmp/seta-task-files/users.csv /app/users.csv" in sandbox.ever_written["/tmp/setup.sh"]
    assert "bash /tmp/setup.sh" in sandbox.commands
    for path in sandbox.ever_written:
        assert not path.endswith(REFERENCE_NAMES), path
    # Staged inputs and the setup script are gone before the agent starts.
    assert not [p for p in sandbox.files if p.startswith("/tmp/seta-task-files") or p == "/tmp/setup.sh"]


@run_async
async def test_tests_reach_sandbox_only_for_pytest_and_are_removed(task_dir):
    sandbox = FakeSandbox()
    env = make_env(sandbox)
    await env.setup()
    assert not [p for p in sandbox.files if p.endswith("test_outputs.py")]

    result = await env.submit_solution(EmptyInput())

    assert result.finished and result.reward == pytest.approx(0.4)
    (during,) = sandbox.files_at_pytest
    assert [p for p in during if p.endswith("/tests/test_outputs.py")]
    assert not [p for p in sandbox.files if p.endswith("test_outputs.py") or p.endswith("report.json")]
    for path in sandbox.ever_written:
        assert not path.endswith(("solution.sh", "run-tests.sh", "weights.json", "task.yaml", "draft_spec.md")), path
    # Top-level task inputs are refreshed into /app for the tests.
    assert sandbox.files["/app/users.csv"] == b"name\nalice\n"


@run_async
async def test_suite_that_cannot_run_scores_zero_without_raising(task_dir):
    sandbox = FakeSandbox(pytest_writes_report=False)
    env = make_env(sandbox)

    result = await env.submit_solution(EmptyInput())

    assert result.reward == 0.0 and result.finished
    assert result.metadata["tests_ran"] is False and result.metadata["timed_out"] is False
    assert "No module named pytest" not in result.blocks[0].text
    assert sum("--json-report-file=" in c for c in sandbox.commands) == 1

    repeat = await env.submit_solution(EmptyInput())
    assert repeat.reward == seta.REPEAT_SUBMISSION_PENALTY


@run_async
async def test_suite_slower_than_sdk_default_timeout_is_graded(task_dir):
    env = make_env(FakeSandbox(pytest_seconds=400))

    result = await env.submit_solution(EmptyInput())

    assert result.metadata["tests_ran"] is True
    assert result.reward == pytest.approx(0.4)


@run_async
async def test_suite_timeout_scores_zero_and_says_so(task_dir):
    sandbox = FakeSandbox(pytest_seconds=seta.GRADER_TIMEOUT_S + 1)
    env = make_env(sandbox)

    result = await env.submit_solution(EmptyInput())

    assert result.reward == 0.0 and result.finished
    assert result.metadata["tests_ran"] is False and result.metadata["timed_out"] is True
    assert f"did not finish within {seta.GRADER_TIMEOUT_S} s" in result.blocks[0].text
    assert sum("--json-report-file=" in c for c in sandbox.commands) == 1
    assert not [p for p in sandbox.files if p.startswith("/tmp/.grader-")]


@run_async
async def test_planted_report_is_not_read_when_pytest_cannot_run(task_dir):
    sandbox = FakeSandbox(pytest_writes_report=False)
    sandbox._write("/app/report.json", json.dumps({"tests": [
        {"nodeid": f"tests/test_outputs.py::{name}", "outcome": "passed"} for name in WEIGHTS
    ]}).encode())
    env = make_env(sandbox)

    result = await env.submit_solution(EmptyInput())

    assert result.reward == 0.0


@run_async
async def test_result_reports_counts_not_test_names_or_weights(task_dir):
    env = make_env(FakeSandbox())

    result = await env.submit_solution(EmptyInput())

    visible = result.blocks[0].text + json.dumps(result.metadata)
    for name in WEIGHTS:
        assert name not in visible
    assert "weight" not in visible.lower()
    assert "Passed: 1/2" in result.blocks[0].text
    assert result.metadata["passed_count"] == 1 and result.metadata["test_count"] == 2
    assert result.metadata["score"] == pytest.approx(0.4)


@run_async
async def test_concurrent_submits_grade_once(task_dir):
    sandbox = FakeSandbox()
    env = make_env(sandbox)

    results = await asyncio.gather(env.submit_solution(EmptyInput()), env.submit_solution(EmptyInput()))

    assert sum("--json-report-file=" in c for c in sandbox.commands) == 1
    assert sorted(r.reward for r in results) == [seta.REPEAT_SUBMISSION_PENALTY, pytest.approx(0.4)]


@run_async
async def test_sandbox_failure_raises_and_keeps_the_attempt(task_dir):
    sandbox = FakeSandbox(raise_on="--json-report-file=")
    env = make_env(sandbox)

    with pytest.raises(RuntimeError, match="sandbox connection lost"):
        await env.submit_solution(EmptyInput())
    assert env.submitted == 0
    assert sum("--json-report-file=" in c for c in sandbox.commands) == 3


@run_async
async def test_upload_splits_large_payload_into_chunks(task_dir, monkeypatch):
    monkeypatch.setattr(seta, "UPLOAD_CHUNK_CHARS", 4096)
    blob = random.Random(0).randbytes(20_000)
    (task_dir / "big.bin").write_bytes(blob)
    sandbox = FakeSandbox()
    env = make_env(sandbox)

    assert await env._upload_files({"big.bin": task_dir / "big.bin"}, "/data") is True

    assert sandbox.files["/data/big.bin"] == blob
    chunks = [c for c in sandbox.commands if c.startswith("printf ")]
    assert len(chunks) > 1
    assert all(len(c) < 4096 + 200 for c in chunks)
