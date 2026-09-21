import subprocess

repo = "C:/Users/shubham/Desktop/git_ai_agent"
venv_python = repo + "/.venv/Scripts/python.exe"
cmd = [venv_python, "-m", "pytest", "tests/test_security_hardening.py", "tests/test_phase7_security_abuse.py", "-q"]
print("RUNNING:", cmd)
result = subprocess.run(cmd, cwd=repo)
raise SystemExit(result.returncode)
