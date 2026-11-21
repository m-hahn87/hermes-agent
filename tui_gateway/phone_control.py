"""Route Phone Control to one capable socket attached to the running chat."""

from pydantic import ValidationError

from tui_gateway import server_requests
from tui_gateway.contracts.phone import PHONE_CONTRACTS
from tui_gateway.contracts.registry import SERVER_REQUESTS

PHONE_TIMEOUT = 10


def request(sid, method, args, peers):
    phones = [peer for peer in peers if server_requests.answers_phone(peer)]
    if not phones:
        return {"error": "offline"}
    if len(phones) != 1:
        return {"error": "ambiguous"}
    contract = SERVER_REQUESTS[method]
    if "session_id" in args:
        return {"error": "invalid_params"}
    try:
        contract.params.model_validate({**args, "session_id": sid})
    except ValidationError:
        return {"error": "invalid_params"}
    transport = phones[0]
    result = server_requests.send(method, sid, args, timeout=PHONE_TIMEOUT, transport=transport)
    if result is None:
        return {"error": "timeout" if server_requests.answers_phone(transport) else "offline"}
    try:
        model = PHONE_CONTRACTS[method.removeprefix("phone.")][1]
        return model.model_validate(result).model_dump(exclude_none=True)
    except ValidationError:
        return {"error": "invalid_response"}
