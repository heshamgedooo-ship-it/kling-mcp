#!/usr/bin/env python3
import asyncio
import os
import time
import httpx
import jwt
from mcp.server.fastmcp import FastMCP

KLING_ACCESS_KEY = os.environ.get("KLING_ACCESS_KEY", "")
KLING_SECRET_KEY = os.environ.get("KLING_SECRET_KEY", "")
KLING_BASE_URL = "https://api.klingai.com"

mcp = FastMCP("kling")

def generate_token() -> str:
    now = int(time.time())
    return jwt.encode(
        {"iss": KLING_ACCESS_KEY, "exp": now + 1800, "nbf": now - 5},
        KLING_SECRET_KEY, algorithm="HS256"
    )

def get_headers() -> dict:
    return {"Authorization": f"Bearer {generate_token()}", "Content-Type": "application/json"}

async def wait_for_task(client, endpoint, task_id, max_wait=300):
    url = f"{KLING_BASE_URL}{endpoint}/{task_id}"
    for _ in range(max_wait // 5):
        await asyncio.sleep(5)
        res = await client.get(url, headers=get_headers())
        data = res.json()
        status = data.get("data", {}).get("task_status", "")
        if status == "succeed":
            return data
        if status == "failed":
            raise Exception(f"Task failed: {data.get('data', {}).get('task_status_msg', 'unknown')}")
    raise Exception("Timeout after 5 minutes")

def extract_image_url(data):
    works = data.get("data", {}).get("task_result", {}).get("images", [])
    return works[0].get("url", "") if works else ""

def extract_video_url(data):
    works = data.get("data", {}).get("task_result", {}).get("videos", [])
    return works[0].get("url", "") if works else ""


@mcp.tool()
async def generate_image(prompt: str, negative_prompt: str = "", aspect_ratio: str = "1:1", model: str = "kling-v1-5", n: int = 1) -> str:
    """Generate an image from a text prompt using Kling AI."""
    async with httpx.AsyncClient(timeout=30) as client:
        payload = {"model_name": model, "prompt": prompt, "aspect_ratio": aspect_ratio, "n": n}
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        res = await client.post(f"{KLING_BASE_URL}/v1/images/generations", headers=get_headers(), json=payload)
        data = res.json()
        task_id = data.get("data", {}).get("task_id")
        if not task_id:
            return f"API Error: {data}"
        result = await wait_for_task(client, "/v1/images/generations", task_id)
        url = extract_image_url(result)
        return f"Image generated!\nURL: {url}\nTask ID: {task_id}" if url else f"Done (Task: {task_id})\n{result}"


@mcp.tool()
async def generate_video(prompt: str, negative_prompt: str = "", duration: str = "5", aspect_ratio: str = "16:9", model: str = "kling-v1-6", mode: str = "std") -> str:
    """Generate a video from a text prompt using Kling AI."""
    async with httpx.AsyncClient(timeout=30) as client:
        payload = {"model_name": model, "prompt": prompt, "duration": duration, "aspect_ratio": aspect_ratio, "mode": mode}
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        res = await client.post(f"{KLING_BASE_URL}/v1/videos/text2video", headers=get_headers(), json=payload)
        data = res.json()
        task_id = data.get("data", {}).get("task_id")
        if not task_id:
            return f"API Error: {data}"
        result = await wait_for_task(client, "/v1/videos/text2video", task_id, max_wait=300)
        url = extract_video_url(result)
        return f"Video generated!\nURL: {url}\nTask ID: {task_id}" if url else f"Done (Task: {task_id})\n{result}"


@mcp.tool()
async def animate_image(image_url: str, prompt: str, negative_prompt: str = "", duration: str = "5", model: str = "kling-v1-6", mode: str = "std") -> str:
    """Animate a static image into a video using Kling AI (image-to-video)."""
    async with httpx.AsyncClient(timeout=30) as client:
        payload = {"model_name": model, "image": image_url, "prompt": prompt, "duration": duration, "mode": mode}
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        res = await client.post(f"{KLING_BASE_URL}/v1/videos/image2video", headers=get_headers(), json=payload)
        data = res.json()
        task_id = data.get("data", {}).get("task_id")
        if not task_id:
            return f"API Error: {data}"
        result = await wait_for_task(client, "/v1/videos/image2video", task_id, max_wait=300)
        url = extract_video_url(result)
        return f"Video generated!\nURL: {url}\nTask ID: {task_id}" if url else f"Done (Task: {task_id})\n{result}"


@mcp.tool()
async def check_credits() -> str:
    """Check remaining Kling AI credits."""
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(f"{KLING_BASE_URL}/v1/account/costs", headers=get_headers())
        return str(res.json())


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
