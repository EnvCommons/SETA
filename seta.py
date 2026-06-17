from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, List, Dict, Optional

from openreward.environments import Environment, JSONObject, TextBlock, ToolOutput, tool, upload_text
from openreward.toolsets import CLIToolset
from openreward import SandboxSettings, SandboxBucketConfig, AsyncOpenReward

from pydantic import BaseModel

from constants import ENV_PATH


SETA_BASE_IMAGE = (
    "generalreasoning/seta-base"
    "@sha256:369515ae30815448a3b2e0189c5ef3df40786edc2a611f6bb1d3bc6b5636c363"
)
PER_TASK_IMAGE_PREFIX = "generalreasoning/eigent-seta"


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
            # Adjust COPY source paths to point to /orwd_data
            copy_args = stripped[5:].strip().split()
            if len(copy_args) >= 2:
                src = copy_args[0]
                dst = copy_args[-1]  # Last argument is destination
                bash_lines.append(f'cp -r /orwd_data/{src} {dst}')
        elif stripped.startswith('ENV '):
            # Convert to export
            env_def = stripped[4:].strip()
            bash_lines.append(f'export {env_def}')
        else:
            # Keep line as-is (handles continuations automatically)
            bash_lines.append(line)

    return '\n'.join(bash_lines)


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

        self.task_id = int(task_spec["task_id"])
        if self.task_id not in TASKS:
            raise ValueError(f"Task ID {self.task_id} not found in loaded tasks")
        self.task_data = TASKS[self.task_id]

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
            bucket_config=SandboxBucketConfig(
                mount_path="/orwd_data",
                read_only=True,
                only_dir=f"Dataset/{self.task_id}"
            )
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
            # Download Dockerfile (task directory mounted at /orwd_data via only_dir)
            dockerfile_path = "/orwd_data/Dockerfile"
            #print(f"[SETUP] Reading Dockerfile: {dockerfile_path}")

            dockerfile_bytes = await self.sandbox.download(dockerfile_path)
            dockerfile_text = dockerfile_bytes.decode('utf-8')

            # Convert to bash script
            bash_script = dockerfile_to_bash(dockerfile_text, self.task_id)
            #print(f"[SETUP] Generated bash script ({len(bash_script)} bytes)")

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

            # Cleanup: Delete metadata files that shouldn't be visible to agent
            cleanup_files = [
                "docker-compose.yaml",
                "Dockerfile",
                "draft_spec.md",
                "solution.sh",
                "task.yaml"
            ]

            #print(f"[SETUP] Cleaning up metadata files...")
            for filename in cleanup_files:
                file_path = f"/orwd_data/{filename}"
                cleanup_output, cleanup_code = await self.sandbox.run(f"rm -f {file_path}")
                if cleanup_code == 0:
                    print(f"[SETUP] Deleted {filename}")


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
        and returns detailed results.

        Returns:
            ToolOutput with:
            - blocks: Formatted test results and score
            - metadata: Structured test data
            - reward: Final score (0.0 to 1.0)
            - finished: True (ends episode)
        """
        # Run the test suite in the sandbox and parse the JSON report. The sandbox
        # round-trip is the grader's flaky external op; _run_tests_with_retry retries
        # transient failures and then *raises* on a persistent failure (sandbox dead,
        # pytest couldn't run/write the report) so the SDK turns it into ToolFailed ->
        # a clean terminal. We do NOT fabricate reward=0.0 for a grader failure — a
        # legitimately failing solution still produces a report (pytest-json-report
        # records failures/collection errors) and gets a real low score below.
        report = await self._run_tests_with_retry()

        # Parse test results
        passed_tests = set()
        failed_tests = set()

        for test in report.get("tests", []):
            # Extract test function name from nodeid
            # Example nodeid: "tests/test_outputs.py::test_user_accounts_created"
            test_name = test["nodeid"].split("::")[-1]

            if test["outcome"] == "passed":
                passed_tests.add(test_name)
            else:
                failed_tests.add(test_name)

        # Step 5: Calculate weighted score
        weights = self.task_data["weights"]
        total_score = 0.0

        for test_name, weight in weights.items():
            if test_name in passed_tests:
                total_score += weight

        # Normalize score to 0.0-1.0 range
        total_weight = sum(weights.values())
        if total_weight > 0:
            total_score = total_score / total_weight

        # Step 6: Format results for display
        test_details = []
        for test_name in weights.keys():
            status = "✓ PASSED" if test_name in passed_tests else "✗ FAILED"
            weight = weights[test_name]
            test_details.append(f"  {status} | {test_name} (weight: {weight:.2f})")

        summary_text = f"""
Test Execution Complete
========================

Task ID: {self.task_id}
Category: {self.task_data.get('category', 'unknown')}
Difficulty: {self.task_data.get('difficulty', 'unknown')}

Test Results:
{chr(10).join(test_details)}

Passed: {len(passed_tests)}/{len(weights)}
Final Score: {total_score:.2%}
"""

        return ToolOutput(
            blocks=[TextBlock(text=summary_text)],
            metadata={
                "task_id": self.task_id,
                "score": total_score,
                "passed_tests": list(passed_tests),
                "failed_tests": list(failed_tests),
                "test_count": len(weights),
                "weights": weights,
            },
            reward=total_score,
            finished=True
        )

    async def _run_tests_with_retry(self, *, max_attempts: int = 3) -> dict:
        """Run the pytest suite in the sandbox and return the parsed JSON report.

        The sandbox round-trip (mkdir/cp/pytest/download) is the grader's flaky
        external op. Transient failures are retried; after ``max_attempts`` the last
        exception is re-raised so the tool fails loudly (the SDK turns it into
        ToolFailed -> terminal) instead of swallowing a grader/sandbox failure into
        a fabricated reward=0.0. A genuinely failing solution is NOT an exception —
        pytest still writes report.json (recording failures/collection errors), so it
        returns normally here and is scored as a real low result by the caller.
        """
        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            try:
                # Ensure test directory structure exists and copy files. The task
                # directory is mounted at /orwd_data/ via the only_dir parameter.
                await self.sandbox.run("mkdir -p /app/tests")
                await self.sandbox.run("cp /orwd_data/tests/test_outputs.py /app/tests/")
                # Copy data files the task needs (excluding tests/ and Dockerfile).
                await self.sandbox.run(
                    "find /orwd_data/ -maxdepth 1 -type f ! -name 'Dockerfile' -exec cp {} /app/ \\;"
                )
                # Run pytest with a JSON report.
                await self.sandbox.run(
                    "cd /app && pytest tests/test_outputs.py -rA --json-report --json-report-file=/app/report.json"
                )
                report_content = await self.sandbox.download("/app/report.json")
                return json.loads(report_content)
            except Exception as e:
                last_exc = e
                if attempt < max_attempts - 1:
                    wait = min(2 ** attempt, 30)
                    print(f"SETA GRADER ERROR: {type(e).__name__}: {e} | retry in {wait}s (attempt {attempt + 1}/{max_attempts})")
                    await asyncio.sleep(wait)
        assert last_exc is not None
        raise last_exc
