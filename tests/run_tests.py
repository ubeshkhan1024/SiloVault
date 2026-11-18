import subprocess
result = subprocess.run(
    [".venv\\Scripts\\python.exe", "-m", "pytest", "-o", "addopts=''", "test_recovery.py", "-q"],
    cwd="c:\\Projects\\SiloVault\\tests",
    capture_output=True,
    text=True
)
print("STDOUT:", result.stdout)
print("STDERR:", result.stderr)
print("Return code:", result.returncode)