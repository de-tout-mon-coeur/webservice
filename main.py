import os
import asyncio
import logging
import httpx
from fastapi import FastAPI, Request, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com"
API_KEY = os.getenv("GEMINI_API_KEY")

# Таймаут выставлен с запасом относительно клиентского (120 с).
# Gemini может думать над большим изображением 60–90 с.
PROXY_TIMEOUT = httpx.Timeout(connect=15.0, read=150.0, write=60.0, pool=15.0)

# Коды ответа от Gemini, при которых стоит повторить попытку
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 3


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_gemini(request: Request, path: str):
    logger.info("📥 %s /%s  content-length=%s",
                request.method, path, request.headers.get("content-length", "?"))

    query = request.url.query
    target_url = f"{GEMINI_BASE_URL}/{path}?key={API_KEY}"
    if query:
        target_url += "&" + query

    logger.info("➡️  target: .../%s?key=***", path)

    # Убираем заголовки, которые не нужно пробрасывать
    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "accept-encoding",
                             "transfer-encoding", "connection")
    }
    # Просим Google не сжимать ответ, чтобы не разжимать его в прокси
    forward_headers["accept-encoding"] = "identity"

    body = await request.body()

    last_status = 500
    last_content = b"Proxy error: unknown"
    last_headers: dict = {}

    async with httpx.AsyncClient(timeout=PROXY_TIMEOUT) as client:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                gemini_resp = await client.request(
                    method=request.method,
                    url=target_url,
                    headers=forward_headers,
                    content=body,
                )
                logger.info("⬅️  attempt=%d status=%d", attempt, gemini_resp.status_code)

                if gemini_resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                    wait = 2 ** attempt  # 2, 4 сек
                    logger.warning("🔄 retryable status %d, waiting %ds before retry %d/%d",
                                   gemini_resp.status_code, wait, attempt + 1, MAX_RETRIES)
                    await asyncio.sleep(wait)
                    last_status = gemini_resp.status_code
                    last_content = gemini_resp.content
                    last_headers = dict(gemini_resp.headers)
                    continue

                # Успех или неповторяемая ошибка — отдаём как есть
                resp_headers = dict(gemini_resp.headers)
                resp_headers.pop("content-encoding", None)
                resp_headers.pop("content-length", None)
                resp_headers.pop("transfer-encoding", None)

                return Response(
                    content=gemini_resp.content,
                    status_code=gemini_resp.status_code,
                    headers=resp_headers,
                )

            except httpx.ReadTimeout:
                logger.error("💥 attempt=%d ReadTimeout (Gemini не ответил за %.0fs)",
                             attempt, PROXY_TIMEOUT.read)
                last_content = b"Proxy error: Gemini read timeout"
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(2 ** attempt)
                    continue

            except httpx.ConnectTimeout:
                logger.error("💥 attempt=%d ConnectTimeout", attempt)
                last_content = b"Proxy error: connect timeout"
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(2 ** attempt)
                    continue

            except httpx.RequestError as e:
                logger.error("💥 attempt=%d RequestError: %r", attempt, e)
                last_content = f"Proxy error: {type(e).__name__}: {e}".encode()
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(2 ** attempt)
                    continue

            except Exception as e:
                logger.error("💥 attempt=%d unexpected: %r", attempt, e)
                last_content = f"Proxy error: {type(e).__name__}: {e}".encode()
                break

    # Все попытки исчерпаны
    resp_headers = {k: v for k, v in last_headers.items()
                    if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")}
    return Response(content=last_content, status_code=last_status or 500, headers=resp_headers)
