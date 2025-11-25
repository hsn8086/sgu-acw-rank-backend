import time
from fastapi import FastAPI, Request, Response
from httpx import AsyncClient
from asyncio import Semaphore, Lock
from loguru import logger

logger.add("debug.log", rotation="10 MB", level="DEBUG")

SECOND = 1
MINUTE = 60 * SECOND
HOUR = 60 * MINUTE
DAY = 24 * HOUR


class CFApi:
    def __init__(self):
        self.client = AsyncClient(base_url="https://codeforces.com/api/")
        self.semaphore = Semaphore(2)
        self.lock = Lock()
        self.next_allowed_time = 0

    async def get(self, path: str, query_params: dict = None):
        async with self.semaphore:
            with await self.lock:
                while self.next_allowed_time > time.monotonic():
                    await time.sleep(max(0, self.next_allowed_time - time.monotonic()))
                self.next_allowed_time = time.monotonic() + 0.5
            response = await self.client.get(path, params=query_params)
            return response


app = FastAPI()

cache = {}
cfapi = CFApi()


@app.get("{path:path}")
@logger.catch
async def proxy(path: str, req: Request):
    key = (path, tuple(sorted(req.query_params.items())))
    if key in cache:
        response = cache[key]
        if time.monotonic() - response["time"] < 3 * HOUR:
            return response["resp"]

    response = await cfapi.client.get(path, params=req.query_params)
    if response.status_code == 200:
        cache[key] = {
            "resp": Response(
                content=response.content,
                status_code=response.status_code,
                # headers=response.headers,
                media_type=response.headers.get("content-type", "text/plain"),
            ),
            "time": time.monotonic(),
        }
        return cache[key]["resp"]
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "text/plain"),
    )
    # return {"path":path,"query_params":req.query_params}
