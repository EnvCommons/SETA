# SETA Environment

OpenReward environment for the **SETA (Synthetic Environment for Terminal Agents)** benchmark - 400 terminal-based coding and system administration tasks with automated pytest validation.

## Overview

SETA provides realistic Linux terminal tasks covering:
- **Software Engineering** (309 tasks)
- **System Administration** (79 tasks)
- **DevOps, Security, Networking** (12 tasks)

Each task is validated by a pytest test suite with weighted scoring.

## Environment Details

- **Base Class**: CLIEnvironment (provides bash, read, write, grep, glob, ls, edit, todo_write)
- **Tasks**: 400 (all in "train" split)
- **Difficulty Distribution**:
  - Easy: 1 task
  - Medium: 347 tasks
  - Hard: 52 tasks
- **Sandbox**: Required (generalreasoning/seta-ubuntu-24-04:20250624)
- **Scoring**: Weighted pytest tests (0.0 to 1.0)

## Installation

```bash
git clone https://github.com/EnvCommons/seta
cd seta
pip install -r requirements.txt
```

## Data Requirements

See [DATA_UPLOAD.md](DATA_UPLOAD.md) for instructions on uploading the SETA dataset to OpenReward cloud storage.

The environment requires the complete Dataset/ directory from https://github.com/camel-ai/seta-env to be uploaded to `/orwd_data/seta/Dataset/`.

## Usage

### Local Testing

```bash
# Start server
python server.py

# In another terminal, run test agent
export OPENAI_API_KEY=your_key
python test_agent.py
```

### Example Task

**Task 162: User Provisioning from CSV**

**Instruction**: Create user accounts based on data in users.csv. Each user should have their full name in the GECOS field. Use non-interactive commands for automation.

**Available Tools**:
- bash, read, write, edit, grep, glob, ls
- submit_solution (run tests and get score)

**Workflow**:
1. Read users.csv with the read tool
2. Write a bash script to create users
3. Execute the script with the bash tool
4. Call submit_solution to run tests

**Scoring**: Multiple pytest tests verify user creation, GECOS fields, etc. Each test has a specific weight, and the final score is the weighted sum of passed tests.

## Development

### Project Structure

```
seta/
├── seta.py              # Environment class
├── server.py            # Server wrapper
├── test_agent.py        # Testing script
├── constants.py         # Path configuration
├── requirements.txt
├── Dockerfile
├── README.md
└── DATA_UPLOAD.md
```

### Environment Architecture

The SETA environment extends `CLIEnvironment` to provide:

1. **Task Loading**: 400 tasks loaded from Dataset/ at module import
2. **Sandbox Configuration**: Each task runs in an isolated Docker container
3. **Test Execution**: pytest runs with JSON output for structured results
4. **Weighted Scoring**: Each test has a weight; final score is weighted sum

### Adding New Tasks

SETA tasks are loaded from `/orwd_data/seta/Dataset/`. To add tasks:

1. Follow the task structure from https://github.com/camel-ai/seta-env
2. Each task needs:
   - `task.yaml` (instruction, metadata, timeouts)
   - `weights.json` (test scoring weights)
   - `tests/test_outputs.py` (pytest test suite)
   - `run-tests.sh` (test execution script)
   - Data files (CSVs, JSONs, etc. as needed)
3. Upload new task directories to cloud storage
4. Restart environment server

## Task Categories

- **software-engineering**: Package management, archive extraction, file processing, automation scripts
- **system-administration**: User management, system configuration, cron jobs, GRUB setup
- **devops**: CI/CD, deployment automation, monitoring
- **security**: Permissions, access control, secure configurations
- **networking**: Network configuration, troubleshooting
- **debugging**: Log analysis, error diagnosis

## Testing Strategy

### Unit Testing

Test core functions:
```bash
python -c "from seta import TASKS; print(f'Loaded {len(TASKS)} tasks')"
```

### Integration Testing

Run test_agent.py with different task types:
```bash
# Test easy task
python test_agent.py  # defaults to task 0

# Test specific task by ID
# Modify test_agent.py to filter: [t for t in tasks if t.task_spec["task_id"] == 162][0]
```

### Docker Testing

```bash
docker build -t seta:test .
docker run -p 8080:8080 seta:test
```

## Deployment

### GitHub

```bash
# Ensure you're in the seta directory
cd /Users/rosstaylor/Documents/or_envs/newenvs/seta

# Initialize git (if not already done)
git init
git add .
git commit -m "Initial SETA environment implementation"

# Create and push to EnvCommons
source /Users/rosstaylor/Documents/or_envs/newenvs/.env
gh repo create EnvCommons/seta --public --source=. --remote=origin
git push -u origin main
```

### OpenReward Deployment

1. Upload Dataset/ to OpenReward cloud storage (see DATA_UPLOAD.md)
2. Go to https://openreward.ai/environments/new
3. Connect GitHub repository: EnvCommons/seta
4. Set namespace: EnvCommons/seta
5. Deploy and test

## License

- SETA dataset: MIT License (see https://github.com/camel-ai/seta-env)
- Environment implementation: MIT License

## References

- **SETA Repository**: https://github.com/camel-ai/seta-env
- **Terminal-Bench**: https://github.com/laude-institute/terminal-bench
- **OpenReward Documentation**: https://docs.openreward.org/

## Credits

SETA benchmark created by CAMEL-AI.org. This OpenReward environment implementation adapts the SETA benchmark for use with the OpenReward platform.
