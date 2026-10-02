"""Run synthetic catalog checks in two owned, loopback-only Docker containers."""
import json
import os
import re
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
IMAGES = {"postgres": "postgres:16-alpine", "rabbitmq": "rabbitmq:4-alpine"}


def validate_local_endpoint(endpoint):
    if not isinstance(endpoint, str) or "\x00" in endpoint:
        raise ValueError("Invalid Docker endpoint")
    parsed = urlsplit(endpoint)
    unix = (parsed.scheme == "unix" and not parsed.netloc and parsed.path.startswith("/")
            and not parsed.path.startswith("//"))
    pipe = re.fullmatch(r"npipe:(?://|////)\./pipe/[A-Za-z0-9_.-]+", endpoint)
    if (not (unix or pipe) or parsed.query or parsed.fragment
            or any(character.isspace() for character in endpoint)):
        raise ValueError("Only local Unix sockets or local named pipes are allowed")


def run():
    suffix = uuid.uuid4().hex[:12]
    project = "rag-probe-" + suffix
    password = secrets.token_urlsafe(32)
    env = os.environ.copy()
    env.update(POSTGRES_USER="rag_probe", POSTGRES_PASSWORD=password,
               POSTGRES_DB="rag_probe_" + suffix,
               RABBITMQ_DEFAULT_USER="rag_probe", RABBITMQ_DEFAULT_PASS=password,
               RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS="+S 2:2")
    owned = []
    stage = "local Docker preflight"

    def docker(*args, check=True, timeout=120):
        result = subprocess.run(["docker", *args], env=env, capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            # Do not emit potentially credential-bearing daemon diagnostics.
            raise RuntimeError("Docker operation failed: " + args[0])
        return result

    try:
        context = json.loads(docker("context", "inspect").stdout)[0]
        endpoint = context["Endpoints"]["docker"]["Host"]
        if env.get("DOCKER_HOST"):
            raise RuntimeError("Use an explicit local Docker context with DOCKER_HOST unset")
        validate_local_endpoint(endpoint)
        # These are public dependency images, not pre-existing application containers.
        for service, image in IMAGES.items():
            stage = "prepare " + service
            if docker("image", "inspect", image, check=False).returncode:
                print("Pulling " + image, flush=True)
                docker("pull", image, timeout=300)
            name = project + "-" + service
            port = "5432" if service == "postgres" else "5672"
            keys = ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"] if service == "postgres" else ["RABBITMQ_DEFAULT_USER", "RABBITMQ_DEFAULT_PASS", "RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS"]
            args = ["create", "--name", name, "--label", "rag.probe.owner=" + project,
                    "--publish", "127.0.0.1::" + port, "--memory", "768m", "--cpus", "2"]
            for key in keys:
                args.extend(["--env", key])
            # Track the name before creation so timeout-after-create is cleaned up too.
            owned.append(name)
            docker(*args, image)
            docker("start", name)
            print("Started disposable " + service, flush=True)
        for service in IMAGES:
            stage = "readiness " + service
            deadline = time.monotonic() + 120
            name = project + "-" + service
            probe = ["pg_isready", "-U", "rag_probe", "-d", env["POSTGRES_DB"]] if service == "postgres" else ["rabbitmq-diagnostics", "-q", "check_port_connectivity"]
            # A root Erlang CLI can create a root-owned cookie before broker startup.
            exec_args = ["exec"] if service == "postgres" else ["exec", "--user", "rabbitmq"]
            while True:
                try:
                    if docker(*exec_args, name, *probe, check=False, timeout=15).returncode == 0:
                        break
                except subprocess.TimeoutExpired:
                    pass
                running = docker("inspect", name, "--format", "{{.State.Running}}", timeout=15)
                if running.stdout.strip() != "true":
                    state = json.loads(docker("inspect", name, "--format", "{{json .State}}", timeout=15).stdout)
                    print(json.dumps({"service": service, "exit_code": state.get("ExitCode"), "oom_killed": state.get("OOMKilled")}), flush=True)
                    diagnostic = docker("logs", "--tail", "30", name, check=False, timeout=15)
                    print((diagnostic.stdout + diagnostic.stderr).replace(password, "<redacted>"), flush=True)
                    raise RuntimeError("Disposable service exited: " + service)
                if time.monotonic() >= deadline:
                    raise TimeoutError("Disposable service not ready: " + service)
                time.sleep(2)
            print("Ready: " + service, flush=True)
        ports = {}
        image_ids = {}
        for service, container_port in (("postgres", "5432/tcp"), ("rabbitmq", "5672/tcp")):
            obj = json.loads(docker("inspect", project + "-" + service).stdout)[0]
            binding = obj["NetworkSettings"]["Ports"][container_port]
            if len(binding) != 1 or binding[0]["HostIp"] != "127.0.0.1":
                raise RuntimeError("Refusing non-loopback test service")
            ports[service] = binding[0]["HostPort"]
            image_ids[service] = obj["Image"]
        env.update(RAG_INTEGRATION="1", RAG_TEST_DB=env["POSTGRES_DB"],
                   RAG_TEST_PASSWORD=password, RAG_TEST_PG_PORT=ports["postgres"],
                   RAG_TEST_OWNER=project, RAG_TEST_BROKER_CONTAINER=project + "-rabbitmq",
                   RAG_TEST_AMQP_URL="amqp://rag_probe:" + password + "@127.0.0.1:" + ports["rabbitmq"] + "/")
        print(json.dumps({"images": image_ids, "scope": "synthetic SQL + AMQP, no model or Telegram"}), flush=True)
        stage = "integration tests"
        result = subprocess.run([sys.executable, "-m", "pytest", "tests/integration", "-q", "--tb=short"],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
        print((result.stdout + result.stderr).replace(password, "<redacted>"))
        return result.returncode
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, RuntimeError) as error:
        print("Integration runner failed at " + stage + " (" + type(error).__name__ + "); no raw service diagnostics emitted.", file=sys.stderr)
        return 1
    finally:
        failures = []
        for name in reversed(owned):
            try:
                info = docker("inspect", name, check=False, timeout=30)
                if info.returncode:
                    # Distinguish absent containers from a daemon failure.
                    existing = docker("ps", "-a", "--filter", "name=^/" + name + "$", "--format", "{{.Names}}", timeout=30)
                    if existing.stdout.strip():
                        failures.append(name)
                    continue
                labels = json.loads(info.stdout)[0]["Config"]["Labels"] or {}
                if labels.get("rag.probe.owner") != project:
                    failures.append(name)
                    continue
                docker("rm", "--force", "--volumes", name, timeout=30)
            except (subprocess.SubprocessError, OSError, ValueError, KeyError, RuntimeError):
                failures.append(name)
        if failures:
            print("Owned test container cleanup incomplete: " + ", ".join(failures), file=sys.stderr)
            raise RuntimeError("Test resource cleanup incomplete")
        print("Owned test containers and their anonymous volumes removed.", flush=True)


if __name__ == "__main__":
    raise SystemExit(run())
