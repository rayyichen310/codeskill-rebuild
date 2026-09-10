# 昨日 TB2 軌跡重用盤點與後續實驗界線

日期：2026-09-05。本次只有讀取、解析及 checksum 盤點；沒有推論、抽取或重跑 benchmark。

## 結論

**可以重用昨天的 baseline 軌跡啟動 CODESKILL 重建，無需重新跑同一批 source tasks。**

兩組各有 12 個 trial；各 11 條 raw session 通過本次結構檢查，形成 **11 對、22 條**完整結束並有官方 verifier 結果的紀錄。`polyglot-c-py` 兩組均是 `AgentSetupTimeoutError`，沒有 agent session，不能作抽取來源。失敗但正常結束的任務仍可提供失敗經驗，不因 reward=0 排除。

此处「完整」限於已有 session 的訊息鏈、動作／觀察配對和結果齊備，不保證每次工具原始輸出從未截斷，也不保證摘要沒有漏重要資訊。仍須在 importer 中處理原始 truncation 訊號。

## 來源

共同父目錄：

`<OPENCLAW_ROOT>/docs/plan/summary-spine/terminal-bench-results/raw/`

- Baseline：`dsv4-v1d-core12-paired-r1-20260904-baseline/`
- Spine B：`dsv4-v1d-core12-paired-r1-20260904-spineB/`
- 原始資料保持原位、不修改。新專案保存檔案清單、hash 和來源身分；真正 import 時檢查 hash，按白名單匯入必要內容，不能整份搬入含憑證的設定或 environment dump。

## 逐題可用性

| 題目 | Baseline reward | Spine reward | 抽取來源狀態 |
|---|---:|---:|---|
| build-pmars | 1 | 1 | 兩組文字軌跡可用 |
| cancel-async-tasks | 1 | 1 | 兩組文字軌跡可用 |
| cobol-modernization | 1 | 1 | 兩組文字軌跡可用 |
| code-from-image | 0 | 1 | 軌跡存在且圖片 bytes 有保存；需多模態處理，不可無聲刪圖片 |
| fix-git | 1 | 1 | 兩組文字軌跡可用 |
| fix-ocaml-gc | 1 | 0 | 兩組文字軌跡可用 |
| git-leak-recovery | 1 | 1 | 兩組文字軌跡可用 |
| headless-terminal | 1 | 1 | 兩組文字軌跡可用 |
| kv-store-grpc | 0 | 0 | 兩組文字軌跡可用 |
| polyglot-c-py | 無 | 無 | 兩組 setup timeout，無可抽取軌跡 |
| pypi-server | 1 | 0 | 兩組文字軌跡可用 |
| schemelike-metacircular-eval | 0 | 0 | 兩組文字軌跡可用 |

22 條有 session 的紀錄均符合：JSONL 可解析、entry IDs 不重複、parent references 齊全、tool call/result IDs 雙向對齊、最後 assistant stop、reward.txt 與 result.json 的 reward 相符、無 trial exception。ATIF 工具呼叫 ID 亦與 session 相符。11 對的 task checksum 和 instruction hash 一致；模型名稱、OpenClaw version 記錄一致；兩組差別為 Spine 設定及實際執行軌跡。這不代表已核對當時部署權重 hash 或重跑控制組的嚴格等價性。

## 重要發現：以 raw session 為輸入

每個可用 trial 都有：

- `agent/instruction.txt`
- `agent/openclaw.session.jsonl`
- `agent/trajectory.json`（ATIF-v1.7）
- `agent/openclaw.txt`
- `result.json`、`verifier/reward.txt`、`verifier/ctrf.json`

**不能只讀 trajectory.json。** 原始 session 有 `thinking`、text、toolCall、toolResult；ATIF 在不少 agent steps 寫成 `(no assistant text)`。例如 Spine build-pmars 第一個工具步驟的 session 同時有 thinking/text/toolCall，但 ATIF 的 message 是 placeholder。Importer 必須從原始 session 重建可見 reasoning、動作、參數、觀察及結果；不得用 placeholder 當「原文沒有推理」。

這批軌跡用過 `exec/read/write/edit/process/update_plan` 等 OpenClaw 原生工具，不可只保留 bash exec，也不能將 write/edit 內容與結果漏掉。這是相較原論文 bash-based agents 的明確 harness 變體。

`code-from-image` 的 baseline 有兩份 image blocks，Spine 有一份，均為實際保存的 PNG base64；不是只有圖片路徑。第一版可先用其餘 10 條 baseline 文字軌跡，將此題列為 `multimodal_pending`。多模態例外不改寫成「11 條文字軌跡已可直接送入 manager」。

## 第一版如何重用

1. 先從零實作 CODESKILL importer、配對、抽取、bank、evolve / maintain 及 agent hooks。
2. 第一批來源使用上述 baseline 的 raw session；Spine raw session 與摘要材料保留作後續實驗，暫不混進主要 bank。
3. 描述、task grouping、skill candidates、bank 和操作歷史均由新實作重新生成；節省的是 source agent rollout，不是直接沿用舊 skill。
4. Task grouping 仍需 2–3 條相關且不同 instance 的軌跡；同題 baseline / Spine 不能冒充兩個不同 task，也不能為湊配對而強行組合無關任務。
5. 來源題目和開發用題不可再成為正式 held-out；同題兩組共享同一 instance identity，來源過濾包含 merge / evolve 全部祖先。
6. Offline extraction / bank 操作可完全用現有資料啟動。真正檢驗新 skill 是否影響 agent 的下一步、是否提高新題成功率，仍需之後的新 skill-conditioned rollout；舊 trace 不提供新策略的反事實結果。
7. 昨天 trace 是歷史 source evidence，不能直接算本版 M3 / M4 已通過，也不自動當新 held-out 的 no-skill 控制結果。

## 後續：Spine 是否讓抽取更省、更好

使用者想比較的問題有兩層，需分開：

### E1：兩種 agent 執行方式產生的經驗，哪個更適合抽 skill？

同題 baseline raw session vs Spine raw session。固定 manager、prompt、group 成員、抽取次數與後續測試條件，分開產生 bank。

這是 **經驗來源比較**，會同時受到不同動作、推理、步數與任務結果影響。已有資料顯示 code-from-image 是 0→1、fix-ocaml-gc 與 pypi-server 是 1→0；不能把抽取差異全部歸因於壓縮。

### E2：同一份經驗，用完整資料還是較短 Spine 表示來抽？

在同一個 Spine run 內，比較其 raw session 與對應的 Spine 摘要表示。已保存 `summary-spine-context.md`、call-span summaries、state、raw-store pointers 及每次更新目錄，可用來構建這種對照。

但最後一份 Spine board 不等於整條軌跡，也不一定等於某次完整 model request：必須核對摘要覆蓋 call 範圍、補上尚未摘要的 raw tail、保留 user context 與最後 outcome；要用原始 pointer 取回的內容及額外成本也必須記錄。重建出的 manager 輸入標示 reconstructed extraction representation，不能說是歷史模型真正收到的原樣 prompt。

E2 比 E1 更接近「壓縮是否保留抽取所需資訊」；原始軌跡與可見壓縮表示必須涵蓋相同截止點，不能比較不同經驗長度。論文 event 的 full-trajectory 路徑與 Spine representation 分開標示為原文／變體。

後續判斷標準：相同 manager tokenizer 實測抽取 input tokens、輸出與耗時；候選的證據完整度與可用性；再看新題實際成功率及成本。不能只用 judge 分數接近或未達統計顯著，就說「兩者等效」。若主張效果差不多，需要事先定義可接受差距並呈現不確定性。

**昨天 solver 的 final prompt 變短，不等於今日 manager 的完整抽取輸入也縮短同樣比例。** 本輪沒有 tokenization 或抽取測量，不引用先前 solver context 百分比當 manager 節省量。

## 可重現證據

- 新寫盤點工具：`<PROJECT_ROOT>/audit_prior_traces.py`
- 逐檔 hash、tool 配對、session/ATIF 差异與 pair 核對：`<PROJECT_ROOT>/evidence/prior-traces/audit-20260905.json`
- 以上只證明本輪盤點，不等於 CODESKILL 已實作或 live 驗收通過。
