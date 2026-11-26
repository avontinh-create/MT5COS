import os

import uvicorn

from app.api.application import app


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=os.getenv("MT5_API_HOST", "127.0.0.1"),
        port=int(os.getenv("MT5_API_PORT", "9000")),
        reload=False,
    )
