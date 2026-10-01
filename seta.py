from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import secrets as secrets_lib
import shlex
import tarfile
from pathlib import Path
from typing import Any, List, Dict, Optional

from openreward.environments import Environment, JSONObject, TextBlock, ToolOutput, tool, upload_text
from openreward.toolsets import CLIToolset
from openreward import SandboxSettings, AsyncOpenReward

from pydantic import BaseModel

from constants import ENV_PATH


SETA_BASE_IMAGE = (
    "generalreasoning/seta-base"
    "@sha256:369515ae30815448a3b2e0189c5ef3df40786edc2a611f6bb1d3bc6b5636c363"
)
PER_TASK_IMAGE_PREFIX = "generalreasoning/eigent-seta"

# Per-task data on the env server: DATASET_DIR/<task_id>/ holds the task inputs
# next to the grader files (tests/, solution.sh, ...). The agent sandbox does not
# mount it: setup copies in only the task inputs, and submit_solution copies in
# the test suite just for the duration of the pytest run.
DATASET_DIR = ENV_PATH / "Dataset"
TESTS_SUBDIR = "tests"
# Top-level files of a task dir that are reference/grader material, never task inputs.
REFERENCE_FILES = frozenset({
    "Dockerfile",
    "docker-compose.yaml",
    "draft_spec.md",
    "run-tests.sh",
    "solution.sh",
    "task.yaml",
    "weights.json",
})
# Sandbox dir where Dockerfile COPY sources are staged during setup; removed after.
SETUP_STAGING_DIR = "/tmp/seta-task-files"
# The base image installs pytest and pytest-json-report for python3.12 only, and
# some tasks repoint /usr/bin/python3 (the pytest script's shebang), so the
# grader names its interpreter explicitly. -P keeps the cwd off sys.path, as
# when pytest runs as a script.
GRADER_PYTHON = "/usr/bin/python3.12"
# base64 characters per sandbox command; the sandbox /run endpoint rejects
# request bodies over ~4 MiB.
UPLOAD_CHUNK_CHARS = 1 << 20
GRADER_PYTEST_INI = b"[pytest]\n"


def load_task_images() -> dict[str, str]:
    """Load task_images.json: {task_id_str: task_image_digest}.

    Returns an empty dict if the file is missing. Tasks without an entry
    fall back to the seta-base image + runtime dockerfile_to_bash setup.
    """
    path = ENV_PATH / "task_images.json"
    if not path.exists():
        return {}
    with open(path) as f:
        raw = json.load(f)
    return {str(k): v["task_image_digest"] for k, v in raw.items() if v.get("task_image_digest")}


def load_tasks() -> dict[int, dict]:
    """
    Load all SETA tasks from pre-built task_index.json.

    Run build_task_index.py to regenerate the index if tasks change.

    Returns:
        Dict mapping task_id to task dict with structure:
        {
            "task_id": int,
            "instruction": str,
            "difficulty": str,
            "category": str,
            "tags": list[str],
            "weights": dict[str, float],  # test_name -> weight
        }
    """
    index_path = ENV_PATH / "task_index.json"
    with open(index_path, "r") as f:
        raw = json.load(f)
    return {int(k): v for k, v in raw.items()}


# Load tasks at module import time
TASKS = load_tasks()
TASK_IMAGES = load_task_images()


def dockerfile_to_bash(dockerfile_content: str, task_id: int) -> str:
    """
    Convert Dockerfile to bash script by stripping FROM and transforming instructions.

    Args:
        dockerfile_content: Raw Dockerfile text
        task_id: Task ID for COPY path adjustments

    Returns:
        Bash script ready to execute
    """
    lines = dockerfile_content.split('\n')
    bash_lines = []

    # Skip until after FROM line
    from_found = False
    for line in lines:
        if not from_found:
            if line.strip().startswith('FROM '):
                from_found = True
            continue

        # Transform Dockerfile instructions to bash
        stripped = line.strip()

        # Keep comments and empty lines as-is
        if not stripped or stripped.startswith('#'):
            bash_lines.append(line)
            continue

        # Transform instructions (simple string replacements)
        if stripped.startswith('RUN '):
            # Strip RUN prefix - the rest is already bash
            bash_lines.append(stripped[4:])
        elif stripped.startswith('WORKDIR '):
            # Convert to mkdir + cd
            path = stripped[8:].strip()
            bash_lines.append(f'mkdir -p {path} && cd {path}')
        elif stripped.startswith('COPY '):
            # COPY sources are staged in SETUP_STAGING_DIR before the script runs
            copy_args = stripped[5:].strip().split()
            if len(copy_args) >= 2:
                src = copy_args[0]
                dst = copy_args[-1]  # Last argument is destination
                bash_lines.append(f'cp -r {SETUP_STAGING_DIR}/{src} {dst}')
        elif stripped.startswith('ENV '):
            # Convert to export
            env_def = stripped[4:].strip()
            bash_lines.append(f'export {env_def}')
        else:
            # Keep line as-is (handles continuations automatically)
            bash_lines.append(line)

    return '\n'.join(bash_lines)


def task_input_files(task_dir: Path) -> dict[str, Path]:
    """Files and dirs of a task that may be placed in the agent sandbox.

    Everything in the task dir except the tests/ dir and the top-level
    REFERENCE_FILES, keyed by path relative to task_dir.
    """
    entries: dict[str, Path] = {}
    for path in sorted(task_dir.rglob("*")):
        rel = path.relative_to(task_dir)
        if rel.parts[0] == TESTS_SUBDIR or (len(rel.parts) == 1 and rel.name in REFERENCE_FILES):
            continue
        entries[rel.as_posix()] = path
    return entries


# Reward for a submission made after the task has already been graded. Negative
# so repeat submissions are actively discouraged, not merely left unscored.
REPEAT_SUBMISSION_PENALTY = -0.1


class EmptyInput(BaseModel):
    """Empty params for submit_solution tool."""
    pass


class SETAEnv(Environment):
    """
    SETA (Scaling Environments for Terminal Agents) environment.

    Terminal-based coding and system administration tasks with automated
    pytest validation. Agents use CLI tools (bash, read, write, etc.) to
    complete tasks, then submit for scoring.
    """

    # 9-tool sandboxed CLI surface provided by the SDK. The class attribute
    # makes the framework auto-instantiate the toolset against self.sandbox.
    toolsets = [CLIToolset]

    @classmethod
    def list_splits(cls) -> list[str]:
        """Return available splits. All tasks in 'train' split."""
        return ["train"]

    @classmethod
    def list_tasks(cls, split: str) -> list[JSONObject]:
        """
        Return task specifications for requested split.

        Args:
            split: Only "train" is supported

        Returns:
            List of task specs with metadata
        """
        if split != "train":
            return []

        return [
            {
                "task_id": task["task_id"],
                "difficulty": task["difficulty"],
                "category": task["category"],
                "tags": task["tags"],
            }
            for task in TASKS.values()
        ]

    def __init__(self, task_spec: JSONObject, secrets: dict[str, str] = {}) -> None:
        """
        Initialize SETA environment for a specific task.

        Args:
            task_spec: Task specification with task_id
            secrets: Must contain 'api_key' for sandbox access
        """
        super().__init__(task_spec)

        # Scored submissions this session. submit_solution runs the test suite and
        # reports passed/total back, so an uncapped tool is a free CI loop against
        # the graded tests: edit, submit, read the score, edit again.
        self.submitted = 0
        # Concurrent submit_solution calls share one sandbox and one attempt.
        self._submit_lock = asyncio.Lock()

        self.task_id = int(task_spec["task_id"])
        if self.task_id not in TASKS:
            raise ValueError(f"Task ID {self.task_id} not found in loaded tasks")
        self.task_data = TASKS[self.task_id]
        self.task_dir = DATASET_DIR / str(self.task_id)

        # Validate API key
        if not secrets.get("api_key"):
            raise ValueError("OpenReward API key required in secrets")

        # Prefer the per-task image when available (built by
        # scripts/build_task_images.py and recorded in task_images.json).
        # Falls back to seta-base + runtime dockerfile_to_bash setup when
        # no entry exists, so newly-added tasks keep working until they
        # have been built.
        self._task_image_digest: Optional[str] = TASK_IMAGES.get(str(self.task_id))
        if self._task_image_digest is not None:
            image = f"{PER_TASK_IMAGE_PREFIX}@{self._task_image_digest}"
        else:
            image = SETA_BASE_IMAGE

        self.sandbox_settings = SandboxSettings(
            environment="Eigent/SETA",
            image=image,
            machine_size="0.5:1",
            block_network=False,
        )

        or_client = AsyncOpenReward(api_key=secrets.get("api_key"))
        self.sandbox = or_client.sandbox(self.sandbox_settings)

    async def setup(self) -> None:
        """
        Start sandbox and execute task-specific Dockerfile setup.

        When the task has a pre-built image (image_sha.txt resolved at init),
        the image already contains the Dockerfile's installs and nothing
        more is needed. Otherwise, fall back to converting the Dockerfile
        to a bash script and executing it inside a seta-base sandbox.
        """
        await self.sandbox.start()

        if self._task_image_digest is not None:
            print(
                f"[SETUP SUCCESS] Task {self.task_id} on pre-built image "
                f"@{self._task_image_digest[:19]}..."
            )
            return

        try:
            # Read the Dockerfile from the env server's copy of the task data
            dockerfile_text = (self.task_dir / "Dockerfile").read_text(encoding="utf-8")

            # Convert to bash script
            bash_script = dockerfile_to_bash(dockerfile_text, self.task_id)
            #print(f"[SETUP] Generated bash script ({len(bash_script)} bytes)")

            # Stage the task inputs the Dockerfile COPYs from (never tests or reference files)
            inputs = task_input_files(self.task_dir)
            if inputs and not await self._upload_files(inputs, SETUP_STAGING_DIR):
                print(f"[SETUP WARNING] Could not stage task inputs for task {self.task_id}")

            # Upload script to sandbox
            await upload_text(self.sandbox, "/tmp/setup.sh", bash_script)

            # Execute the script
            #print(f"[SETUP] Executing setup script...")
            output, exit_code = await self.sandbox.run("bash /tmp/setup.sh")

            # Print output
            #print(f"[SETUP OUTPUT]\n{output}")

            if exit_code != 0:
                print(f"[SETUP WARNING] Script exited with code {exit_code}")
            else:
                print(f"[SETUP SUCCESS] Task {self.task_id} setup completed")

            await self.sandbox.run(f"rm -rf {SETUP_STAGING_DIR} /tmp/setup.sh")

        except Exception as e:
            print(f"[SETUP ERROR] Failed to setup task {self.task_id}: {e}")
            # Don't raise - allow task to continue

    async def teardown(self) -> None:
        await self.sandbox.stop()

    async def get_prompt(self) -> List[TextBlock]:
        """
        Generate task prompt for agent.

        Returns:
            Task instruction with context and guidance.
        """
        instruction = self.task_data["instruction"]

        return [TextBlock(text=instruction + "\n\n" + "When finished, call `submit_solution` to run the test suite and get your score.")]
    
    @tool
    async def submit_solution(self, params: EmptyInput) -> ToolOutput:
        """
        Submit solution and run test suite.

        Executes pytest tests in sandbox, calculates weighted score,
        and returns the result.

        Returns:
            ToolOutput with:
            - blocks: Pass count and score
            - metadata: Score and counts
            - reward: Final score (0.0 to 1.0)
            - finished: True (ends episode)
        """
        async with self._submit_lock:
            return await self._submit()

    async def _submit(self) -> ToolOutput:
        if self.submitted > 0:
            return ToolOutput(
                blocks=[TextBlock(text="A solution has already been submitted for this task. "
                                       "This episode is over: it is not re-scored, and repeat "
                                       "submissions are penalised (reward -0.1).")],
                metadata={"already_submitted": True, "submission_count": self.submitted},
                reward=REPEAT_SUBMISSION_PENALTY,
                finished=True,
            )

        weights = self.task_data["weights"]

        # Run the test suite in the sandbox and parse the JSON report. The sandbox
        # round-trip is the grader's flaky external op; _run_tests_with_retry retries
        # transient failures and then *raises* on a persistent sandbox failure so the
        # SDK turns it into ToolFailed -> a clean terminal. A failing solution still
        # produces a report (pytest-json-report records failures/collection errors)
        # and gets a real low score below. When the sandbox answers but the suite
        # cannot run at all (no report, e.g. the solution broke the interpreter), the
        # attempt is scored 0.
        report = await self._run_tests_with_retry()
        self.submitted += 1

        if report is None:
            return ToolOutput(
                blocks=[TextBlock(text="The test suite could not run in the sandbox; this submission is scored 0.")],
                metadata={
                    "task_id": self.task_id,
                    "score": 0.0,
                    "passed_count": 0,
                    "test_count": len(weights),
                    "tests_ran": False,
                },
                reward=0.0,
                finished=True,
            )

        # Parse test results
        passed_tests = set()

        for test in report.get("tests", []):
            # Extract test function name from nodeid
            # Example nodeid: "tests/test_outputs.py::test_user_accounts_created"
            test_name = test["nodeid"].split("::")[-1]

            if test["outcome"] == "passed":
                passed_tests.add(test_name)

        # Calculate weighted score
        total_score = 0.0
        passed_count = 0

        for test_name, weight in weights.items():
            if test_name in passed_tests:
                total_score += weight
                passed_count += 1

        # Normalize score to 0.0-1.0 range
        total_weight = sum(weights.values())
        if total_weight > 0:
            total_score = total_score / total_weight

        # Counts and score only: test names and weights describe the hidden suite.
        summary_text = f"""
Test Execution Complete
========================

Task ID: {self.task_id}
Category: {self.task_data.get('category', 'unknown')}
Difficulty: {self.task_data.get('difficulty', 'unknown')}

Passed: {passed_count}/{len(weights)}
Final Score: {total_score:.2%}
"""

        return ToolOutput(
            blocks=[TextBlock(text=summary_text)],
            metadata={
                "task_id": self.task_id,
                "score": total_score,
                "passed_count": passed_count,
                "test_count": len(weights),
                "tests_ran": True,
            },
            reward=total_score,
            finished=True
        )

    async def _upload_files(self, files: dict[str, Path | bytes], dest_dir: str) -> bool:
        """Copy ``files`` ({relative path: server path or content}) into ``dest_dir`` in the sandbox.

        The files travel as one gzipped tar, base64-encoded in chunks that fit the
        sandbox request body limit. Returns False if a shell step exits non-zero
        (the sandbox answers but its filesystem or tools are broken); sandbox and
        transport errors propagate.
        """
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for arcname, source in files.items():
                if isinstance(source, bytes):
                    info = tarfile.TarInfo(arcname)
                    info.size = len(source)
                    info.mode = 0o644
                    tar.addfile(info, io.BytesIO(source))
                else:
                    tar.add(source, arcname=arcname, recursive=False)
        payload = base64.b64encode(buf.getvalue()).decode("ascii")

        dest = shlex.quote(dest_dir)
        b64_path = f"/tmp/.upload-{secrets_lib.token_hex(8)}.b64"
        result = await self.sandbox.run(f"mkdir -p {dest} && : > {b64_path}")
        if result.return_code != 0:
            return False
        for start in range(0, len(payload), UPLOAD_CHUNK_CHARS):
            chunk = payload[start:start + UPLOAD_CHUNK_CHARS]
            result = await self.sandbox.run(f"printf '%s' '{chunk}' >> {b64_path}")
            if result.return_code != 0:
                return False
        # -m and --no-same-* give the extracted files the ownership, mode and mtime
        # a plain `cp -r` by root would.
        result = await self.sandbox.run(
            f"set -o pipefail; base64 -d {b64_path} | tar -xzmf - -C {dest} "
            f"--no-same-owner --no-same-permissions; rc=$?; rm -f {b64_path}; exit $rc"
        )
        return result.return_code == 0

    async def _run_tests_once(self) -> Optional[dict]:
        """One grading pass. Returns the pytest JSON report, or None if the suite
        could not produce one."""
        # A fresh, unguessable dir per pass: the agent cannot pre-place files in it,
        # and it is removed as soon as the report is read.
        grader_dir = f"/tmp/.grader-{secrets_lib.token_hex(8)}"
        report_path = f"{grader_dir}/report.json"
        grader_files = {
            f"{TESTS_SUBDIR}/test_outputs.py": self.task_dir / TESTS_SUBDIR / "test_outputs.py",
            # The grader's own pytest.ini pins rootdir to grader_dir, so ini and
            # conftest files elsewhere in the sandbox do not apply.
            "pytest.ini": GRADER_PYTEST_INI,
        }
        # Top-level task inputs are refreshed into /app for the tests.
        data_files = {
            path.name: path
            for path in sorted(self.task_dir.iterdir())
            if path.is_file() and path.name not in REFERENCE_FILES
        }
        try:
            if not await self._upload_files(grader_files, grader_dir):
                return None
            if data_files and not await self._upload_files(data_files, "/app"):
                return None
            await self.sandbox.run(
                f"mkdir -p /app && cd /app && {GRADER_PYTHON} -P -m pytest "
                f"{grader_dir}/{TESTS_SUBDIR}/test_outputs.py -rA "
                f"--json-report --json-report-file={report_path}"
            )
            if (await self.sandbox.run(f"test -f {report_path}")).return_code != 0:
                return None
            report_content = await self.sandbox.download(report_path)
        finally:
            # Best effort: a lost sandbox already fails the pass with its own error.
            with contextlib.suppress(Exception):
                await self.sandbox.run(f"rm -rf {grader_dir}")
        try:
            return json.loads(report_content)
        except ValueError:
            return None

    async def _run_tests_with_retry(self, *, max_attempts: int = 3) -> Optional[dict]:
        """Run the pytest suite in the sandbox and return the parsed JSON report.

        Returns None when the sandbox answers but the suite produces no readable
        report (scored 0 by the caller). Sandbox/transport failures are retried;
        after ``max_attempts`` the last exception is re-raised so the tool fails
        loudly (the SDK turns it into ToolFailed -> terminal) instead of
        fabricating a score for a grader outage.
        """
        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            try:
                return await self._run_tests_once()
            except Exception as e:
                last_exc = e
                if attempt < max_attempts - 1:
                    wait = min(2 ** attempt, 30)
                    print(f"SETA GRADER ERROR: {type(e).__name__}: {e} | retry in {wait}s (attempt {attempt + 1}/{max_attempts})")
                    await asyncio.sleep(wait)
        assert last_exc is not None
        raise last_exc
