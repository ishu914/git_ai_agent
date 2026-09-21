import subprocess
import sys
from pathlib import Path

repo = r"C:\Users\shubham\Desktop\git_ai_agent"
log_path = Path(repo) / "tmp_pytest_sqlite.log"
cmd = [r"C:\Users\shubham\Desktop\git_ai_agent\.venv\Scripts\python.exe", "-m", "pytest", "tests/test_phase7_sqlite_backup_restore.py", "-q"]
result = subprocess.run(cmd, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
log_path.write_text(result.stdout, encoding="utf-8")
print(result.returncode)
print(log_path)
