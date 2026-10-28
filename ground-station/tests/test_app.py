import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ground_station.app import create_app
from ground_station.config import Config

from .test_integration import free_port


@pytest.fixture
def client(tmp_path):
    cfg = Config.model_validate({"rc": {"port": free_port()}, "logging": {"dir": str(tmp_path)}, "storage": {"dir": str(tmp_path)}})
    with TestClient(create_app(cfg)) as c:
        yield c


def test_serves_ui(client):
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()


@pytest.mark.parametrize("origin", [None, "http://evil.example", "http://testserver.evil.example"])
def test_foreign_origin_rejected(client, origin):
    headers = {"origin": origin} if origin else {}
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ws", headers=headers) as ws:
            ws.receive_json()
    assert e.value.code == 4003


def test_one_controlling_tab(client):
    headers = {"origin": "http://testserver"}
    with client.websocket_connect("/ws", headers=headers) as first:
        hello = first.receive_json()
        assert hello["type"] == "hello" and hello["config"]["speed_scale_default"] == 0.3
        with client.websocket_connect("/ws", headers=headers) as second:
            assert second.receive_json()["type"] == "rejected"
            with pytest.raises(WebSocketDisconnect) as e:
                second.receive_json()
            assert e.value.code == 4001
        first.send_json({"type": "ui_heartbeat", "seq": 0})
        assert first.receive_json()["type"] == "status"


def test_mission_ops_over_websocket(client):
    from .test_mission import mission

    headers = {"origin": "http://testserver"}
    with client.websocket_connect("/ws", headers=headers) as ws:
        seen = {}
        while not {"hello", "status", "missions", "schedules"} <= seen.keys():
            msg = ws.receive_json()
            seen[msg["type"]] = msg
        assert seen["missions"]["missions"] == []
        ws.send_json({"type": "op", "id": "o1", "op": "save_mission", "args": {"mission": mission().model_dump(mode="json")}})
        ack = missions = None
        while ack is None or missions is None:
            msg = ws.receive_json()
            if msg["type"] == "ack":
                ack = msg
            elif msg["type"] == "missions":
                missions = msg["missions"]
        assert ack["ok"] and len(ack["data"]["id"]) == 8
        assert missions[0]["name"] == "test" and missions[0]["report"]["home_checked"] is False
        ws.send_json({"type": "op", "id": "o2", "op": "create_schedule", "args": {"mission_id": ack["data"]["id"], "launch_at": 9e9}})
        while (msg := ws.receive_json())["type"] != "ack":
            pass
        assert not msg["ok"] and "confirmation" in msg["error"]


def test_generate_survey_over_websocket(client):
    headers = {"origin": "http://testserver"}
    square = [[47.0, 8.0], [47.0, 8.0013], [47.0009, 8.0013], [47.0009, 8.0]]  # ~100 m x 100 m
    with client.websocket_connect("/ws", headers=headers) as ws:
        ws.send_json({"type": "op", "id": "s1", "op": "generate_survey",
                      "args": {"polygon": square, "alt": 40, "spacing_m": 25, "speed": 8}})
        while (msg := ws.receive_json())["type"] != "ack":
            pass
        assert msg["ok"], msg["error"]
        assert msg["data"]["lines"] == 4 and len(msg["data"]["mission"]["waypoints"]) == 8
        ws.send_json({"type": "op", "id": "s2", "op": "generate_survey",
                      "args": {"polygon": square[:2], "alt": 40, "spacing_m": 25, "speed": 8}})
        while (msg := ws.receive_json())["type"] != "ack":
            pass
        assert not msg["ok"] and "polygon" in msg["error"]
