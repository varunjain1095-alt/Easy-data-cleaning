"""NaN/Inf-safe JSON responses.

json.dumps with allow_nan=False (Starlette's default) raises on NaN floats,
turning any dataset containing NaN into a 500. NaN and ±Inf are serialized
as null instead.
"""

import json
import math

from fastapi.responses import JSONResponse


def json_safe(value):
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


class SafeJSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(
            json_safe(content),
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
        ).encode("utf-8")
