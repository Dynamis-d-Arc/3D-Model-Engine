"""Run the backend:  python -m twin.api  (host/port from TWIN_HOST / TWIN_PORT)."""

import logging

import uvicorn

from twin.config import get_settings

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings()
    uvicorn.run("twin.api.app:create_app", factory=True, host=s.host, port=s.port, log_level="info")
