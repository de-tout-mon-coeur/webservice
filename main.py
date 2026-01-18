# main.py — с логированием и фиксом gzip
import os
import httpx
from fastapi import FastAPI, Request, Response

app = FastAPI()

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com"
API_KEY = os.getenv("GEMINI_API_KEY")

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_gemini(request: Request, path: str):
    print(f"📥 Получен запрос: {request.method} /{path}")
    print(f"   Заголовки: {dict(request.headers)}")

    # Извлекаем query string, но НЕ ожидаем ключа от клиента
    query = request.url.query
    if query:
        print(f"   Query string: {query}")

    # ВСЕГДА используем внутренний ключ
    target_url = f"{GEMINI_BASE_URL}/{path}?key={API_KEY}"
    if query:
        # Если клиент прислал параметры (например, &alt=json), добавляем их
        target_url += "&" + query

    print(f"➡️  Проксируем в: {target_url.split('key=')[0]}key=***")

    headers = {k: v for k, v in request.headers.items() if k not in ("host", "content-length", "accept-encoding")}
    # ↑ Убираем accept-encoding, чтобы Google не сжимал ответ!
    headers["accept-encoding"] = "identity"  # ← ГОВОРИМ GOOGLE: НЕ СЖИМАЙ!

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            body = await request.body()
            gemini_resp = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
            )

        print(f"⬅️  Ответ от Google: {gemini_resp.status_code}")
        print(f"   Заголовки от Google: {dict(gemini_resp.headers)}")

        # Убираем проблемные заголовки
        resp_headers = dict(gemini_resp.headers)
        resp_headers.pop("content-encoding", None)
        resp_headers.pop("content-length", None)

        return Response(
            content=gemini_resp.content,
            status_code=gemini_resp.status_code,
            headers=resp_headers,
        )

    except Exception as e:
        print(f"💥 Ошибка в прокси: {e}")
        return Response(content=f"Proxy error: {str(e)}", status_code=500)
