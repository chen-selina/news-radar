"""檢查文章頁擷取結果：網站改版或新增來源時，用來確認標題、內文、轉載媒體有沒有抓對。

執行：python tools\\check_extract.py 網址1 網址2 …
只印出內文開頭與結尾各一小段，用來判斷有沒有夾雜導覽、廣告、延伸閱讀等雜訊。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar import http  # noqa: E402
from radar.crawl import urls  # noqa: E402
from radar.crawl.extract import extract  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    for url in argv:
        src = urls.source_of(url)
        print(f"■ {url}")
        try:
            p = extract(http.get(url).text, src)
        except (http.Blocked, RuntimeError) as e:
            print(f"  讀取失敗：{e}\n")
            continue
        paras = p.body.split("\n")
        print(f"  來源：{src or '其他網站'}　標題：{p.title}")
        print(f"  發布：{p.published_at}　轉載自：{p.via_outlet}　付費：{p.paywalled}　只有摘要：{p.partial}")
        print(f"  內文 {len(p.body)} 字、{len(paras)} 段")
        print(f"  第一段：{paras[0][:80]}")
        if len(paras) > 1:
            print(f"  最後兩段：{' ／ '.join(x[:60] for x in paras[-2:])}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
