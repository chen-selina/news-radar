"""啟動網站後端：python -m radar.api [--port 8000] [--open]

只在本機（127.0.0.1）開放；要讓同網路的隊友連進來，加 --host 0.0.0.0。
--open：網站準備好之後自動打開瀏覽器（start_site.bat 使用）。
"""
import argparse
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn


def open_when_ready(port: int, timeout: float = 60) -> None:
    """等網站真的回應了才打開瀏覽器；一直沒回應（例如啟動失敗）就不開，避免打開一個連不上的頁面。"""
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url + "/api/meta", timeout=2) as r:
                if r.status == 200:
                    webbrowser.open(url)
                    return
        except OSError:
            pass
        time.sleep(0.5)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="啟動台股新聞雷達網站後端")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true", help="改程式後自動重新啟動（開發用）")
    ap.add_argument("--open", action="store_true", help="網站準備好之後自動打開瀏覽器")
    args = ap.parse_args(argv)
    print(f"網站：http://127.0.0.1:{args.port} （關掉這個視窗或按 Ctrl+C 停止）")
    if args.open:
        threading.Thread(target=open_when_ready, args=(args.port,), daemon=True).start()
    uvicorn.run("radar.api.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
