# CODESKILL rebuild：專案規則

獨立重現 CODESKILL（arXiv:2605.25430）的工程與研究 repo。全域規則照常適用；本檔只放
本專案特有的內容，細節以 docs/ 為準。

## 權威位置

- T2 `~/ray/codeskill-rebuild-20260905` 是權威 Git repo；其他 checkout、`~/ray/tmp/`
  副本與 GitHub 分支都不是。
- 原始 trace、request／response、ledger、模型快取留在 T2，不進 git；以
  `artifacts/provenance-index.json` 的路徑與 SHA-256 引用。
- GitHub `rayyichen310/codeskill-rebuild` 是公開 repo，只接受
  `scripts/create_public_snapshot.py` 產生的脫敏快照；不推私有歷史或開發分支。

## 開始前先讀

1. docs/STATUS.md（現況與已知問題）→ docs/DECISIONS.md（有效決策與待決事項）→
   docs/PAPER_ALIGNMENT.md（論文對照）。
2. 需要時再讀：docs/ARCHITECTURE.md（模組、入口、模型服務、軌跡來源）、
   docs/EXPERIMENTS.md（所有 run 的位置與結果）、docs/VERSION_CONTROL.md。
3. docs/archive/ 是 Codex 時期的原始文件（舊驗收契約、R001–R014 原文、派工紀錄、審查），
   只供查證；仍有效的內容已併入 DECISIONS.md。

## 指令

- Python ≥ 3.12。離線測試：
  `PYTHONPATH=src PYTHONUTF8=1 python3 -m unittest discover -s tests -v`
- 模型 endpoint 放在本機忽略檔 `configs/model-endpoints.json`（範例見
  `configs/model-endpoints.example.json`），不提交。
- `.gitattributes` 設定 `* -text`：prompt、config 與 manifest 逐位元組保存，不要改換行。

## 方法與關卡

- 需使用者確認的方法決策：prompt 語義、模型、embedding、context 截斷／摘要／compaction、
  retrieval 門檻、題組與分割、bank lifecycle 與更新語義、統計口徑。
- 任何正式比較（原規劃 12 題 × 3 組 × 2 次 = 72 trials，規模待重議）前必須停下，向使用者
  完整說明設定並取得當次確認；主代理驗收或 pilot 完成都不能免除。所有要跑 solver 的實驗
  （含診斷）也先確認。
- 診斷 run 只跑指定的題目與輪次一次，不自行延伸；生成、bank、檢索與注入、solver 使用、
  結果、成本分開報告。
- 不修改 OpenClaw 原始碼或 dist，不用 fork 或 monkeypatch（R013）。

## 論文對齊

- 以論文原文（圖、附錄 prompt）為基準，逐項列出偏差；實作加的 provenance 欄位
  （如 `rule_evidence`）是 sidecar，與論文格式分開保存與判斷。
- 分開判斷：論文格式、本地驗證器、候選有用、任務成功、下游 solver 效果。離線測試通過
  不代表其中任何一項成立。

## Run 與共享服務

- 每個 run 記錄 git commit、是否 dirty（dirty 時保留 patch）、config 與來源 hash；
  保留失敗的 run 與 manifest。
- <MODEL_SERVICE_HOST> 上的模型服務是共享的：只讀 metadata，不部署、重啟或改設定；伺服器資訊只輸出
  需要的欄位。
