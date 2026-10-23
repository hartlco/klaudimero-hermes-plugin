"""Hermes-mounted API. The host authenticates every route before dispatch."""
from functools import lru_cache
import hashlib
import json
import re
import shutil
import subprocess

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from . import VERSION, PROTOCOL
from .contracts import Problem, appearance, identifier, request_id
from .delivery import ManagedRelay, Relay, Sender
from .storage import Store

REPOSITORY = "https://github.com/hartlco/klaudimero-hermes-plugin"
class PrivateRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def handle(request):
            try:
                return await handler(request)
            except RequestValidationError:
                # Validation errors must not echo a device token or image bytes.
                return JSONResponse({"detail": "invalid_request"}, status_code=422)
        return handle

router = APIRouter(route_class=PrivateRoute)


@lru_cache(maxsize=8)
def service(directory):
    store = Store(directory)
    return store, Sender(store)


def services():
    from .storage import data_directory
    return service(str(data_directory()))


def owner(request: Request):
    session = getattr(request.state, "session", None)
    principal = getattr(request.state, "token_principal", None)
    native = getattr(request.state, "native_http_principal", None)
    if session is not None:
        identity = ["session", session.provider, getattr(session, "issuer", ""), session.user_id]
    elif principal is not None:
        identity = ["service", principal.provider, principal.principal]
    elif native is not None and native.get("subject"):
        identity = ["native", native["subject"]]
    elif getattr(request.app.state, "auth_required", None) is False and request.headers.get("authorization", "").startswith("Bearer "):
        # In legacy loopback mode Hermes has already validated its single owner
        # token in the outer middleware. Partition by its digest, never persist it.
        identity = ["legacy-owner", request.headers["authorization"]]
    else:
        raise HTTPException(401, detail="Verified dashboard identity required")
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def call(operation, *args):
    try:
        return operation(*args)
    except Problem as error:
        raise HTTPException(error.status, detail=error.code) from None


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AvatarUpdate(Body):
    expected_revision: StrictInt = Field(ge=0)
    request_id: str = Field(max_length=40)
    appearance: dict


class Subscription(Body):
    profile_id: str = Field(min_length=1, max_length=180)
    session_id: str = Field(min_length=1, max_length=180)


class Device(Body):
    device_token: str | None = Field(default=None, min_length=32, max_length=512, pattern=r"^[0-9a-fA-F]+$")
    environment: str | None = Field(default=None, pattern=r"^(production|sandbox)$")
    relay_grant_id: str | None = Field(default=None, min_length=36, max_length=36)
    sessions: list[Subscription] = Field(max_length=1000)
    encryption: dict


class ProfileSetup(Body):
    profile_ids: list[str] = Field(max_length=16)
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")


@router.get("/capabilities")
def capabilities(response: Response, account=Depends(owner)):
    from .push_crypto import ALGORITHM
    response.headers["Cache-Control"] = "no-store"
    store, sender = services()
    sender.start()
    from .relay_auth import RELAY_URL
    return {"protocol": PROTOCOL, "version": VERSION, "server_id": store.server_id,
            "account_id": account, "avatar_sync": True, "push_relay_configured": Relay().legacy_configured or store.has_managed_grant(account),
            "push_relay": {"protocol": 2, "url": RELAY_URL},
            "avatar_revision": store.avatar_revision(account),
            "push_content": "encrypted", "push_encryption": ALGORITHM,
            "push_signing_key": store.push_signing_public_key, "device_lease_days": 30}


@router.get("/avatars")
def get_avatars(response: Response, account=Depends(owner)):
    response.headers["Cache-Control"] = "no-store"
    return {"protocol": PROTOCOL, "avatars": services()[0].avatars(account)}


@router.put("/avatars/{profile_id}")
def put_avatar(profile_id: str, body: AvatarUpdate, response: Response, account=Depends(owner)):
    response.headers["Cache-Control"] = "no-store"
    return call(services()[0].put_avatar, account, call(identifier, profile_id), body.expected_revision,
                call(request_id, body.request_id), call(appearance, body.appearance))


@router.put("/devices/{device_id}")
def register_device(device_id: str, body: Device, response: Response, account=Depends(owner)):
    from .push_crypto import decode_public_key
    response.headers["Cache-Control"] = "no-store"
    sessions = [{"profile_id": call(identifier, s.profile_id), "session_id": call(identifier, s.session_id)} for s in body.sessions]
    if set(body.encryption) != {"version", "public_key"} or type(body.encryption["version"]) is not int or body.encryption["version"] != 2:
        raise HTTPException(422, detail="encrypted_push_required")
    public_key = body.encryption["public_key"]
    call(decode_public_key, public_key)
    store, sender = services()
    if body.relay_grant_id:
        if body.device_token is not None or body.environment is not None:
            raise HTTPException(422, detail="ambiguous_registration")
        import uuid
        from .push_crypto import key_id
        try:
            grant = str(uuid.UUID(body.relay_grant_id))
        except ValueError:
            raise HTTPException(422, detail="invalid_relay_grant") from None
        call(ManagedRelay(store).verify_grant, grant, key_id(decode_public_key(public_key)))
        call(store.register_device, account, call(request_id, device_id), "", "", sessions, public_key, grant)
    else:
        if not body.device_token or len(body.device_token) % 2 or not body.environment:
            raise HTTPException(422, detail="invalid_device_token")
        call(store.register_device, account, call(request_id, device_id), body.device_token.lower(), body.environment, sessions, public_key)
    sender.start()
    return {"registered": True, "delivery_configured": bool(body.relay_grant_id) or Relay().legacy_configured}


@router.delete("/devices/{device_id}")
def remove_device(device_id: str, response: Response, account=Depends(owner)):
    response.headers["Cache-Control"] = "no-store"
    store, sender = services()
    store.remove_device(account, call(request_id, device_id))
    sender.start()
    return {"ok": True}


@router.post("/profiles/enable")
def enable_profiles(body: ProfileSetup, account=Depends(owner)):
    """Use Hermes's public CLI to install/enable this exact package for named bots.

    No shell, arbitrary repository, overwrite flag, or gateway restart is accepted.
    A partially completed call can be resumed without touching existing packages.
    """
    from hermes_cli.profiles import get_profile_dir, list_profile_names
    executable = shutil.which("hermes")
    if not executable:
        raise HTTPException(503, detail="hermes_cli_unavailable")
    known = set(list_profile_names()) | {"default"}
    if any(p not in known for p in body.profile_ids):
        raise HTTPException(404, detail="profile_not_found")
    results = []
    for profile in dict.fromkeys(body.profile_ids):
        manifest = get_profile_dir(profile) / "plugins" / "klaudimero" / "plugin.yaml"
        installed = manifest.is_file()
        if installed:
            import yaml
            try:
                current = yaml.safe_load(manifest.read_text())
                if not isinstance(current, dict) or str(current.get("version")) != VERSION:
                    results.append({"profile_id": profile, "enabled": False})
                    continue
            except (OSError, ValueError, yaml.YAMLError):
                results.append({"profile_id": profile, "enabled": False})
                continue
        arguments = [executable, "-p", profile, "plugins"]
        arguments += ["enable", "klaudimero"] if installed else ["install", REPOSITORY, "--ref", body.revision, "--enable"]
        try:
            result = subprocess.run(arguments, capture_output=True, timeout=120, check=False)
            results.append({"profile_id": profile, "enabled": result.returncode == 0})
        except (OSError, subprocess.TimeoutExpired):
            results.append({"profile_id": profile, "enabled": False})
    return {"profiles": results, "gateway_restart_may_be_required": True}

