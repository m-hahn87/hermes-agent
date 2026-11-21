from tui_gateway import phone_control, server_requests


class Phone:
    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)
        assert not server_requests.open_requests("chat")
        assert not server_requests.resolve_pinned_response(
            {"id": frame["id"], "result": {"nodes": []}}, object()
        )
        server_requests.resolve_pinned_response(
            {"id": frame["id"], "result": {"nodes": []}}, self
        )
        return True


def test_phone_routes_only_to_the_single_capable_session_socket():
    phone = Phone()
    other = Phone()
    try:
        server_requests.advertise_phone(phone, True)
        assert phone_control.request("chat", "phone.screen", {}, [other, phone]) == {"nodes": []}
        assert phone.frames[0]["params"] == {"session_id": "chat"}
        assert other.frames == []
        server_requests.advertise_phone(other, True)
        assert phone_control.request("chat", "phone.screen", {}, [other, phone])["error"] == "ambiguous"
    finally:
        server_requests.forget(phone)
        server_requests.forget(other)
    assert phone_control.request("chat", "phone.screen", {}, [phone])["error"] == "offline"


def test_timeout_and_invalid_params(monkeypatch):
    phone = Phone()
    try:
        server_requests.advertise_phone(phone, True)

        def unanswered(method, sid, args, *, timeout, transport):
            assert (method, sid, args, timeout, transport) == ("phone.tap", "chat", {"node_index": 0}, 10, phone)

        monkeypatch.setattr(server_requests, "send", unanswered)
        assert phone_control.request("chat", "phone.tap", {"node_index": 0}, [phone])["error"] == "timeout"
        assert phone_control.request("chat", "phone.tap", {"node_index": "0"}, [phone])["error"] == "invalid_params"
    finally:
        server_requests.forget(phone)


def test_disconnect_settles_the_inflight_request():
    class DisconnectingPhone:
        def write(self, _frame):
            server_requests.forget(self)
            return False

    phone = DisconnectingPhone()
    server_requests.advertise_phone(phone, True)
    assert phone_control.request("chat", "phone.back", {}, [phone]) == {"error": "offline"}


def test_tool_definitions_are_discovered():
    import model_tools

    schemas = model_tools.get_tool_definitions(enabled_toolsets=["phone_control"], quiet_mode=True, skip_tool_search_assembly=True)
    names = {schema["function"]["name"] for schema in schemas}
    assert {"phone_screen", "phone_tap", "phone_type", "phone_swipe", "phone_back", "phone_screenshot"} <= names


def test_screen_tap_screenshot_model_roundtrip(monkeypatch):
    import json

    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.phone_control_tool import phone_tool
    from tui_gateway import server

    class PhoneClient:
        def write(self, frame):
            assert frame["params"]["session_id"] == "chat"
            if frame["method"] == "phone.tap":
                assert frame["params"]["node_index"] == 0
            return server_requests.resolve_pinned_response(
                {"id": frame["id"], "result": answers[frame["method"]]}, self
            )

    answers = {
        "phone.screen": {"nodes": [{"i": 0, "clickable": True}]},
        "phone.tap": {"observation": "target_changed"},
        "phone.screenshot": {"data_url": "data:image/jpeg;base64,/9j/2Q==", "width": 1, "height": 1},
    }
    phone = PhoneClient()
    server_requests.advertise_phone(phone, True)
    monkeypatch.setattr(server, "phone_peers", lambda sid: [phone] if sid == "chat" else [])
    tokens = set_session_vars(ui_session_id="chat")
    try:
        assert json.loads(phone_tool("phone.screen", {})) == answers["phone.screen"]
        assert json.loads(phone_tool("phone.tap", {"node_index": 0})) == answers["phone.tap"]
        image = phone_tool("phone.screenshot", {})["content"][1]
        assert image["image_url"]["url"] == answers["phone.screenshot"]["data_url"]
    finally:
        clear_session_vars(tokens)
        server_requests.forget(phone)
