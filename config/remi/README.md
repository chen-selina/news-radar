# 台灣財經情緒字典（外部資料）

這個資料夾的兩個檔案不是本專案編的，是由下列公開資料轉檔而來：

- 來源：<https://github.com/RemiMFB/Taiwan-Financial-Sentiment-Dictionary>（2026.03.20 版）
- 論文：黃裕烈 (2025)，〈台灣財經情緒字典與議題分類字詞之編製與應用：大型語言模型之協作與分類評估〉，《經濟論文》。
  Huang, Yu-Lieh (2025), "The Taiwan Financial Sentiment Dictionary and Topic-Classification Lexicons:
  LLM-Assisted Collaboration and Classification Evaluation," *Academia Economic Papers*.
- DOI：<https://doi.org/10.5281/zenodo.17076771>
- 授權：資料為 CC BY 4.0（可自由使用與改編，須註明出處）。

## 檔案

| 檔案 | 內容 | 原始檔 |
|---|---|---|
| `dictionary.tsv` | 主字典：詞、方向（pos／neg）、種類（word＝一般詞，negation＝否定詞組）、在 2003–2025 財經新聞的出現次數與篇數 | `Source Up to 2025/RemiDictionary.xlsx` |
| `topics.tsv` | 五個議題（景氣、貨幣政策、利率、匯率、物價）的分類字詞；true＝通常有利經濟成長 | `Source Up To 2024/Topic*.xlsx` |

## 轉檔時做的更動

- 四張工作表（Positive、Negative、Negation of a Negative、Negation of a Positive）合併成一個檔。
- 「壓倒性」「扳倒」「獨霸」三個詞在原始檔同時列為正面與負面，無法判斷方向，沒有收錄。
- 其餘內容沒有更動。

字典改版後，用 `python tools/import_remi.py "解壓縮後的資料夾/data"` 重新轉檔。
