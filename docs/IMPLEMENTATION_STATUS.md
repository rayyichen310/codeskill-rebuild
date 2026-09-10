# R012 實作狀態

更新：2026-09-11。此文件只記錄 `codeskill-rebuild-20260905` 的 R012 實作與離線驗證；不把 fixture、計畫檔或 HTTP proxy 單測說成真 OpenClaw、solver 或 official verifier 證據。

## 已完成的 R012 實作

- **壓縮與 event 生命週期：**overlay 的 durable state 已升至 R012。原生 compaction 有有效 request-transition 證據時，task 仍可 relocation；event 明確區塊會 retire，不會搬到新上下文前端。之後有新的相符 anchor 時，可重新注入同一 skill ID／version；仍留在上下文的同一版本不重複注入。未知 anchor 或不屬於本 request 的 compaction 仍 fail closed。
- **多條 event 選取：**`EventSelectionSettings` 只在明示的 development profile reference 下允許多條 matching event；selection 維持既有排序。這次沒有自行新增 relevance threshold、token budget 或額外 LLM judge。
- **每軌跡最多三次初始 event 抽取：**`event_extraction.py` 對初始探索固定上限三次，遇 `skip` 或 canonical validated skill content 的完全重複即停止。repair／transport retry 另存記錄，不佔 initial slot。第二、三次帶入先前候選的 title、condition、rules 及 trigger／response／outcome step references。
- **只演化實際提供的 skill：**`evolution.py` 僅從 durable proxy 的 actual upstream request 取候選；先前只被 retrieval 選到、沒有出現在送出訊息的 skill 會被拒絕。事件即使後來被 compaction retire，只要存在已送出的 injection block/hash 證據仍可列入。task 的 combined block 與每個 skill 的 rendered hash 都會驗證；transport failure 後重送亦保留同一選取身分。
- **同題 bank freeze：**`trial_schedule.py` 在題目開始前為每 arm／repeat 建立獨立 immutable snapshot。所有 trial 都完成後才以明示順序 staging update；callback 出錯時 live bank 不變、保留已完成 release evidence、禁止自動 replay 或進入下一題。
- **可持久化的 freeze／finish／release：**`run_m3_r012_lifecycle.py` 將同一題所有 arm/repeat 的 coordinator、profile hash 與 contract snapshot 存成單一狀態。`finish` 在任何 copy 前先檢查 pending assignment、trial ID、每份 proxy attempt 的 trial ID、可選 trajectory 的 canonical source ID；完成後拒絕重寫。`release` 要求完整、明示的 release order；Full Lifecycle arm 一律是 `evaluate_all_supplied`，不能以人工 `skip` 躲過已提供的 skill。只有已複製的 durable proxy evidence 確認沒有 supplied skill 時，才會產生無 manager call 的 factual no-evolution record。只有 `--execute-manager` 才會接觸 manager；pre-call journal、candidate schema 驗證、所有實際 supplied skill 的 Fig.8 prompt、Fig.9 maintenance 及 staged bank 更新都在同一 fail-closed 路徑。
- **SQLite native compaction adapter：**`SqliteTranscriptCompactionDetector` 解析 OpenClaw 的 canonical `sqlite:<agent>:<session>:<store>` marker，依 OpenClaw 的 agent-ID normalization 和 store-path 規則唯讀查詢該 agent/session 的 `transcript_events`。它支援 canonical `agents/<agent>/sessions/sessions.json`、noncanonical `sessions.json`、custom store 與 direct SQLite path；marker 與 canonical direct/path owner 不一致時 fail closed。新 compaction 只保留 session ID、sequence、event ID／parent、`firstKeptEntryId` 與**原始** `event_json` hash；summary 不會複製到 CODESKILL sidecar。SQLite schema／JSON、既見 compaction 的 rewrite／消失或 session identity 改變也都 fail closed。
- **公開 plugin 的 native-summary permit 與正常呼叫邊界：**`openclaw_plugin/` 是獨立的 OpenClaw public-plugin package，不修改 OpenClaw source。它以公開 provider `wrapStreamFn` 在每個帶 public session ID 的一般 solver call 同步寫入 normal-call boundary，並以公開 `before_compaction` hook 寫入短效、單一 session permit。支援 release 若讓 native summary stream 缺少 public session ID，plugin 僅在同一 process、剛被該 hook arm 的 permit 有效時接受最多四次；一般 session call 會立即解除 arm。`NativeSummaryPermitGate` 只允許設定中同一 trial/session 的隔離 sidecar，在「本 process 剛啟用 permit、沒有 normal boundary」時原樣轉送 bounded native summary retry；沒有 prompt、role、token 或 payload-shape heuristic。任何 normal boundary 都會先撤銷未完成 permit，取消／no-op／失敗後的一般 request 因而回到 overlay，不能 bypass。只有唯讀 SQLite detector 確認新的 native compaction row，permit 才會退休，後續 solver request 才回到原有 overlay，留下 task relocation、event retire 和下一個匹配批次重新注入的 evidence chain。foreign／stale／multiple permit／boundary、proxy restart 後的未解析 permit，與四次 retry 未轉換都 fail closed。
- **演化 provenance：**被選中的 supplied base skill、當前 instance 與 maintenance merge target 的 canonical/raw source IDs 會聯集保留；演化後 skill 對這些所有來源都不可再被 retrieval 選取。
- **配對診斷：**`pairing_audit.py` 對 `no_related_group` 保存短描述、被引用與未引用的 raw step，以及 pairing reason，供人工檢視「描述遺漏」或「來源不支持」。它不自動改寫描述、調整配對規則或強制形成 group。

## 可執行入口

- `scripts/run_m2_r012_event_extraction.py` 預設只寫出每來源的 bounded schedule；只有同時明示 `--execute-manager --activate-unlimited-development-ledger` 才會建立 manager request，並把既有 ledger 原封不動地升為可稽核的 `unlimited`。每個 initial event request 先以同一服務的 exact tokenizer 計完整 raw trace（含先前候選）；`<=` allowance 時送 full，僅 `>` 時才使用 D03/R006 的 action-observation segment summaries。每段覆蓋所有 raw step、最多帶回三個完整 tool pair，最後一段保留原始 final toolResult；final candidate 的所有引用仍必須來自帶回的原始 fragment。每次保存 full 與實際 forwarded messages hash、prompt refs、tokens、step IDs；若 final 仍超額，會保存已完成 summary call IDs/count 與 phase，分類為 post-summary context block，不截斷或當作 skip。它不會默默重用歷史結果。
- `scripts/audit_m3_r012_supplied_evolution.py` 只由 proxy attempt records 寫出 supplied-only evolution candidates，不呼叫模型。
- `scripts/plan_m3_r012_instance_freeze.py` 只寫入各 arm／repeat 的 pre-instance snapshot manifest，不啟動 trial。
- `scripts/run_m3_r012_lifecycle.py` 以 `freeze`、`finish`、`release` 三個命令管理 durably frozen lifecycle；非 Full Lifecycle arm 可明示 skip，Full Lifecycle arm 必須 `evaluate_all_supplied`，沒有 `--execute-manager` 不會呼叫 manager。
- `scripts/run_openclaw_r012_sidecar.py` 的一般模式現在是 `selection.mode=frozen-bank`：它只接受同一 `trialId` 的 pending frozen assignment，先核對 lifecycle 檔、R012 profile 與 frozen bank snapshot 的 SHA-256。profile 必須為每個 arm 明示 `enable_task`／`enable_event`；注入資格不再由 Full Lifecycle arm 推論，因此可設定無 skill arm、只注入 task 的非演化 arm，及 task/event 都注入的 arm。啟用的 selector 才會以既有 MiniLM encoder 和 `SkillBank.eligible` 檢索；同題或 canonical source ID 相同的 skills 仍由 bank exclusion 排除；query、ranked results、profile 和 snapshot identity 都會寫入既有 durable proxy evidence。每個 forwarded request 都以完整 payload 含所有 active event blocks 與不含 event blocks 的 token delta，依 frozen `complete_payload_active_event_blocks_delta` scope 對 `skillTokenBudget` 檢查；超出即 409 fail closed，不截斷或挑選子集。它不會建立或更新 bank，也不會自動執行 finish/release/evolution。
- sidecar 的 `--check-config` 同時驗證唯一公開 `codeskill-r012` provider、該 provider 的 model 與 `agents.defaults.model.primary` 都精確指向本 sidecar listener，以及 plugin manifest identity、allow/load/enable 和 plugin trial/session/permit binding。若 provider/plugin binding 不完整，正常 solver call 無法可靠通過公開 wrapper 的 normal-call boundary，因此會在 listener 綁定前停止。舊的手動 `taskSkill`/`eventSkill` 只可放入有明示 acknowledgement 的 `fixture-test-only` selection，不能被當成 retrieval 或 lifecycle evidence。

## 本機離線驗證

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
python -m unittest discover -s tests -v
```

結果：**142 / 142 passed**；最終 R014 回驗輸出保留在 `validation/r014-final-corrections-full-test-20260911.log`。新增／受影響測試覆蓋實際入口同一 server exact `<`／`=` full path 與 `>` compacted path、多段 action-observation coverage、final compacted request hash、summary 後仍超額時沒有 final completion 且保存 call IDs/phase、summary 完成後 tokenizer 失敗仍保存 call IDs/phase、finite legacy profile 不得降回 activated unlimited ledger、native `type="compaction"` raw control record/hash 與 legacy custom control、historical compaction summary 不可當原始 evidence、缺 raw step 的 generated claim 必須被拒絕；其餘既有 frozen bank、OpenClaw plugin、lifecycle 與 provenance coverage 同樣重跑通過。fake local manager 只驗證 transport/entrypoint，不是新真實 manager、solver 或 official verifier evidence。

首次以未設定 `PYTHONPATH` 的 plain unittest invocation 失敗，原因是未安裝 package 的 test import path；原始輸出保留在 `evidence/implementation/r012-local-test-first-failure-20260909.txt`，不是 R012 assertion failure。修正 invocation 後已重跑完整 suite。

## T2 受控 native CLI probe

2026-09-09 的初始受控 probe 位於 `evidence/implementation/r012-native-probe-controlled-20260909-01/`：真 OpenClaw CLI 加本機 fake transport，exit code 0、29.97 秒、proxy／fake transport 各 3 個 request、`real_model_calls` 為 0。2026-09-10 再在隔離 worktree 的 `evidence/implementation/r012-native-sqlite-controlled-20260910/run-20260910-01` 至 `run-20260910-06` 重跑；各 run 都是 Docker 內的真 CLI、同一個 fake transport、隔離 SQLite state 與固定 request 上限，沒有啟動既有 Hermes container、GPU 或真實模型。計數是字元式 synthetic accounting，不是 tokenizer 或 task-solving 效能數據。

新版 CLI 回報的 `sessionFile` 為 `sqlite:main:<session>:.../agents/main/sessions/sessions.json`，但該 JSON path 不會實體存在；OpenClaw 會依它解析到 `agents/main/agent/openclaw-agent.sqlite`。`run-20260910-02` 至 `-06` 用同一 canonical marker 建立 detector baseline，並唯讀查詢同一真 CLI SQLite `transcript_events`，證明 SQLite adapter 的 marker→database→session read 路徑可用。這些 runs 都在 fake provider 的 bounded 400 overflow 後得到 **零個** native `compaction` row；預設 mode 會記錄「auto-compaction start」後 incomplete，而 safeguard run 在 8 個 proxy HTTP request／4 個 fake upstream request 上限內也未寫出 compaction。所有這些負結果、CLI stdout/stderr、raw request、SQLite state 與 detector observation 都已保留。它們**沒有**驗證 native summary routing、overlay event retire/reinject，或任何 solver/verifier 成效。

`plugin-run-20260910-05` 的舊 happy-path 以同一真 CLI、公開 compaction hook 的 plugin 和隔離 per-session sidecar 寫出一個 native SQLite transition；三個 CLI invocation 都 exit 0，6 個 proxy／6 個 fake transport request、0 real model call、52.65 秒。它保留作為原始證據，但當時尚未證明取消／no-op／失敗後一般 request 不會借用 active permit，也未實測 event re-injection，因此**不能單獨作為 R013 接受依據**。本版 normal-call boundary 與後續 8-request controlled probe 用來補這兩個條件。官方 release compatibility runner 會把全新的 npm tarball 安裝到 per-run isolated npm prefix；v2026.9.3 要求 Node ≥24.16，因此 lifecycle 與 probe 都在隔離 Docker `node:24-slim` 中完成，之後才以 Docker `--read-only` mount 該 prefix 的 dependency 執行；共享 OpenClaw/source、服務、GPU 和真實模型都不會被修改或啟動。`official-openclaw-2026.9.3-run-20260910-08` 已保存官方 tarball SHA-256 `d1c63366833f8ae4a6ab4f3b60b1aa84ca82d03dba13d3d3eba989aa159e2449`、isolated install、4/4 CLI exit 0、8/8 fake request、0 real model call、native SQLite compaction、raw summary forwarding、task relocation、event retirement 和之後的 event reinjection。它是相容性接線證據，不是 solver/verifier 或正式 trial。

## 尚未驗證與停止點

- 尚未連到 Harbor adapter、solver 或 official verifier；受控 CLI SQLite session 不等於它們，因此 A10、A11、A14 及 M3/M4 不可標通過。
- native boundary 已在單一、隔離的真 CLI + fake transport session 完整接通；它刻意依賴「一個 endpoint 對一個明示 session」和 `before_compaction` 的同步 permit handoff。並行、多 session 共用 endpoint、plugin 無法寫入 `0700` permit directory、或 SQLite transition 延遲超過 permit lifetime 都會 fail closed，尚未形成多 session deployment 成效結論。
- 尚未決定新的 event relevance threshold、數量上限、token budget 或 LLM relevance judge；程式要求 profile reference，沒有填入自行猜定的值。
- R014 已在程式加入可稽核的 explicit `unlimited` ledger activation；本次只用 fake local manager 驗證，未改寫或啟用 T2 真實 ledger。歷史 R009／R011 的 first attempt 目前**不可直接重用**：要在任何重用前，逐筆核對 raw source trace hash、完整 composed prompt hash（含 prior candidates）、manager profile/template/tokenizer、完整 messages/request bytes 和 source contract。尚無完成的 input-equivalence audit，故沒有 R012 新模型 call 或重用結論。
- 正式 12 × 3 × 2 試驗仍須等全部開發驗證、完整功能說明及使用者明確確認。
