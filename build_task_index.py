"""
One-time script to consolidate all task.yaml + weights.json files
into a single task_index.json for fast loading at server startup.

Usage:
    python build_task_index.py
"""

import json
import yaml
from pathlib import Path

DATASET_DIR = Path(__file__).parent / "Dataset"
OUTPUT_FILE = Path(__file__).parent / "task_index.json"
NUM_TASKS = 1376


def main():
    tasks = {}

    for task_id in range(NUM_TASKS):
        task_dir = DATASET_DIR / str(task_id)

        task_yaml_path = task_dir / "task.yaml"
        if not task_yaml_path.exists():
            continue

        with open(task_yaml_path, "r") as f:
            task_yaml = yaml.safe_load(f)

        weights_path = task_dir / "weights.json"
        if weights_path.exists():
            with open(weights_path, "r") as f:
                weights = json.load(f)
        else:
            weights = {}

        tasks[str(task_id)] = {
            "task_id": task_id,
            "instruction": task_yaml.get("instruction", ""),
            "difficulty": task_yaml.get("difficulty", "medium"),
            "category": task_yaml.get("category", "unknown"),
            "tags": task_yaml.get("tags", []),
            "weights": weights,
        }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(tasks, f)

    print(f"Wrote {len(tasks)} tasks to {OUTPUT_FILE} ({OUTPUT_FILE.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
