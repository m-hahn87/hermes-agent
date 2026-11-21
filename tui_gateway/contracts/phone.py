"""Phone Control requests sent to the Android client."""

from pydantic import Field

from .base import JsonValue, Result
from .registry import server_request
from .server_requests import ServerRequestParams


class PhoneNodeParams(ServerRequestParams):
    node_index: int = Field(ge=0, le=2**31 - 1, strict=True)


class PhoneTypeParams(PhoneNodeParams):
    text: str


class PhoneSwipeParams(PhoneNodeParams):
    delta_x: int = Field(ge=-(2**31), le=2**31 - 1, strict=True)
    delta_y: int = Field(ge=-(2**31), le=2**31 - 1, strict=True)


class PhoneError(Result):
    code: str


class PhoneActionResult(Result):
    observation: str | None = None
    error: PhoneError | None = None


class PhoneScreenResult(Result):
    nodes: list[dict[str, JsonValue]] | None = None
    error: PhoneError | None = None


class PhoneScreenshotResult(Result):
    data_url: str | None = None
    width: int | None = None
    height: int | None = None
    error: PhoneError | None = None


PHONE_CONTRACTS = {
    "screen": (ServerRequestParams, PhoneScreenResult, "Read the current Android accessibility node list."),
    "tap": (PhoneNodeParams, PhoneActionResult, "Tap a node by its index from the latest phone_screen."),
    "type": (PhoneTypeParams, PhoneActionResult, "Replace the text of an editable node from the latest phone_screen."),
    "swipe": (PhoneSwipeParams, PhoneActionResult, "Swipe from a node center by signed pixel deltas."),
    "back": (ServerRequestParams, PhoneActionResult, "Perform the Android Back action."),
    "screenshot": (ServerRequestParams, PhoneScreenshotResult, "Capture a JPEG image of the phone screen."),
}

for name, (params, result, description) in PHONE_CONTRACTS.items():
    server_request(f"phone.{name}", params=params, result=result, doc=description)
