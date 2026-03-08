# Data Upload Requirements for SETA Environment

## Overview

The SETA environment requires the complete Dataset/ directory from the seta-env repository to be uploaded to OpenReward cloud storage.

## Directory Structure

Upload the following structure to your OpenReward namespace at https://openreward.ai:

```
/orwd_data/seta/
└── Dataset/
    ├── 0/
    │   ├── task.yaml
    │   ├── weights.json
    │   ├── tests/test_outputs.py
    │   ├── run-tests.sh
    │   ├── Dockerfile (reference only, not used at runtime)
    │   └── [task-specific data files: CSVs, JSONs, archives, etc.]
    ├── 1/
    │   ├── task.yaml
    │   ├── weights.json
    │   ├── tests/test_outputs.py
    │   ├── run-tests.sh
    │   └── [task-specific data files]
    ├── 2/
    │   └── ... (same structure)
    ...
    └── 1375/
        └── ... (same structure)
```

## Upload Instructions

### Step 1: Clone the seta-env repository

```bash
git clone https://github.com/camel-ai/seta-env
cd seta-env
```

### Step 2: Create/Access EnvCommons Namespace

1. Go to https://openreward.ai
2. Navigate to "Namespaces"
3. Create or access the "EnvCommons" namespace

### Step 3: Upload Dataset Directory

Upload the `Dataset/` directory to the path: `seta/Dataset/`

The final path should be: `/orwd_data/seta/Dataset/`

### Step 4: Verify Upload

Check that the following files exist:
- `/orwd_data/seta/Dataset/0/task.yaml`
- `/orwd_data/seta/Dataset/0/weights.json`
- `/orwd_data/seta/Dataset/0/tests/test_outputs.py`
- All 1376 task directories are present (0-1375)

## Required Files Per Task

Each task directory (0-1375) must contain:

1. **task.yaml** (required)
   - Task specification with instruction, difficulty, category, timeouts, metadata

2. **weights.json** (required)
   - Test scoring weights (test_name → float)
   - Weights should sum to 1.0

3. **tests/test_outputs.py** (required)
   - Pytest test suite (typically 3-5 test functions)
   - Tests validate the solution

4. **run-tests.sh** (required)
   - Test execution script
   - Installs pytest and runs tests

5. **Data files** (task-specific)
   - CSVs, JSONs, archives, config files, etc.
   - Varies by task

6. **Dockerfile** (reference only)
   - Not used at runtime (for now)
   - User will build task-specific images later

## Total Size

Approximately **58 MB** for all 1376 tasks.

Individual tasks range from 1 KB to 5 MB depending on data files.

## How the Environment Uses the Data

The SETA environment:

1. **Mounts** `/orwd_data/seta/` (read-only) in the sandbox
2. **Loads** task metadata (task.yaml, weights.json) at module import time
3. **Copies** test files and data files to `/app/` in sandbox at runtime
4. **Executes** pytest tests when `submit_solution` is called

## File Access Pattern

```python
# In environment code:
task_yaml = Path("/orwd_data/seta/Dataset/{task_id}/task.yaml")
weights_json = Path("/orwd_data/seta/Dataset/{task_id}/weights.json")

# In sandbox at runtime:
cp /orwd_data/seta/Dataset/{task_id}/tests/test_outputs.py /app/tests/
cp /orwd_data/seta/Dataset/{task_id}/data_file.csv /app/
```

## Troubleshooting

### Missing Files

If tasks fail to load:
- Check that all 1376 directories exist
- Verify task.yaml and weights.json are present in each directory
- Confirm file permissions are readable

### Path Issues

If data files are not found:
- Ensure Dataset/ is uploaded to `seta/Dataset/` (not just `Dataset/`)
- Verify the mount path in SandboxSettings points to `/orwd_data`
- Check the `only_dir` parameter is set to `"seta"`

### Large Files

If upload is slow:
- The total dataset is ~50-100 MB, which should upload quickly
- If individual tasks have large files, consider compressing them
- Verify network connection is stable

## Support

For issues with data upload or environment configuration:
- Check OpenReward documentation: https://docs.openreward.org/
- Review SETA repository: https://github.com/camel-ai/seta-env
- Open an issue on the environment repository: https://github.com/EnvCommons/seta/issues
