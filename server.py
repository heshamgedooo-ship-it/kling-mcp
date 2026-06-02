#!/usr/bin/env python3
import asyncio
import os
import secrets
import time
import httpx
import jwt as pyjwt

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.provider import (
    OAuthAuthorizationServerProvider,
    AuthorizationCode,
    AuthorizationParams,
    AccessToken,
    RefreshToken,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

# ── Config ────────────────────────────────────────────────────────────────────

KLING_ACCESS_KEY = os.environ.get("KLING_ACCESS_KEY", "")
KLING_SECRET_KEY = os.environ.get("KLING_SECRET_KEY", "")
KLING_BASE_URL = "https://api.klingai.com"

POLLO_API_KEY = os.environ.get("POLLO_API_KEY", "")
POLLO_BASE_URL = "https://pollo.ai/api/platform"

SERVER_URL = os.environ.get("SERVER_URL", "https://kling-mcp-production.up.railway.app")

# ── OAuth provider (auto-approves everything) ─────────────────────────────────

class AutoApproveOAuthProvider(OAuthAuthorizationServerProvider):
    def __init__(self):
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.auth_codes: dict[str, dict] = {}
        self.tokens: dict[str, dict] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        code = secrets.token_urlsafe(32)
        self.auth_codes[code] = {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "code_challenge": params.code_challenge,
            "expires_at": time.time() + 600,
            "scopes": params.scopes or [],
        }
        redirect = str(params.redirect_uri)
        sep = "&" if "?" in redirect else "?"
        redirect += f"{sep}code={code}"
        if params.state:
            redirect += f"&state={params.state}"
        return redirect

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        data = self.auth_codes.get(authorization_code)
        if not data or data["expires_at"] < time.time():
            return None
        return AuthorizationCode(
            code=authorization_code,
            client_id=client.client_id,
            redirect_uri=AnyUrl(data["redirect_uri"]),
            redirect_uri_provided_explicitly=True,
            expires_at=data["expires_at"],
            scopes=data["scopes"],
            code_challenge=data["code_challenge"],
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        self.auth_codes.pop(authorization_code.code, None)
        token = secrets.token_urlsafe(32)
        self.tokens[token] = {
            "client_id": client.client_id,
            "expires_at": time.time() + 86400 * 30,
            "scopes": authorization_code.scopes,
        }
        return OAuthToken(access_token=token, token_type="bearer", expires_in=86400 * 30)

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self.tokens.get(token)
        if not data or data["expires_at"] < time.time():
            return None
        return AccessToken(token=token, client_id=data["client_id"], scopes=data["scopes"], expires_at=int(data["expires_at"]))

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        return None

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list) -> OAuthToken:
        raise NotImplementedError

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            self.tokens.pop(token.token, None)


# ── FastMCP setup ─────────────────────────────────────────────────────────────

oauth_provider = AutoApproveOAuthProvider()
PORT = int(os.environ.get("PORT", 8000))

mcp = FastMCP(
    "kling-pollo",
    host="0.0.0.0",
    port=PORT,
    auth_server_provider=oauth_provider,
    auth=AuthSettings(
        issuer_url=AnyUrl(SERVER_URL),
        resource_server_url=AnyUrl(SERVER_URL),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["mcp"],
            default_scopes=["mcp"],
        ),
    ),
)


# ── Kling helpers ─────────────────────────────────────────────────────────────

def kling_token() -> str:
    now = int(time.time())
    return pyjwt.encode(
        {"iss": KLING_ACCESS_KEY, "exp": now + 1800, "nbf": now - 5},
        KLING_SECRET_KEY, algorithm="HS256"
    )

def kling_headers() -> dict:
    return {"Authorization": f"Bearer {kling_token()}", "Content-Type": "application/json"}

async def kling_wait(client, endpoint, task_id, max_wait=300):
    url = f"{KLING_BASE_URL}{endpoint}/{task_id}"
    for _ in range(max_wait // 5):
        await asyncio.sleep(5)
        res = await client.get(url, headers=kling_headers())
        data = res.json()
        status = data.get("data", {}).get("task_status", "")
        if status == "succeed":
            return data
        if status == "failed":
            raise Exception(f"Task failed: {data.get('data', {}).get('task_status_msg', 'unknown')}")
    raise Exception("Timeout after 5 minutes")

def kling_image_url(data):
    works = data.get("data", {}).get("task_result", {}).get("images", [])
    return works[0].get("url", "") if works else ""

def kling_video_url(data):
    works = data.get("data", {}).get("task_result", {}).get("videos", [])
    return works[0].get("url", "") if works else ""


# ── Pollo helpers ─────────────────────────────────────────────────────────────

def pollo_headers() -> dict:
    return {"x-api-key": POLLO_API_KEY, "Content-Type": "application/json"}

async def pollo_wait(client, task_id, max_wait=300):
    url = f"{POLLO_BASE_URL}/generation/{task_id}/status"
    for _ in range(max_wait // 5):
        await asyncio.sleep(5)
        res = await client.get(url, headers=pollo_headers())
        data = res.json()
        status = data.get("status", "")
        if status == "succeed":
            return data
        if status == "failed":
            raise Exception(f"Task failed: {data}")
    raise Exception("Timeout after 5 minutes")


# ── Kling Tools ───────────────────────────────────────────────────────────────

@mcp.tool()
async def kling_generate_image(prompt: str, negative_prompt: str = "", aspect_ratio: str = "1:1", model: str = "kling-v1-5", n: int = 1) -> str:
    """Generate an image using Kling AI. Models: kling-v1, kling-v1-5, kling-v2."""
    async with httpx.AsyncClient(timeout=30) as client:
        payload = {"model_name": model, "prompt": prompt, "aspect_ratio": aspect_ratio, "n": n}
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        res = await client.post(f"{KLING_BASE_URL}/v1/images/generations", headers=kling_headers(), json=payload)
        data = res.json()
        task_id = data.get("data", {}).get("task_id")
        if not task_id:
            return f"API Error: {data}"
        result = await kling_wait(client, "/v1/images/generations", task_id)
        url = kling_image_url(result)
        return f"Image generated!\nURL: {url}\nTask ID: {task_id}" if url else f"Done\n{result}"


@mcp.tool()
async def kling_generate_video(prompt: str, negative_prompt: str = "", duration: str = "5", aspect_ratio: str = "16:9", model: str = "kling-v1-6", mode: str = "std") -> str:
    """Generate a video using Kling AI. Models: kling-v1, kling-v1-5, kling-v1-6."""
    async with httpx.AsyncClient(timeout=30) as client:
        payload = {"model_name": model, "prompt": prompt, "duration": duration, "aspect_ratio": aspect_ratio, "mode": mode}
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        res = await client.post(f"{KLING_BASE_URL}/v1/videos/text2video", headers=kling_headers(), json=payload)
        data = res.json()
        task_id = data.get("data", {}).get("task_id")
        if not task_id:
            return f"API Error: {data}"
        result = await kling_wait(client, "/v1/videos/text2video", task_id, max_wait=300)
        url = kling_video_url(result)
        return f"Video generated!\nURL: {url}\nTask ID: {task_id}" if url else f"Done\n{result}"


@mcp.tool()
async def kling_animate_image(image_url: str, prompt: str, negative_prompt: str = "", duration: str = "5", model: str = "kling-v1-6", mode: str = "std") -> str:
    """Animate an image into a video using Kling AI (image-to-video)."""
    async with httpx.AsyncClient(timeout=30) as client:
        payload = {"model_name": model, "image": image_url, "prompt": prompt, "duration": duration, "mode": mode}
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        res = await client.post(f"{KLING_BASE_URL}/v1/videos/image2video", headers=kling_headers(), json=payload)
        data = res.json()
        task_id = data.get("data", {}).get("task_id")
        if not task_id:
            return f"API Error: {data}"
        result = await kling_wait(client, "/v1/videos/image2video", task_id, max_wait=300)
        url = kling_video_url(result)
        return f"Video generated!\nURL: {url}\nTask ID: {task_id}" if url else f"Done\n{result}"


@mcp.tool()
async def kling_check_credits() -> str:
    """Check remaining Kling AI credits."""
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(f"{KLING_BASE_URL}/v1/account/costs", headers=kling_headers())
        return str(res.json())


# ── Pollo Tools ───────────────────────────────────────────────────────────────

@mcp.tool()
async def pollo_generate_video(
    prompt: str,
    model: str = "pollo-v1-6",
    aspect_ratio: str = "16:9",
    resolution: str = "720p",
    length: int = 5,
    mode: str = "basic",
    negative_prompt: str = "",
) -> str:
    """Generate a video using Pollo AI. Supports 50+ models: pollo-v2-0, pollo-v1-6, kling-v3, veo3, sora-2-pro, runway-gen4, hailuo-01, pika-2-2, wan-2-6 and more."""
    async with httpx.AsyncClient(timeout=30) as client:
        payload: dict = {"input": {"prompt": prompt, "aspectRatio": aspect_ratio, "resolution": resolution, "length": length, "mode": mode}}
        if negative_prompt:
            payload["input"]["negativePrompt"] = negative_prompt
        res = await client.post(f"{POLLO_BASE_URL}/generation/pollo/{model}", headers=pollo_headers(), json=payload)
        data = res.json()
        task_id = data.get("taskId")
        if not task_id:
            return f"API Error: {data}"
        result = await pollo_wait(client, task_id)
        video_url = result.get("output", {}).get("url") or result.get("url", "")
        return f"Video generated!\nURL: {video_url}\nTask ID: {task_id}" if video_url else f"Done\n{result}"


@mcp.tool()
async def pollo_animate_image(
    image_url: str,
    prompt: str = "",
    model: str = "pollo-v1-6",
    resolution: str = "720p",
    length: int = 5,
    mode: str = "basic",
) -> str:
    """Animate an image into a video using Pollo AI (image-to-video). Supports 50+ models."""
    async with httpx.AsyncClient(timeout=30) as client:
        payload: dict = {"input": {"image": image_url, "resolution": resolution, "length": length, "mode": mode}}
        if prompt:
            payload["input"]["prompt"] = prompt
        res = await client.post(f"{POLLO_BASE_URL}/generation/pollo/{model}", headers=pollo_headers(), json=payload)
        data = res.json()
        task_id = data.get("taskId")
        if not task_id:
            return f"API Error: {data}"
        result = await pollo_wait(client, task_id)
        video_url = result.get("output", {}).get("url") or result.get("url", "")
        return f"Video generated!\nURL: {video_url}\nTask ID: {task_id}" if video_url else f"Done\n{result}"


@mcp.tool()
async def pollo_check_task(task_id: str) -> str:
    """Check the status of a Pollo AI generation task."""
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(f"{POLLO_BASE_URL}/generation/{task_id}/status", headers=pollo_headers())
        return str(res.json())


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
