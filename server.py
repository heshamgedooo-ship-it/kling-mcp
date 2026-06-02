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

# ── OAuth provider ────────────────────────────────────────────────────────────

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
        self.tokens[token] = {"client_id": client.client_id, "expires_at": time.time() + 86400 * 30, "scopes": authorization_code.scopes}
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


# ── FastMCP ───────────────────────────────────────────────────────────────────

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
            enabled=True, valid_scopes=["mcp"], default_scopes=["mcp"],
        ),
    ),
)

# ── Kling helpers ─────────────────────────────────────────────────────────────

def kling_token() -> str:
    now = int(time.time())
    return pyjwt.encode({"iss": KLING_ACCESS_KEY, "exp": now + 1800, "nbf": now - 5}, KLING_SECRET_KEY, algorithm="HS256")

def kling_headers() -> dict:
    return {"Authorization": f"Bearer {kling_token()}", "Content-Type": "application/json"}

async def kling_wait(client: httpx.AsyncClient, endpoint: str, task_id: str, max_wait: int = 300) -> dict:
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

def kling_image_url(data: dict) -> str:
    works = data.get("data", {}).get("task_result", {}).get("images", [])
    return works[0].get("url", "") if works else ""

def kling_video_url(data: dict) -> str:
    works = data.get("data", {}).get("task_result", {}).get("videos", [])
    return works[0].get("url", "") if works else ""

# ── Pollo helpers ─────────────────────────────────────────────────────────────

def pollo_headers() -> dict:
    return {"x-api-key": POLLO_API_KEY, "Content-Type": "application/json"}

async def pollo_wait(client: httpx.AsyncClient, task_id: str, max_wait: int = 300) -> dict:
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


# ════════════════════════════════════════════════════════════════════════════════
# KLING TOOLS (11)
# ════════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def kling_generate_image(
    prompt: str,
    negative_prompt: str = "",
    aspect_ratio: str = "1:1",
    model: str = "kling-v1-5",
    n: int = 1,
) -> str:
    """Generate an image using Kling AI KOLORS model.
    aspect_ratio: 1:1, 16:9, 9:16, 4:3, 3:4, 3:2, 2:3
    model: kling-v1, kling-v1-5, kling-v2"""
    async with httpx.AsyncClient(timeout=30) as client:
        payload = {"model_name": model, "prompt": prompt, "aspect_ratio": aspect_ratio, "n": n}
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        res = await client.post(f"{KLING_BASE_URL}/v1/images/generations", headers=kling_headers(), json=payload)
        data = res.json()
        task_id = data.get("data", {}).get("task_id")
        if not task_id:
            return f"Error: {data}"
        result = await kling_wait(client, "/v1/images/generations", task_id)
        url = kling_image_url(result)
        return f"Image ready!\nURL: {url}\nTask ID: {task_id}" if url else f"Done\n{result}"


@mcp.tool()
async def kling_generate_video(
    prompt: str,
    negative_prompt: str = "",
    duration: str = "5",
    aspect_ratio: str = "16:9",
    model: str = "kling-v1-6",
    mode: str = "std",
) -> str:
    """Generate a video from text using Kling AI.
    duration: 5 or 10 seconds
    aspect_ratio: 16:9, 9:16, 1:1
    model: kling-v1, kling-v1-5, kling-v1-6
    mode: std (free) or pro"""
    async with httpx.AsyncClient(timeout=30) as client:
        payload = {"model_name": model, "prompt": prompt, "duration": duration, "aspect_ratio": aspect_ratio, "mode": mode}
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        res = await client.post(f"{KLING_BASE_URL}/v1/videos/text2video", headers=kling_headers(), json=payload)
        data = res.json()
        task_id = data.get("data", {}).get("task_id")
        if not task_id:
            return f"Error: {data}"
        result = await kling_wait(client, "/v1/videos/text2video", task_id)
        url = kling_video_url(result)
        return f"Video ready!\nURL: {url}\nTask ID: {task_id}" if url else f"Done\n{result}"


@mcp.tool()
async def kling_animate_image(
    image_url: str,
    prompt: str,
    negative_prompt: str = "",
    duration: str = "5",
    model: str = "kling-v1-6",
    mode: str = "std",
) -> str:
    """Animate a static image into a video using Kling AI (image-to-video).
    duration: 5 or 10 seconds
    mode: std or pro"""
    async with httpx.AsyncClient(timeout=30) as client:
        payload = {"model_name": model, "image": image_url, "prompt": prompt, "duration": duration, "mode": mode}
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        res = await client.post(f"{KLING_BASE_URL}/v1/videos/image2video", headers=kling_headers(), json=payload)
        data = res.json()
        task_id = data.get("data", {}).get("task_id")
        if not task_id:
            return f"Error: {data}"
        result = await kling_wait(client, "/v1/videos/image2video", task_id)
        url = kling_video_url(result)
        return f"Video ready!\nURL: {url}\nTask ID: {task_id}" if url else f"Done\n{result}"


@mcp.tool()
async def kling_extend_video(
    task_id: str,
    prompt: str = "",
    duration: str = "5",
    model: str = "kling-v1-6",
) -> str:
    """Extend an existing Kling video by 4-5 more seconds.
    task_id: the original video's task ID"""
    async with httpx.AsyncClient(timeout=30) as client:
        payload = {"task_id": task_id, "duration": duration, "model_name": model}
        if prompt:
            payload["prompt"] = prompt
        res = await client.post(f"{KLING_BASE_URL}/v1/video/extension", headers=kling_headers(), json=payload)
        data = res.json()
        new_task_id = data.get("data", {}).get("task_id")
        if not new_task_id:
            return f"Error: {data}"
        result = await kling_wait(client, "/v1/videos/text2video", new_task_id)
        url = kling_video_url(result)
        return f"Extended video ready!\nURL: {url}\nTask ID: {new_task_id}" if url else f"Done\n{result}"


@mcp.tool()
async def kling_lipsync(
    video_url: str,
    mode: str = "audio2video",
    audio_url: str = "",
    text: str = "",
    voice_id: str = "",
    voice_language: str = "en",
    voice_speed: float = 1.0,
) -> str:
    """Sync lips in a video to audio or text-to-speech using Kling AI.
    mode: audio2video (provide audio_url) or text2video (provide text + voice_id)
    For text2video, get voice IDs from Kling AI docs."""
    async with httpx.AsyncClient(timeout=30) as client:
        inp: dict = {"video_url": video_url, "mode": mode}
        if mode == "audio2video":
            inp["audio_type"] = "url"
            inp["audio_url"] = audio_url
        else:
            inp["text"] = text
            inp["voice_id"] = voice_id
            inp["voice_language"] = voice_language
            inp["voice_speed"] = voice_speed
        res = await client.post(f"{KLING_BASE_URL}/v1/videos/lip-sync", headers=kling_headers(), json={"input": inp})
        data = res.json()
        task_id = data.get("data", {}).get("task_id")
        if not task_id:
            return f"Error: {data}"
        result = await kling_wait(client, "/v1/videos/lip-sync", task_id)
        url = kling_video_url(result)
        return f"Lipsync video ready!\nURL: {url}\nTask ID: {task_id}" if url else f"Done\n{result}"


@mcp.tool()
async def kling_video_effects(
    image_urls: list[str],
    effect_scene: str,
    model: str = "kling-v1-6",
    duration: str = "5",
) -> str:
    """Apply a special effect to images to create a video using Kling AI.
    effect_scene options: hug, kiss, heart_gesture, squish, expansion, fuzzyfuzzy, bloombloom, dizzydizzy
    image_urls: list of 1-2 image URLs (some effects need 2 people)"""
    async with httpx.AsyncClient(timeout=30) as client:
        payload = {"input": {"image_urls": image_urls, "effect_scene": effect_scene, "duration": duration, "model_name": model}}
        res = await client.post(f"{KLING_BASE_URL}/v1/videos/effects", headers=kling_headers(), json=payload)
        data = res.json()
        task_id = data.get("data", {}).get("task_id")
        if not task_id:
            return f"Error: {data}"
        result = await kling_wait(client, "/v1/videos/effects", task_id)
        url = kling_video_url(result)
        return f"Effect video ready!\nURL: {url}\nTask ID: {task_id}" if url else f"Done\n{result}"


@mcp.tool()
async def kling_virtual_try_on(
    person_image_url: str,
    cloth_image_urls: list[str],
    model: str = "kolors-virtual-try-on-v1.5",
) -> str:
    """Virtual clothing try-on using Kling AI — place clothes onto a person's image.
    person_image_url: URL of the person photo
    cloth_image_urls: list of 1-5 clothing item image URLs
    model: kolors-virtual-try-on-v1 or kolors-virtual-try-on-v1.5"""
    async with httpx.AsyncClient(timeout=30) as client:
        payload = {"model_name": model, "person_image_url": person_image_url, "cloth_image_urls": cloth_image_urls}
        res = await client.post(f"{KLING_BASE_URL}/v1/virtual-try-on", headers=kling_headers(), json=payload)
        data = res.json()
        task_id = data.get("data", {}).get("task_id")
        if not task_id:
            return f"Error: {data}"
        result = await kling_wait(client, "/v1/virtual-try-on", task_id)
        url = kling_image_url(result)
        return f"Try-on result ready!\nURL: {url}\nTask ID: {task_id}" if url else f"Done\n{result}"


@mcp.tool()
async def kling_check_task(task_id: str, task_type: str = "text2video") -> str:
    """Check the status of any Kling AI task without waiting.
    task_type: text2video, image2video, lip-sync, effects, virtual-try-on, images/generations"""
    async with httpx.AsyncClient(timeout=30) as client:
        url = f"{KLING_BASE_URL}/v1/videos/{task_type}/{task_id}"
        res = await client.get(url, headers=kling_headers())
        data = res.json()
        status = data.get("data", {}).get("task_status", "unknown")
        video_url = kling_video_url(data) or kling_image_url(data)
        result = f"Status: {status}"
        if video_url:
            result += f"\nURL: {video_url}"
        return result


@mcp.tool()
async def kling_list_tasks(task_type: str = "text2video", page: int = 1, page_size: int = 10) -> str:
    """List all Kling AI generation tasks.
    task_type: text2video, image2video, images/generations, lip-sync, effects"""
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(
            f"{KLING_BASE_URL}/v1/videos/{task_type}",
            headers=kling_headers(),
            params={"pageNum": page, "pageSize": page_size},
        )
        return str(res.json())


@mcp.tool()
async def kling_check_credits() -> str:
    """Check remaining Kling AI credits and account balance."""
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(f"{KLING_BASE_URL}/v1/account/costs", headers=kling_headers())
        return str(res.json())


@mcp.tool()
async def kling_get_packages() -> str:
    """Get available Kling AI subscription packages and resource info."""
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(f"{KLING_BASE_URL}/v1/account/packages", headers=kling_headers())
        return str(res.json())


# ════════════════════════════════════════════════════════════════════════════════
# POLLO TOOLS (3)
# ════════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def pollo_generate_video(
    prompt: str,
    provider: str = "pollo",
    model: str = "pollo-v2-0",
    aspect_ratio: str = "16:9",
    resolution: str = "720p",
    length: int = 5,
    mode: str = "basic",
    negative_prompt: str = "",
) -> str:
    """Generate a video using Pollo AI — 55+ models across all providers.

    provider/model combinations:
      pollo      → pollo-v2-0, pollo-v1-6, pollo-v1-5
      bytedance  → seedance-2-0, seedance-2-0-fast, seedance-1-5-pro, seedance-pro, seedance-pro-fast, seedance
      kling-ai   → kling-v3, kling-v2-6, kling-v2-5-turbo, kling-v2-1-master, kling-v2-1, kling-v2, kling-video-o1, kling-v1-6, kling-v1-5, kling-v1
      google     → veo3, veo3-fast, veo3-1, veo3-1-fast, veo2
      sora       → sora-2-pro, sora-2
      runway     → runway-gen-4-turbo, runway-gen-3-turbo
      minimax    → minimax-hailuo-2.3, minimax-hailuo-2.3-fast, minimax-hailuo-02, video-01
      pika       → pika-v2-2, pika-v2-1
      wanx       → wan-v2-6, wan-v2-5-preview, wan-v2-2-plus, wan-v2-2-flash, wanx-v2-1
      luma       → luma-ray-2-0, luma-ray-2-0-flash, luma-ray-1-6
      pixverse   → pixverse-v5-5, pixverse-v5, pixverse-v4-5, pixverse-v4, pixverse-v3-5
      vidu       → viduq3-pro, viduq2-pro, viduq2-turbo, vidu-q1, vidu-v2-0, vidu-v1-5
      hunyuan    → hunyuan

    resolution: 480p, 720p, 1080p | length: 5 or 10 | mode: basic or pro"""
    async with httpx.AsyncClient(timeout=30) as client:
        payload: dict = {"input": {"prompt": prompt, "aspectRatio": aspect_ratio, "resolution": resolution, "length": length, "mode": mode}}
        if negative_prompt:
            payload["input"]["negativePrompt"] = negative_prompt
        res = await client.post(f"{POLLO_BASE_URL}/generation/{provider}/{model}", headers=pollo_headers(), json=payload)
        data = res.json()
        task_id = data.get("taskId")
        if not task_id:
            return f"Error: {data}"
        result = await pollo_wait(client, task_id)
        video_url = result.get("output", {}).get("url") or result.get("url", "")
        return f"Video ready!\nURL: {video_url}\nTask ID: {task_id}" if video_url else f"Done\n{result}"


@mcp.tool()
async def pollo_animate_image(
    image_url: str,
    prompt: str = "",
    provider: str = "pollo",
    model: str = "pollo-v1-6",
    resolution: str = "720p",
    length: int = 5,
    mode: str = "basic",
) -> str:
    """Animate an image into a video using Pollo AI (image-to-video).
    Use same provider/model options as pollo_generate_video."""
    async with httpx.AsyncClient(timeout=30) as client:
        payload: dict = {"input": {"image": image_url, "resolution": resolution, "length": length, "mode": mode}}
        if prompt:
            payload["input"]["prompt"] = prompt
        res = await client.post(f"{POLLO_BASE_URL}/generation/{provider}/{model}", headers=pollo_headers(), json=payload)
        data = res.json()
        task_id = data.get("taskId")
        if not task_id:
            return f"Error: {data}"
        result = await pollo_wait(client, task_id)
        video_url = result.get("output", {}).get("url") or result.get("url", "")
        return f"Video ready!\nURL: {video_url}\nTask ID: {task_id}" if video_url else f"Done\n{result}"


@mcp.tool()
async def pollo_generate_image(
    prompt: str,
    model: str = "nano-banana-2",
    aspect_ratio: str = "1:1",
) -> str:
    """Generate an image using Pollo AI.
    model: nano-banana (Google), nano-banana-2 (Google), nano-banana-pro (Google), pollojourney-v7-image (Pollo)
    aspect_ratio: 1:1, 16:9, 9:16, 4:3"""
    async with httpx.AsyncClient(timeout=30) as client:
        provider = "pollojourney" if "pollojourney" in model else "google"
        endpoint = f"{POLLO_BASE_URL}/generation/{provider}/{model}/image"
        payload: dict = {"input": {"prompt": prompt, "aspectRatio": aspect_ratio}}
        res = await client.post(endpoint, headers=pollo_headers(), json=payload)
        data = res.json()
        task_id = data.get("taskId")
        if not task_id:
            return f"Error: {data}"
        result = await pollo_wait(client, task_id)
        image_url = result.get("output", {}).get("url") or result.get("url", "")
        return f"Image ready!\nURL: {image_url}\nTask ID: {task_id}" if image_url else f"Done\n{result}"


@mcp.tool()
async def pollo_check_task(task_id: str) -> str:
    """Check the status of a Pollo AI generation task."""
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(f"{POLLO_BASE_URL}/generation/{task_id}/status", headers=pollo_headers())
        return str(res.json())


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
