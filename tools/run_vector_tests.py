"""Run actual local Chroma tests in temporary directories, without model downloads."""
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    print(json.dumps({"chromadb": version("chromadb"), "python": sys.version.split()[0]}), flush=True)
    env = dict(os.environ, RAG_VECTOR_TESTS="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
               ANONYMIZED_TELEMETRY="False")
    return subprocess.run([sys.executable, "-m", "pytest", "tests/vector", "-q", "--tb=short"],
                          cwd=Path(__file__).resolve().parents[1], env=env, timeout=180).returncode


if __name__ == "__main__":
    raise SystemExit(main())
