"""啟動網站後端：python -m radar.api [--port 8000]

只在本機（127.0.0.1）開放；要讓同網路的隊友連進來，加 --host 0.0.0.0。
"""
import argparse
import sys

import uvicorn


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="啟動台股新聞雷達網站後端")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true", help="改程式後自動重新啟動（開發用）")
    args = ap.parse_args(argv)
    print(f"網站後端：http://{args.host}:{args.port}/docs （按 Ctrl+C 停止）")
    uvicorn.run("radar.api.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
