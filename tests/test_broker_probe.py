import json
from types import SimpleNamespace

import pytest

from tools import broker_probe
from tools.broker_probe import OwnedBroker
from tools.run_integration import validate_local_endpoint


def environment():
    return {
        "RAG_INTEGRATION": "1", "RAG_TEST_OWNER": "rag-probe-123456abcdef",
        "RAG_TEST_BROKER_CONTAINER": "rag-probe-123456abcdef-rabbitmq",
        "RAG_TEST_DB": "rag_probe_123456abcdef", "RAG_TEST_PASSWORD": "synthetic-password",
        "RAG_TEST_AMQP_URL": "amqp://rag_probe:synthetic-password@127.0.0.1:10002/",
    }


class DockerControl:
    def __init__(self):
        self.endpoint = "unix:///var/run/docker.sock"
        self.container = {
            "Id": "a" * 64, "Name": "/rag-probe-123456abcdef-rabbitmq",
            "Config": {"Labels": {"rag.probe.owner": "rag-probe-123456abcdef"}},
            "State": {"Running": True},
            "NetworkSettings": {"Ports": {"5672/tcp": [{"HostIp": "127.0.0.1", "HostPort": "10002"}]}},
        }
        self.actions = []

    def __call__(self, command, **kwargs):
        result = lambda data: SimpleNamespace(stdout=json.dumps(data), stderr="", returncode=0)
        if command[1:3] == ["context", "inspect"]:
            return result([{"Endpoints": {"docker": {"Host": self.endpoint}}}])
        assert command[:3] == ["docker", "--host", self.endpoint]
        assert "DOCKER_CONTEXT" not in kwargs["env"]
        if command[3] == "inspect":
            return result([self.container])
        assert command[3:6] == ["exec", "--user", "rabbitmq"]
        assert command[6] == self.container["Id"]
        self.actions.append(command[7:])
        return result({})


@pytest.mark.parametrize("endpoint", ["ssh://remote", "tcp://127.0.0.1:2375",
    "npipe:////remote/pipe/docker_engine", "unix://remote/socket",
    "npipe:////./pipe/../remote", "unix:////remote/socket", "unix:///socket?x=1", ""])
def test_remote_or_ambiguous_endpoints_are_rejected(endpoint):
    with pytest.raises(ValueError):
        validate_local_endpoint(endpoint)


@pytest.mark.parametrize("endpoint", ["unix:///var/run/docker.sock",
    "npipe:////./pipe/dockerDesktopLinuxEngine", "npipe://./pipe/docker_engine"])
def test_local_endpoints_are_accepted(endpoint):
    validate_local_endpoint(endpoint)


@pytest.mark.parametrize("change", [
    {"RAG_INTEGRATION": "0"}, {"RAG_TEST_OWNER": "production"},
    {"RAG_TEST_BROKER_CONTAINER": "other-rabbitmq"}, {"RAG_TEST_DB": "existing"},
    {"RAG_TEST_AMQP_URL": "amqp://rag_probe:synthetic-password@remote:10002/"},
    {"RAG_TEST_PASSWORD": "wrong"}, {"DOCKER_HOST": "tcp://remote"},
])
def test_invalid_configuration_does_not_invoke_docker(monkeypatch, change):
    def forbidden(*args, **kwargs):
        pytest.fail("Docker should not be called")
    monkeypatch.setattr(broker_probe.subprocess, "run", forbidden)
    with pytest.raises(ValueError):
        OwnedBroker({**environment(), **change})


@pytest.mark.parametrize("change", ["owner", "name", "port", "public", "extra-port", "stopped", "id"])
def test_unowned_or_reconfigured_containers_are_never_controlled(monkeypatch, change):
    docker = DockerControl()
    if change == "owner":
        docker.container["Config"]["Labels"]["rag.probe.owner"] = "other"
    elif change == "name":
        docker.container["Name"] = "/other"
    elif change == "port":
        docker.container["NetworkSettings"]["Ports"]["5672/tcp"][0]["HostPort"] = "10003"
    elif change == "public":
        docker.container["NetworkSettings"]["Ports"]["5672/tcp"][0]["HostIp"] = "0.0.0.0"
    elif change == "extra-port":
        docker.container["NetworkSettings"]["Ports"]["15672/tcp"] = [{"HostIp": "127.0.0.1", "HostPort": "10004"}]
    elif change == "stopped":
        docker.container["State"]["Running"] = False
    else:
        docker.container["Id"] = "invalid"
    monkeypatch.setattr(broker_probe.subprocess, "run", docker)
    with pytest.raises(RuntimeError, match="Refusing"):
        OwnedBroker(environment()).stop()
    assert docker.actions == []


def test_control_pins_endpoint_identity_and_service_user(monkeypatch):
    docker = DockerControl()
    monkeypatch.setattr(broker_probe.subprocess, "run", docker)
    broker = OwnedBroker({**environment(), "DOCKER_CONTEXT": "synthetic-local"})
    broker.stop()
    broker.start()
    broker.wait_ready()
    assert docker.actions == [
        ["rabbitmqctl", "-q", "stop_app"], ["rabbitmqctl", "-q", "start_app"],
        ["rabbitmq-diagnostics", "-q", "check_port_connectivity"],
    ]
    docker.container["Id"] = "b" * 64
    with pytest.raises(RuntimeError, match="Refusing"):
        broker.stop()
    assert len(docker.actions) == 3


def test_changed_context_is_rejected_before_second_action(monkeypatch):
    docker = DockerControl()
    monkeypatch.setattr(broker_probe.subprocess, "run", docker)
    broker = OwnedBroker(environment())
    broker.stop()
    docker.endpoint = "unix:///different.sock"
    with pytest.raises(RuntimeError, match="Refusing"):
        broker.start()
    assert len(docker.actions) == 1
