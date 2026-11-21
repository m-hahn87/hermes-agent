"""Model tools for the Phone Control bridge on the chat WebSocket."""

import json
from functools import partial

from tools.registry import registry
from tui_gateway.contracts.phone import PHONE_CONTRACTS


PHONE_TOOL_NAMES = {
    "screen": "phone.screen",
    "tap": "phone.tap",
    "type": "phone.type",
    "swipe": "phone.swipe",
    "back": "phone.back",
    "screenshot": "phone.screenshot",
}


def phone_tool(method, args, **_):
    from gateway.session_context import get_session_env
    from tui_gateway import phone_control, server

    sid = get_session_env("HERMES_UI_SESSION_ID")
    result = phone_control.request(sid, method, args, server.phone_peers(sid) if sid else [])
    if method == "phone.screenshot":
        url = result.pop("data_url", None)
        if isinstance(url, str) and url.startswith("data:image/jpeg;base64,"):
            return {"content": [
                {"type": "text", "text": json.dumps(result)},
                {"type": "image_url", "image_url": {"url": url}},
            ]}
    return json.dumps(result)


for action, method in PHONE_TOOL_NAMES.items():
    params, _, description = PHONE_CONTRACTS[action]
    name = f"phone_{action}"
    schema = params.model_json_schema()
    schema["properties"].pop("session_id")
    schema["required"].remove("session_id")
    schema.pop("title", None)
    registry.register(
        name=name, toolset="phone_control",
        schema={"name": name, "description": description, "parameters": schema},
        handler=partial(phone_tool, method),
    )
