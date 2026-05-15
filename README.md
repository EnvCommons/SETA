# SETA

[![OpenReward Environment](https://img.shields.io/badge/%E2%AD%90%20OpenReward-Environment-f7e6cc)](https://www.openreward.ai/Eigent/SETA)

## Description

SETA (Scaling Environments for Terminal Agents) is an ORS environment for evaluating terminal-based coding and system administration capabilities, developed by [CAMEL-AI](https://github.com/camel-ai/seta). It contains 1376 tasks covering software engineering (622 tasks), system administration (605 tasks), DevOps (33 tasks), debugging (29 tasks), configuration (35 tasks), security (20 tasks), networking (15 tasks), and other categories. Each task is validated by a pytest test suite with weighted scoring.

## Capabilities

- Terminal-based task completion
- Software engineering automation
- System administration and user management
- DevOps, security, and networking tasks

## Compute Requirements

Agents are given a sandboxed environment with CLI tools (bash, read, write, edit, grep, glob, ls). Uses custom Docker image with Ubuntu 24.04.

## License

[Apache 2.0](https://www.apache.org/licenses/LICENSE-2.0).

## Tasks

There is one split in this environment:

- **train**: 1376 tasks

Tasks span software engineering, system administration, DevOps, security, networking, debugging, and configuration categories.

## Reward Structure

This is a multi-turn environment with pytest-based validation. The agent uses CLI tools to complete terminal tasks, then calls `submit_solution` to run tests. Each pytest test has a specific weight, and the final reward is the weighted sum of passed tests (0.0 to 1.0).

## Data

Data consists of the SETA Dataset directory sourced from [GitHub camel-ai/seta-env](https://github.com/camel-ai/seta-env). Each task includes `task.yaml`, `weights.json`, `tests/test_outputs.py`, and required data files. Data is stored on the OpenReward platform.

## Tools

| Tool | Description |
|------|-------------|
| `submit_solution` | Run pytest tests and get weighted score. Ends the episode. |
| `bash` | Execute shell commands in sandbox. |
| `read` | Read file contents. |
| `write` | Write files. |
| `edit` | Edit existing files. |
| `grep` | Search file contents. |
| `glob` | Find files by pattern. |
| `ls` | List directory contents. |

## Time Horizon

Multi-turn. Agents explore files, write scripts, execute commands, then submit for test validation.

## Environment Difficulty

Difficulty distribution: Easy (25), Medium (629), Hard (722).

## Other Environment Requirements

None.

## Safety

Agents in SETA operate within sandboxed environments. Commands are executed in isolated containers with controlled filesystem access.

## Citation

```bibtex
@misc{seta,
  author    = {Qijia Shen and Jay Rainton and Aznaur Aliev and Ahmed Awelkair and Boyuan Ma and Zhiqi (Julie) Huang and Yuzhen Mao and Wendong Fan and Philip Torr and Bernard Ghanem and Changran Hu and Urmish Thakker and Guohao Li},
  title     = {{SETA: Scaling Environments for Terminal Agents}},
  year      = {2026},
  month     = jan,
  url       = {https://github.com/camel-ai/seta},
  note      = {Blog: \url{https://eigent-ai.notion.site/SETA-Scaling-Environments-for-Terminal-Agents-2d2511c70ba280a9b7c0fe3e7f1b6ab8}}
}
```

