# CODESKILL OpenClaw Sidecar

這個套件只使用 OpenClaw 的公開 plugin API。它不修改、fork、patch 或替換 OpenClaw
原始碼。用途是把 native compaction 的公開 `before_compaction` lifecycle 事件，以一個
短效、每 session 的 permit 交給 CODESKILL proxy；它不檢查 prompt、訊息 role、token
數或 request 外型來猜測 summary。

## 邊界與運作方式

一個 CODESKILL sidecar endpoint **只能服務一個確定的 OpenClaw session ID**。設定中的
`sessionId`、plugin hook／provider wrapper 提供的 session ID，和 Python gate 的
`expected_session_id` 必須完全相同。

1. 每個一般 solver stream call 會先經公開 provider `wrapStreamFn`，同步以 temp-file +
   rename 寫入 mode `0640` 的 normal-call boundary；setgid permit directory 讓 container
   producer 與 host sidecar 共享既有 GID，但不開放給 other users。它是正向的「這是一般 call」證據，
   不讀 prompt、訊息 role、token 數或 request 外型。
2. OpenClaw 觸發公開 `before_compaction` hook 時，plugin 同步寫入同一 session 的短效
   permit；內容只有 trial、session、時間和隨機 nonce。
3. 對應的單一 CODESKILL endpoint 只有在本 process 剛建立的 active permit 存在、且沒有
   normal-call boundary 時，才原樣轉送 bounded native summary request。部分支援 release 的
   public wrapper 對 native stream 不提供 `StreamOptions.sessionId`；plugin 只允許同一 process
   中剛由 `before_compaction` arm 的這類 stream，最多四次，並記錄 lifecycle audit。它不讀取
   stream payload。任何正常 session call 都會先解除 arm 並寫 normal-call boundary；該 boundary
   會撤銷未完成 permit，request 照一般 overlay 處理，所以取消、no-op 或失敗後的 solver request
   無法借用 permit。
4. sidecar 唯讀追蹤同一 session 的 SQLite `transcript_events`。只有新的 native
   `compaction` row 才能退休 permit；之後 solver request 回到一般 overlay，完成 task
   relocation、event retire，並容許下一個相符工具批次重新檢索／注入 event。

plugin 與 gate 不會從 native summary 擷取、摘要或另存欄位；不過 proxy 的 request evidence
為了證明「原樣轉送」，會保留完整 `native_request` 和 `forwarded_request`，兩者必須完全相同。
這些是受控試驗的原始 request artifact，不是以內容判斷 summary 的 heuristic。

缺少、過期、重複、不同 trial、不同 session 的 permit／normal boundary，proxy restart 後尚未
解析的 permit，或四次 native summary retry 仍沒有 SQLite transition，都會在開啟 upstream 前
以 409 停止。這個 fail-closed 行為是設計的一部分。

## 安裝

以下示例把 extension 放在 OpenClaw state 以外的受控目錄。目錄與 permit directory
由 OpenClaw producer 與 CODESKILL sidecar 的共享 GID 存取，不要求兩者使用同一 UID。

```bash
install -d -m 755 /opt/codeskill/openclaw-sidecar
cp index.js lifecycle-record.js openclaw.plugin.json package.json README.md /opt/codeskill/openclaw-sidecar/
cd /opt/codeskill/openclaw-sidecar
npm install --omit=dev --ignore-scripts
install -d -m 2770 /var/lib/codeskill/native-summary-permits/session-123
```

`package.json` 的 peer range 是套件安裝相容宣告，**不是**已驗證版本清單。目前唯一經過
完整 native boundary 受控驗證的官方版本是 `openclaw@2026.9.3`。升級或改用其他版本前，
先在隔離 session 重跑受控驗證；不要把這個 plugin 加到多 session 共用的 sidecar endpoint。

在 OpenClaw 設定中載入 extension，並填入該次受控 session 的唯一值：

```json
{
  "plugins": {
    "allow": ["codeskill-r012-sidecar"],
    "load": { "paths": ["/opt/codeskill/openclaw-sidecar"] },
    "entries": {
      "codeskill-r012-sidecar": {
        "enabled": true,
        "config": {
          "permitDirectory": "/var/lib/codeskill/native-summary-permits/session-123",
          "trialId": "r012-session-123",
          "sessionId": "session-123",
          "auditPath": "/var/lib/codeskill/native-summary-permits/session-123/plugin-audit.jsonl"
        }
      }
    }
  }
}
```

`auditPath` 是可選的操作稽核檔；另外三個欄位是 manifest 強制要求的欄位。設定中的
`sessionId` 必須與 OpenClaw CLI 的 `--session-id` 相同。

同一份 OpenClaw 設定還必須明示把主要模型指向 sidecar provider。這個 binding 是正常
solver call 通過公開 `wrapStreamFn`、寫入 normal-call boundary 的必要條件；sidecar 的
`--check-config` 會拒絕缺少 provider、model、`agents.defaults.model.primary`、plugin allow/load/enable
或 plugin 的 trial/session/permit 對應的設定。
將 `<SIDECAR-PORT>`、`<UPSTREAM-MODEL-ID>` 及其他尖括號欄位替換成已核定的單一 trial 值：

```json
{
  "models": {
    "providers": {
      "codeskill-r012": {
        "baseUrl": "http://127.0.0.1:<SIDECAR-PORT>/v1",
        "apiKey": "${CODESKILL_SIDECAR_KEY}",
        "api": "openai-completions",
        "models": [{
          "id": "<UPSTREAM-MODEL-ID>",
          "name": "CODESKILL sidecar upstream model",
          "reasoning": false,
          "input": ["text"],
          "contextWindow": <APPROVED-CONTEXT-TOKENS>,
          "maxTokens": <APPROVED-MAX-OUTPUT-TOKENS>,
          "compat": { "supportsUsageInStreaming": true }
        }]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": { "primary": "codeskill-r012/<UPSTREAM-MODEL-ID>" },
      "contextTokens": <APPROVED-CONTEXT-TOKENS>,
      "maxConcurrent": 1
    }
  }
}
```

`upstream.endpoint` 是實際 solver 的 `/v1/chat/completions` URL；`tokenizer.baseUrl` 是同一
已核定服務提供 `/tokenize` 的 base URL。兩者都不是 sidecar URL。CODESKILL 不替模型、
tokenizer、context/output token 或 selection 值填預設值。

## CODESKILL sidecar 接線

建立 proxy 時，以同一組 trial、session 和 permit directory 建立 gate：

```python
from pathlib import Path

from codeskill_rebuild.openclaw_compaction import SqliteTranscriptCompactionDetector
from codeskill_rebuild.openclaw_native_summary import NativeSummaryPermitGate
from codeskill_rebuild.openclaw_proxy import DurableProxyService

session_id = "session-123"
permit_dir = Path("/var/lib/codeskill/native-summary-permits/session-123")
detector = SqliteTranscriptCompactionDetector(
    location_provider=session_location_provider,
    evidence_dir=proxy_evidence_dir,
)
gate = NativeSummaryPermitGate(
    permit_dir=permit_dir,
    expected_session_id=session_id,
    trial_id="r012-session-123",
)
service = DurableProxyService(
    overlay=overlay,
    transport=transport,
    compaction_evidence_provider=detector,
    native_summary_permit_gate=gate,
)
```

`session_location_provider` 必須回傳 OpenClaw 實際輸出的 SQLite marker。sidecar 只讀
SQLite；plugin/gate 不進行 native summary 的內容轉換，但 proxy request evidence 會保留原始
request 以驗證 raw forwarding。不得共用 `permit_dir`、sidecar process、session ID 或
`trialId` 給另一個 session。

## 可執行 sidecar

以 CODESKILL repository root 為工作目錄，先用既有 freeze entrypoint 建立這一 trial 的
R012 lifecycle state。sidecar 只讀取其中**仍為 pending 的單一 frozen assignment**；它不會
建立 bank、更新 bank、啟動 manager，或自動 finish/release lifecycle。以下命令只是 freeze
的例子，所有 profile/bank/instance/repeat 必須是已核定實驗設定：

```bash
PYTHONPATH=src python3 scripts/run_m3_r012_lifecycle.py freeze \
  --state /var/lib/codeskill/runs/session-123/lifecycle.json \
  --profile /var/lib/codeskill/profiles/r012-approved.json \
  --instance-id terminal-bench/<FROZEN-INSTANCE> \
  --repeat <FROZEN-REPEAT> \
  --arm-bank full=/var/lib/codeskill/banks/full.json \
  --spec docs/archive/REPRODUCTION_SPEC.md \
  --decisions docs/archive/RESEARCH_DECISIONS.md
```

從 lifecycle state 的該 `trial_id` assignment 取得 `frozen_bank.state_sha256`，並以
`sha256sum lifecycle.json` 取得 `lifecycleStateSha256`。下方欄位都必須填入實值；保留尖括號
或省略 selection threshold、token budget、profile reference、bank/profile hash 都會使
`--check-config` 失敗。這避免在未核定的情況下暗中發明相關性門檻、技能數量或 token budget。

```json
{
  "trialId": "r012-session-123",
  "sessionId": "session-123",
  "sessionMarker": "sqlite:main:session-123:/state/agents/main/sessions/sessions.json",
  "permitDirectory": "/var/lib/codeskill/native-summary-permits/session-123",
  "overlay": {
    "statePath": "/var/lib/codeskill/runs/session-123/overlay.json",
    "evidenceDirectory": "/var/lib/codeskill/runs/session-123/proxy",
    "maxInputTokens": 120000
  },
  "tokenizer": { "baseUrl": "http://tokenizer.example/v1", "timeoutSeconds": 30 },
  "upstream": { "endpoint": "http://solver.example/v1/chat/completions", "timeoutSeconds": 300 },
  "listen": { "host": "127.0.0.1", "port": 18080 },
  "openclaw": {
    "configPath": "/var/lib/codeskill/openclaw/openclaw.json",
    "pluginPath": "/opt/codeskill/openclaw-sidecar",
    "providerId": "codeskill-r012",
    "modelId": "<UPSTREAM-MODEL-ID>"
  },
  "selection": { "mode": "frozen-bank" },
  "retrieval": {
    "trialId": "r012-session-123",
    "instanceId": "terminal-bench/<FROZEN-INSTANCE>",
    "lifecycleStatePath": "/var/lib/codeskill/runs/session-123/lifecycle.json",
    "lifecycleStateSha256": "<SHA256-OF-LIFECYCLE-STATE>",
    "profileSha256": "<SHA256-OF-FROZEN-R012-PROFILE>",
    "bankSnapshotSha256": "<FROZEN-BANK-STATE-SHA256>",
    "encoder": {
      "kind": "minilm",
      "repoId": "sentence-transformers/all-MiniLM-L6-v2",
      "revision": "<EXPLICIT-MINILM-REVISION>"
    },
    "taskSelection": {
      "selectionRuleRef": "<APPROVED-TASK-SELECTION-RULE>",
      "threshold": <APPROVED-TASK-THRESHOLD>,
      "maxMatchingSkills": <APPROVED-TASK-MAXIMUM>
    },
    "eventSelection": {
      "profileRef": "<EXACT-FROZEN-PROFILE-REF>",
      "selectionRuleRef": "<EXACT-FROZEN-EVENT-RULE-REF>",
      "threshold": <APPROVED-EVENT-THRESHOLD>,
      "maxMatchingSkills": <EXACT-FROZEN-EVENT-MAXIMUM>,
      "skillTokenBudget": <EXACT-FROZEN-EVENT-TOKEN-BUDGET>
    }
  },
  "maxForwardedRequests": 8,
  "maxOutputTokens": 8192
}
```

先只驗證設定，再明示啟動 listener：

```bash
PYTHONPATH=src python3 scripts/run_openclaw_r012_sidecar.py --config sidecar.json --check-config
PYTHONPATH=src python3 scripts/run_openclaw_r012_sidecar.py --config sidecar.json
```

若 upstream 需要 authorization，將 `upstream.authorizationEnv` 設為環境變數名稱；程式不會把
secret 寫入設定或 evidence。

`frozen-bank` 是唯一的一般模式：每個 task/event request 都會透過現有 MiniLM encoder、
`SkillBank.eligible(instance_id=..., granularity=...)` 的同題 provenance 排除，以及明示
threshold/limit 從 frozen snapshot 選取。request evidence 會保存 query、ranked result、bank
snapshot hash、lifecycle state hash 和 profile hash。已注入的 skill 仍由既有 durable overlay
寫入 supplied evidence；完成 trial 後，仍須以 `run_m3_r012_lifecycle.py finish`/`release` 的
明示流程處理，不能由 sidecar 自動演化或更新 bank。

每一批新選出的 event skill 在 commit 前，sidecar 會以既有 complete-payload token counter
比較「已存在 overlay 的完整 request」與「在實際 tool-batch anchor 插入所有新 event block 後的
完整 request」。兩者差值必須小於或等於 frozen profile 的 `skillTokenBudget`；超出時以 409
`codeskill_event_skill_budget_exceeded` 停止，不截斷、丟棄或任意挑部分 skills。sidecar 也只接受
profile 中 `full_lifecycle_arms` 所列 arm 的 pending frozen assignment，避免 baseline arm 意外注入
skill。

`fixture-test-only` 僅保留給受控 fake probe。它必須在 `selection` 內明寫
`"acknowledgement": "not-a-retrieval-or-lifecycle-run"`，而不是 root-level `taskSkill`/
`eventSkill`。它不代表 retrieval、provenance exclusion 或 lifecycle 已執行，不能作為一般
安裝或研究試驗設定。

## 驗證與限制

在隔離環境確認以下 evidence 都存在，才可視為 native boundary 已接通：

- plugin audit 有 `normal_call_boundary_written`，並在 compaction 前後分別出現
  `before_compaction` 與 `native_summary_permit_written`；
- proxy 有 `r012_native_summary_upstream_request`，且 `native_request` 和
  `forwarded_request` 完全相同；
- SQLite detector 記錄新的 compaction row，並將 permit 狀態改為
  `retired_after_native_sqlite_transition`；
- 同一 transition 後的一般 solver request 有 task relocation 與 event retirement evidence，
  其後一個新的完整工具批次能重新注入相同 event skill；
- 取消／no-op／失敗模擬下，normal-call boundary 會撤銷 permit，下一個一般 request 仍走
  overlay，不能 raw-bypass。

### 官方發行版相容性重跑

下列命令
以官方 npm registry 的指定 tarball 建立全新工作目錄，只把**該 exact tarball** 安裝到全新的 per-run npm prefix，執行
官方 `npm install --prefix <isolated> <tarball> --omit=dev --no-audit --no-fund`，不安裝、改動或掛載共享
OpenClaw。v2026.9.3 的 package engine 要求 Node ≥24.16；runner 預設以 Docker `node:24-slim`
完成 lifecycle，並以同一 image 及 Docker read-only mount 對該 prefix 的
`node_modules/openclaw/openclaw.mjs` 執行上方的假傳輸 CLI probe，最多 8 個 HTTP request、900 秒，並輸出
tarball SHA-256 與 native SQLite transition evidence：

```bash
npm view openclaw version
PYTHONPATH=src python3 scripts/run_openclaw_r012_official_release_compat.py \
  --version 2026.9.3 \
  --work-dir /var/tmp/codeskill-r012-openclaw-2026.9.3
```

已驗版本：`openclaw@2026.9.3`，tarball SHA-256 為
`d1c63366833f8ae4a6ab4f3b60b1aa84ca82d03dba13d3d3eba989aa159e2449`；其 isolated
fake-only run 有 4/4 CLI exit 0、8/8 fake request、0 real model call，並確認 SQLite transition
之後的 event reinjection。這只驗證 native-compaction 接線與已驗版本的 public plugin compatibility。
它不驗證 Harbor、solver、official verifier、tokenizer 計量或正式 trial 結果。
