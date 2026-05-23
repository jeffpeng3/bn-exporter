import os
from dotenv import load_dotenv
from aiohttp import web
from prometheus_client.aiohttp import make_aiohttp_handler

load_dotenv()

app = web.Application()
app.router.add_get("/metrics", make_aiohttp_handler())

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))