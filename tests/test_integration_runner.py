import json
from types import SimpleNamespace

import pytest

from tools import run_integration


class DockerProbe:
    def __init__(self, endpoint="unix:///var/run/docker.sock", wrong_owner=False, create_failure=False):
        self.endpoint = endpoint
        self.wrong_owner = wrong_owner
        self.create_failure = create_failure
        self.containers = {}
        self.removed = []
        self.secret = None
        self.test_called = False

    def __call__(self, args, **kwargs):
        def result(stdout="", code=0):
            return SimpleNamespace(stdout=stdout, stderr="", returncode=code)
        if args[0] != "docker":
            self.test_called = True
            self.secret = kwargs["env"]["RAG_TEST_PASSWORD"]
            return result("synthetic failure with " + self.secret, 1)
        if args[1:3] == ["context", "inspect"]:
            return result(json.dumps([{"Endpoints": {"docker": {"Host": self.endpoint}}}]))
        if "rabbitmq-diagnostics" in args:
            assert args[1:4] == ["exec", "--user", "rabbitmq"]
        if args[1] == "create":
            if self.create_failure:
                return result(code=1)
            name = args[args.index("--name") + 1]
            owner = args[args.index("--label") + 1].split("=", 1)[1]
            assert args[args.index("--publish") + 1].startswith("127.0.0.1::")
            self.containers[name] = owner
        if args[1] == "inspect":
            name = args[2]
            if name not in self.containers:
                return result(code=1)
            return result(json.dumps([{
                "Config": {"Labels": {"rag.probe.owner": "unowned" if self.wrong_owner else self.containers[name]}},
                "NetworkSettings": {"Ports": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "10001"}], "5672/tcp": [{"HostIp": "127.0.0.1", "HostPort": "10002"}]}},
                "Image": "synthetic-image-id",
            }]))
        if args[1] == "rm":
            self.removed.append(args[-1])
            assert args[-1] in self.containers
        return result()


def test_rejects_remote_docker_without_creating_resources(monkeypatch):
    probe = DockerProbe(endpoint="ssh://remote-test-host")
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(run_integration.subprocess, "run", probe)
    assert run_integration.run() == 1
    assert not probe.containers and not probe.test_called


def test_rejects_docker_host_override(monkeypatch):
    probe = DockerProbe()
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote-test-host")
    monkeypatch.setattr(run_integration.subprocess, "run", probe)
    assert run_integration.run() == 1
    assert not probe.containers


def test_test_failure_is_propagated_redacted_and_cleaned(monkeypatch, capsys):
    probe = DockerProbe()
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(run_integration.subprocess, "run", probe)
    assert run_integration.run() == 1
    assert probe.test_called and len(probe.removed) == 2
    output = capsys.readouterr()
    assert probe.secret not in output.out + output.err
    assert "<redacted>" in output.out


def test_cleanup_never_removes_wrong_owner(monkeypatch):
    probe = DockerProbe(wrong_owner=True)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(run_integration.subprocess, "run", probe)
    with pytest.raises(RuntimeError, match="cleanup incomplete"):
        run_integration.run()
    assert not probe.removed


def test_create_failure_does_not_attempt_to_remove_absent_container(monkeypatch):
    probe = DockerProbe(create_failure=True)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(run_integration.subprocess, "run", probe)
    assert run_integration.run() == 1
    assert not probe.test_called and not probe.removed
