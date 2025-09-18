import argparse
import os
from typing import Optional

import uvicorn


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Run Testing Farm SSE Bridge (FastAPI/uvicorn)")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"), help="Bind host")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "10000")), help="Bind port")
    parser.add_argument(
        "--log-level", default=os.environ.get("LOG_LEVEL", "info"), choices=["critical", "error", "warning", "info", "debug", "trace"],
    )
    args = parser.parse_args(argv)

    uvicorn.run(
        "testing_farm_sse_bridge.app:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        reload=False,
        workers=1,
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
