from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Dict

from openreward.environments import JSONObject, TextBlock, ToolOutput, tool
from openreward import SandboxSettings, SandboxBucketConfig, AsyncOpenReward

from pydantic import BaseModel

from cli_environment import CLIEnvironment
from constants import ENV_PATH
from utils import upload_text


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


class SETAEnv(CLIEnvironment):
    """
    SETA (Scaling Environments for Terminal Agents) environment.

    Terminal-based coding and system administration tasks with automated
    pytest validation. Agents use CLI tools (bash, read, write, etc.) to
    complete tasks, then submit for scoring.
    """

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
        super().__init__(task_spec, secrets=secrets)

        self.task_id = int(task_spec["task_id"])
        if self.task_id not in TASKS:
            raise ValueError(f"Task ID {self.task_id} not found in loaded tasks")
        self.task_data = TASKS[self.task_id]

        # Validate API key
        if not secrets.get("api_key"):
            raise ValueError("OpenReward API key required in secrets")

        # Setup sandbox with base image
        # User will build task-specific images later, for now use base image
        self.sandbox_settings = SandboxSettings(
            environment="Eigent/SETA",
            image="generalreasoning/seta-base@sha256:369515ae30815448a3b2e0189c5ef3df40786edc2a611f6bb1d3bc6b5636c363",
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

        self.todos: List[Dict[str, Any]] = []

    async def setup(self) -> None:
        """
        Start sandbox and execute task-specific Dockerfile setup.

        Converts the Dockerfile to a bash script and executes it once.
        """
        await self.sandbox.start()

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
        try:

            # Step 2: Ensure test directory structure exists and copy files
            # The task directory is mounted at /orwd_data/ via only_dir parameter
            await self.sandbox.run("mkdir -p /app/tests")

            # Copy test file
            await self.sandbox.run(
                "cp /orwd_data/tests/test_outputs.py /app/tests/"
            )

            # Copy any data files that the task needs (excluding tests/ directory and Dockerfile)
            copy_result = await self.sandbox.run(
                "find /orwd_data/ -maxdepth 1 -type f ! -name 'Dockerfile' -exec cp {} /app/ \\;"
            )

            # Step 3: Run tests with JSON output
            test_timeout = self.task_data.get("max_test_timeout_sec", 60)

            # Run pytest directly with json-report
            test_result = await self.sandbox.run(
                "cd /app && pytest tests/test_outputs.py -rA --json-report --json-report-file=/app/report.json"
            )

            # Step 4: Download and parse JSON report
            try:
                report_content = await self.sandbox.download("/app/report.json")
                report = json.loads(report_content)
            except Exception as e:
                return ToolOutput(
                    blocks=[TextBlock(text=f"""
Test Report Not Found
=====================

Task ID: {self.task_id}
Error: Could not read test report - {str(e)}

This may indicate that pytest failed to run. Check test execution output above.
""")],
                    metadata={
                        "task_id": self.task_id,
                        "error": "report_not_found",
                        "details": str(e),
                    },
                    reward=0.0,
                    finished=True
                )

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

        except Exception as e:
            # Handle errors gracefully
            error_text = f"""
Test Execution Failed
=====================

Task ID: {self.task_id}
Error: {str(e)}

The test suite encountered an error. Please check:
1. Your solution is complete
2. All required files are in place
3. The sandbox environment is properly configured

You may try running the tests again with submit_solution.
"""

            return ToolOutput(
                blocks=[TextBlock(text=error_text)],
                metadata={
                    "task_id": self.task_id,
                    "error": str(e),
                    "score": 0.0,
                },
                reward=0.0,
                finished=True
            )
