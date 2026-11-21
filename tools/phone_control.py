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
import logging
import re
import time

from tui_gateway import server_requests
from tui_gateway.transport import turn_origin_transport
from tools.approval_context import _approval_session_id
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

PHONE_TIMEOUT_S = 10.0
PHONE_ACTIONS_TIMEOUT_S = 30.0  # Android has 25 s; the extra 5 s covers transit, never a retry.
PHONE_ACTIONS_MAX_BYTES = 8 * 1024
PHONE_ACTIONS_MAX_LIST = 32

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


# ── native actions v1 ────────────────────────────────────────────────────────────────────────
# One model tool for list/run. Android owns the allow-list; the gateway only forwards action ids
# that it also knows how to validate, never free URIs/packages/classes/scripts. Exactly one request
# per run, pinned to the authenticated socket that submitted THIS turn and registered v1 there.

_ACTION_SPECS: dict[str, dict] = {
    "alarm.set": {
        "description": "Stellt einen Wecker (handed_off an die Uhr-App, keine Wecker-Rueckmeldung).",
        "parameters": {"hour": {"type": "integer", "minimum": 0, "maximum": 23},
                       "minute": {"type": "integer", "minimum": 0, "maximum": 59}},
    },
    "timer.set": {
        "description": "Startet einen Timer (handed_off an die Uhr-App).",
        "parameters": {"seconds": {"type": "integer", "minimum": 1, "maximum": 86400}},
    },
    "maps.open": {
        "description": "Oeffnet eine Navigation (handed_off an die Karten-App).",
        "parameters": {"destination": {"type": "string", "maxLength": 300},
                       "travel_mode": {"type": "string",
                                       "enum": ["driving", "walking", "transit", "bicycling", "two-wheeler"]}},
    },
}

_STATUSES = frozenset({
    "handed_off", "denied", "disabled", "needs_foreground",
    "permission_required", "target_unavailable", "invalid_arguments", "cancelled",
    "duplicate", "unknown_outcome", "unavailable",
})
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _actions_fail(message: str, status: str = "unavailable") -> str:
    return tool_result({"status": status, "error": message})


def _valid_int(value, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _valid_destination(value) -> bool:
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        return False
    if any(ch in value for ch in "\x00\n\r\t"):
        return False
    # No free URI schemes, package names, class names or intents: an action is a destination string,
    # never a launch primitive the model can point at an arbitrary target.
    return not (_SCHEME_RE.match(value) or value.startswith(("package:", "class:", "intent:")))


def _validate_arguments(action: str, arguments) -> tuple[dict | None, str | None]:
    if not isinstance(arguments, dict):
        return None, "arguments muss ein Objekt sein."
    allowed = set(_ACTION_SPECS[action]["parameters"])
    if set(arguments) - allowed:
        return None, f"Unbekannte Parameter fuer {action}."
    if action == "alarm.set":
        if set(arguments) != {"hour", "minute"}:
            return None, "alarm.set braucht hour und minute."
        if not (_valid_int(arguments["hour"], 0, 23) and _valid_int(arguments["minute"], 0, 59)):
            return None, "alarm.set: hour 0-23, minute 0-59."
    elif action == "timer.set":
        if set(arguments) != {"seconds"} or not _valid_int(arguments["seconds"], 1, 86400):
            return None, "timer.set: seconds 1-86400."
    else:
        if "destination" not in arguments or not _valid_destination(arguments["destination"]):
            return None, "maps.open: gueltiges destination (kein URI/Paket) noetig."
        if "travel_mode" in arguments and arguments["travel_mode"] not in (
                "driving", "walking", "transit", "bicycling", "two-wheeler"):
            return None, "maps.open: unbekannter travel_mode."
    try:
        encoded = json.dumps(arguments, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError):
        return None, "arguments nicht serialisierbar."
    if len(encoded) > PHONE_ACTIONS_MAX_BYTES:
        return None, "arguments ueberschreiten 8 KiB."
    return dict(arguments), None


def _actions_origin() -> tuple[object | None, str | None]:
    """The turn's own authenticated, v1-registered socket — never the newest global phone."""
    origin = turn_origin_transport()
    if origin is None:
        return None, "Kein nachweisbarer Ursprungs-Socket fuer diesen Turn."
    if getattr(origin, "_closed", False):
        return None, "Ursprungs-Socket ist getrennt."
    from gateway.session_identity import is_authenticated_identity
    if not is_authenticated_identity(getattr(origin, "auth_identity", None)):
        return None, "Ursprungs-Socket ist nicht authentifiziert."
    if not server_requests.phone_actions_registered(origin):
        return None, "Native Aktionen v1 sind auf diesem Socket nicht registriert."
    return origin, None


def _actions_list_payload(result) -> dict:
    if not isinstance(result, dict) or result.get("status") != "ok":
        return {"status": "unavailable"}
    if result.get("version", 1) != 1:
        return {"status": "unavailable"}
    actions = result.get("actions")
    if not isinstance(actions, list) or len(actions) > PHONE_ACTIONS_MAX_LIST:
        return {"status": "unavailable"}
    ids: list[str] = []
    for item in actions:
        aid = item if isinstance(item, str) else (
            item.get("id") if isinstance(item, dict) and set(item) == {"id"} else None)
        if not isinstance(aid, str) or aid not in _ACTION_SPECS:
            return {"status": "unavailable"}  # unknown id/shape: fail closed, no partial list
        if aid not in ids:
            ids.append(aid)
    return {"status": "ok", "version": 1,
            "actions": [{"id": aid, **_ACTION_SPECS[aid]} for aid in ids]}


def _actions_run_payload(action: str, result) -> dict:
    """Typed, bounded result. An unknown/invalid app answer is never success."""
    status = result.get("status") if isinstance(result, dict) else None
    if not isinstance(status, str) or status not in _STATUSES:
        status = "unknown_outcome"
    return {"status": status, "action": action}


def _actions_request(method: str, origin, params: dict) -> dict | None:
    sid = _approval_session_id.get()
    if not sid:
        return None
    return server_requests.send(method, sid, dict(params, version=1),
                                timeout=PHONE_ACTIONS_TIMEOUT_S, transport=origin)


def phone_actions(args=None, **kwargs) -> str:
    """List/run vetted native actions on the turn's own authenticated Conduit socket."""
    merged = _merged(args, kwargs)
    operation = merged.get("operation")
    origin, refusal = _actions_origin()
    if refusal is not None:
        return _actions_fail(refusal)

    if operation == "list":
        started = time.monotonic()
        result = _actions_request("phone.actions.list", origin, {})
        payload = _actions_list_payload(result)
        logger.info("phone actions list status=%s duration_ms=%d",
                    payload["status"], (time.monotonic() - started) * 1000)
        return tool_result(payload)

    if operation != "run":
        return tool_result({"status": "invalid_arguments", "error": "operation muss list oder run sein."})
    action = merged.get("action")
    if not isinstance(action, str) or action not in _ACTION_SPECS:
        return tool_result({"status": "invalid_arguments", "error": "Unbekannte Aktion."})
    arguments, problem = _validate_arguments(action, merged.get("arguments"))
    if problem is not None:
        return tool_result({"status": "invalid_arguments", "error": problem})

    started = time.monotonic()
    # Exactly one send; the srq id is the one-shot correlation key. No retry on timeout/disconnect:
    # an uncertain outcome is reported as unknown_outcome, never as success and never replayed.
    result = _actions_request("phone.actions.run", origin,
                              {"action": action, "arguments": arguments})
    payload = (_actions_run_payload(action, result) if result is not None
               else {"status": "unknown_outcome", "action": action})
    logger.info("phone action=%s status=%s duration_ms=%d",
                action, payload["status"], (time.monotonic() - started) * 1000)
    return tool_result(payload)


registry.register("phone_actions", "phone_control",
                  {"type": "function", "parameters": {
                      "type": "object",
                      "properties": {
                          "operation": {"type": "string", "enum": ["list", "run"],
                                        "description": "list = freigegebene Aktionen abfragen; run = eine Aktion ausfuehren"},
                          "action": {"type": "string", "enum": sorted(_ACTION_SPECS),
                                     "description": "Nur bei operation=run"},
                          "arguments": {"type": "object",
                                        "description": "Nur bei operation=run; strikt pro Aktion validiert"}},
                      "required": ["operation"]}}, phone_actions,
                  description="Conduit native actions v1 (Alarm, Timer, Karten) auf genau dem "
                              "authentifizierten Socket, der diesen Turn eingereicht hat. list ohne "
                              "Nebenwirkung; run wird nie automatisch wiederholt.")
