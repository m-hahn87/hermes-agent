"""Klaus controls Matze's Android phone through the Conduit app (``phone.*`` server→client requests).

The Conduit app advertises ``client.capabilities {phone_control: true}`` when its Accessibility
service runs; these tools pin the request to that transport only, so a Desktop window attached to
the same session can never settle a phone request with a method-not-found error. Every action is
scoped to the snapshot from the most recent ``phone.screen`` call in the session: the app answers
``screen_required`` for node actions without one, and ``inactive`` when the Accessibility service is
off — surface both verbatim so the model can ask Matze for the one-time enable instead of retrying.
Screenshots double as chat evidence: the app files every ``phone.screenshot`` result into the
session thread itself.

Handler convention: the registry always calls ``handler(args, **kwargs)`` with the tool-call
argument dict first, so every handler here takes ``args`` (a plain ``def f()`` raises
``TypeError: takes 0 positional arguments but 1 was given``).
"""

import json

from tui_gateway import server_requests
from tools.approval_context import _approval_session_id
from tools.registry import registry, tool_error, tool_result

PHONE_TIMEOUT_S = 10.0

_EMPTY_PARAMS = {"type": "object", "properties": {}}


def _merged(args, kwargs) -> dict:
    """Tool arguments from the positional dict plus any dispatcher-injected keywords."""
    merged = {k: v for k, v in kwargs.items() if k not in {"args"}}
    if isinstance(args, dict):
        merged.update(args)
    return merged


def _int_arg(args, kwargs, name: str) -> tuple[int | None, str | None]:
    value = _merged(args, kwargs).get(name)
    if value is None:
        return None, tool_error(f"Parameter '{name}' fehlt.")
    try:
        return int(value), None
    except (TypeError, ValueError):
        return None, tool_error(f"Parameter '{name}' muss eine Ganzzahl sein (erhielt {value!r}).")


def _str_arg(args, kwargs, name: str) -> tuple[str | None, str | None]:
    value = _merged(args, kwargs).get(name)
    if value is None:
        return None, tool_error(f"Parameter '{name}' fehlt.")
    return str(value), None


def _send(method: str, params: dict | None = None) -> str:
    transport = server_requests.phone_transport()
    if transport is None:
        return tool_error("Kein Conduit-Client mit Phone Control verbunden "
                          "(App oeffnen und Accessibility-Service aktivieren).")
    sid = _approval_session_id.get()
    result = server_requests.send(method, sid, dict(params or {}), timeout=PHONE_TIMEOUT_S,
                                  transport=transport)
    if result is None:
        return tool_error("Phone-Request ohne Antwort (Timeout oder Abbruch).")
    error = result.get("error")
    if isinstance(error, dict) and error.get("code"):
        return tool_error(f"Phone-Fehler: {error.get('code')}", code=error.get("code"))
    if isinstance(error, str) and error:
        return tool_error(f"Phone-Fehler: {error}")
    return tool_result(result)


def phone_screen(args=None, **kwargs) -> str:
    """Read the active Android screen as an LLM-friendly node list (index-stable snapshot)."""
    return _send("phone.screen")


def phone_tap(args=None, **kwargs) -> str:
    """Tap the node at node_index from the last phone_screen snapshot."""
    node_index, err = _int_arg(args, kwargs, "node_index")
    if err:
        return err
    return _send("phone.tap", {"node_index": node_index})


def phone_type(args=None, **kwargs) -> str:
    """Set the text of the editable node at node_index from the last phone_screen snapshot."""
    node_index, err = _int_arg(args, kwargs, "node_index")
    if err:
        return err
    text, err = _str_arg(args, kwargs, "text")
    if err:
        return err
    return _send("phone.type", {"node_index": node_index, "text": text})


def phone_swipe(args=None, **kwargs) -> str:
    """Swipe starting at node_index by delta_x/delta_y pixels (negative scrolls up/left)."""
    node_index, err = _int_arg(args, kwargs, "node_index")
    if err:
        return err
    delta_x, err = _int_arg(args, kwargs, "delta_x")
    if err:
        return err
    delta_y, err = _int_arg(args, kwargs, "delta_y")
    if err:
        return err
    return _send("phone.swipe", {"node_index": node_index, "delta_x": delta_x, "delta_y": delta_y})


def phone_back(args=None, **kwargs) -> str:
    """Press the Android back button (global action, no snapshot needed)."""
    return _send("phone.back")


def phone_screenshot(args=None, **kwargs) -> str:
    """Capture a downscaled Android screenshot as a data-URL (also filed into the chat as evidence)."""
    result = _send("phone.screenshot")
    try:
        parsed = json.loads(result)
    except (ValueError, TypeError):
        return result
    if isinstance(parsed, dict) and parsed.get("data_url"):
        # The data-URL rides into the thread via the app itself; keep the tool payload lean.
        lean = {k: v for k, v in parsed.items() if k != "data_url"}
        lean["screenshot"] = "im Chat abgelegt"
        return tool_result(lean)
    return result


registry.register("phone_screen", "phone_control",
                  {"type": "function", "parameters": _EMPTY_PARAMS}, phone_screen,
                  description="Liest den aktiven Android-Screen als Node-Liste (Playwright-Fuehl). "
                              "Voraussetzung: Conduit-App mit aktivem Accessibility-Service verbunden.")
registry.register("phone_tap", "phone_control",
                  {"type": "function", "parameters": {"type": "object", "properties": {
                      "node_index": {"type": "integer", "description": "Index aus dem letzten phone_screen-Snapshot"}},
                      "required": ["node_index"]}}, phone_tap,
                  description="Tippt das Node aus dem letzten phone_screen-Snapshot an.")
registry.register("phone_type", "phone_control",
                  {"type": "function", "parameters": {"type": "object", "properties": {
                      "node_index": {"type": "integer"}, "text": {"type": "string"}},
                      "required": ["node_index", "text"]}}, phone_type,
                  description="Setzt den Text eines editierbaren Nodes (Eingabefeld).")
registry.register("phone_swipe", "phone_control",
                  {"type": "function", "parameters": {"type": "object", "properties": {
                      "node_index": {"type": "integer"}, "delta_x": {"type": "integer"},
                      "delta_y": {"type": "integer"}},
                      "required": ["node_index", "delta_x", "delta_y"]}}, phone_swipe,
                  description="Wischt ab node_index um delta_x/delta_y Pixel (negativ = hoch/links).")
registry.register("phone_back", "phone_control",
                  {"type": "function", "parameters": _EMPTY_PARAMS}, phone_back,
                  description="Android-Zurueck-Button (global, ohne Snapshot).")
registry.register("phone_screenshot", "phone_control",
                  {"type": "function", "parameters": _EMPTY_PARAMS}, phone_screenshot,
                  description="Android-Screenshot als Data-URL; landet automatisch als Beweis im Chat.")
