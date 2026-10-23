"""Version-one wire validation. No prompts, credentials, or user text in pushes."""
import base64
import json
import uuid


class Problem(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def identifier(value, limit=180):
    if not isinstance(value, str) or not value or len(value.encode()) > limit or any(ord(c) < 32 for c in value):
        raise Problem(422, "invalid_identifier")
    return value


def request_id(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise Problem(422, "invalid_request_id") from None


def appearance(value):
    if not isinstance(value, dict) or set(value) - {"symbol", "color", "image", "style", "chick"}:
        raise Problem(422, "invalid_appearance")
    if value.get("style") not in {"chick", "symbol", "photo"}:
        raise Problem(422, "invalid_appearance")
    for key in ("symbol", "color"):
        identifier(value.get(key), 100)
    image = value.get("image")
    if image is not None:
        try:
            if not isinstance(image, str) or len(image) > 1_400_000:
                raise ValueError()
            raw = base64.b64decode(image, validate=True)
            if not raw or len(raw) > 1_048_576:
                raise ValueError()
        except (ValueError, TypeError):
            raise Problem(422, "invalid_avatar_image") from None
    if value["style"] == "photo" and not image:
        raise Problem(422, "missing_avatar_image")
    chick = value.get("chick")
    colors = {"body", "shell", "eyes", "beak", "accent", "background"}
    choices = {
        "expression": {"classic", "happy", "wink", "sleepy", "surprised"},
        "beakStyle": {"classic", "smile", "tiny"},
        "shellStyle": {"plain", "speckled", "striped", "none"},
        "accessory": {"none", "glasses", "sunglasses", "headphones", "bow", "flower", "crown"},
        "backdrop": {"solid", "gradient", "confetti", "clear"},
    }
    if not isinstance(chick, dict) or set(chick) != colors | set(choices) | {"cheeks"}:
        raise Problem(422, "invalid_chick")
    for key in colors:
        color = chick[key]
        if not isinstance(color, dict) or set(color) != {"rgb"} or type(color["rgb"]) is not int or not 0 <= color["rgb"] <= 0xFFFFFF:
            raise Problem(422, "invalid_chick_color")
    if type(chick["cheeks"]) is not bool or any(not isinstance(chick[key], str) or chick[key] not in values for key, values in choices.items()):
        raise Problem(422, "invalid_chick")
    return json.loads(json.dumps(value, allow_nan=False))

