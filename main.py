import time
from fastapi import FastAPI, Request
from httpx import AsyncClient
from asyncio import Semaphore

SECOND = 1
MINUTE = 60 * SECOND
HOUR = 60 * MINUTE
DAY = 24 * HOUR


class CFApi:
    def __init__(self):
        self.client = AsyncClient(base_url="https://codeforces.com/api/")
        self.semaphore = Semaphore(2)

    async def get(self, path: str, query_params: dict = None):
        async with self.semaphore:
            response = await self.client.get(path, params=query_params)
            return response


app = FastAPI()

cache = {}


@app.get("{path:path}")
async def proxy(path: str, req: Request):
    key = (path, tuple(sorted(req.query_params.items())))
    if key in cache:
        response = cache[key]
        if time.monotonic() - response["time"] < 3 * HOUR:
            return response["resp"]

    response = await CFApi().client.get(path, params=req.query_params)
    if response.status_code == 200:
        cache[key] = {"resp": response.json(), "time": time.monotonic()}
    return response.json()
    # return {"path":path,"query_params":req.query_params}
