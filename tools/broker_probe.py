"""Test-only control of the exact runner-owned RabbitMQ application."""
import json
import os
import re
import subprocess
import time
from urllib.parse import urlsplit

from tools.run_integration import validate_local_endpoint


class OwnedBroker:
    def __init__(self, environment=None):
        self._env = dict(os.environ if environment is None else environment)
        self._id = None
        self._endpoint = None
        try:
            self._owner = self._env["RAG_TEST_OWNER"]
            self._name = self._env["RAG_TEST_BROKER_CONTAINER"]
            url = urlsplit(self._env["RAG_TEST_AMQP_URL"])
            if (self._env.get("RAG_INTEGRATION") != "1"
                    or not re.fullmatch(r"rag-probe-[0-9a-f]{12}", self._owner)
                    or self._name != self._owner + "-rabbitmq"
                    or self._env["RAG_TEST_DB"] != "rag_probe_" + self._owner.removeprefix("rag-probe-")
                    or url.scheme != "amqp" or url.hostname != "127.0.0.1"
                    or url.username != "rag_probe" or not url.password
                    or url.password != self._env["RAG_TEST_PASSWORD"]
                    or url.port is None or url.path != "/" or url.query or url.fragment
                    or self._env.get("DOCKER_HOST")):
                raise ValueError()
            self._port = str(url.port)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError("Owned broker test configuration is invalid") from None

    def _docker(self, *args, pinned=True, check=True, timeout=20):
        command = ["docker"]
        environment = self._env.copy()
        if pinned:
            command.extend(["--host", self._endpoint])
            environment.pop("DOCKER_CONTEXT", None)
        try:
            result = subprocess.run(command + list(args), env=environment,
                                    capture_output=True, text=True, timeout=timeout)
            if check and result.returncode:
                raise RuntimeError()
            return result
        except (subprocess.SubprocessError, OSError, RuntimeError):
            raise RuntimeError("Owned broker control failed; no raw diagnostics emitted") from None

    def _validate(self):
        try:
            context = json.loads(self._docker("context", "inspect", pinned=False).stdout)
            if len(context) != 1:
                raise ValueError()
            endpoint = context[0]["Endpoints"]["docker"]["Host"]
            validate_local_endpoint(endpoint)
            if self._endpoint is not None and self._endpoint != endpoint:
                raise ValueError()
            self._endpoint = endpoint
            info = json.loads(self._docker("inspect", self._name).stdout)
            if len(info) != 1:
                raise ValueError()
            item = info[0]
            identifier = item["Id"]
            ports = item["NetworkSettings"]["Ports"]
            binding = ports["5672/tcp"]
            if (item["Name"] != "/" + self._name
                    or item["Config"]["Labels"].get("rag.probe.owner") != self._owner
                    or item["State"]["Running"] is not True
                    or not re.fullmatch("[0-9a-f]{64}", identifier)
                    or (self._id is not None and identifier != self._id)
                    or binding != [{"HostIp": "127.0.0.1", "HostPort": self._port}]
                    or any(value for key, value in ports.items() if key != "5672/tcp")):
                raise ValueError()
            self._id = identifier
            return identifier
        except (KeyError, IndexError, ValueError, TypeError, AttributeError):
            raise RuntimeError("Refusing unverified broker ownership or exposure") from None

    def stop(self):
        identifier = self._validate()
        self._docker("exec", "--user", "rabbitmq", identifier, "rabbitmqctl", "-q", "stop_app", timeout=30)

    def start(self):
        identifier = self._validate()
        self._docker("exec", "--user", "rabbitmq", identifier, "rabbitmqctl", "-q", "start_app", timeout=30)

    def wait_ready(self, timeout=45):
        if type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise ValueError("Invalid readiness deadline")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            identifier = self._validate()
            remaining = max(0.1, min(10, deadline - time.monotonic()))
            result = self._docker("exec", "--user", "rabbitmq", identifier,
                                  "rabbitmq-diagnostics", "-q", "check_port_connectivity",
                                  check=False, timeout=remaining)
            if result.returncode == 0:
                return
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        raise TimeoutError("Owned broker did not become ready")
