from pathlib import Path

if Path("/orwd_data/").exists():
    ENV_PATH = Path("/orwd_data/")
else:
    ENV_PATH = Path(__file__).parent
