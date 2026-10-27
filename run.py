import os
import uvicorn

if __name__ == "__main__":
    on_render = "PORT" in os.environ
    uvicorn.run(
        "main:app",
        host="0.0.0.0" if on_render else "127.0.0.1",
        port=int(os.environ.get("PORT", 8800)),
        reload=not on_render,
    )