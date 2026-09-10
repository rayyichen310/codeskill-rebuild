# T2 → <MODEL_SERVICE_HOST> 模型服務連線

記錄日期：2026-09-05。這份文件只沿用既有服務連線資訊；沒有複製舊 CODESKILL 實作或實驗結果。

## 1. 已從 T2 live 核對的 endpoint

| 用途 | OpenAI-compatible base URL | model ID |
|---|---|---|
| 第一版預設所有生成式角色 | `http://<MODEL_SERVICE_HOST>:31000/v1` | `deepseek-ai/DeepSeek-V4-Flash` |
| 備用，尚未納入第一版對照 | `http://<MODEL_SERVICE_HOST>:30002/v1` | `Qwen/Qwen3.5-9B` |

以上 URL 是從 T2 可到達的內部服務；不是 localhost，也沒有在本次建立新的 tunnel。

- T2：`<T2_HOST>`，帳號 `<T2_HOST>`。
- <MODEL_SERVICE_HOST>：SSH alias `<MODEL_SERVICE_HOST>`，IP `<MODEL_SERVICE_HOST>`，本次 hostname `esc8ka-e13p`，帳號 `<REMOTE_USER>`。
- Windows 管理連線使用既有專用設定：`ssh -F <LOCAL_PATH><LOCAL_USER>/.codex/ssh/config t2-codex` 或同設定的 `<MODEL_SERVICE_HOST>` alias。
- 不複製 SSH private keys；不更改 host-key 驗證；不把啟動舊服務的 script 當新專案 runner。

## 2. 本次驗證的服務設定

### DeepSeek Flash

- `/v1/models`：宣告最大模型長度 `524288`。
- `/get_server_info`：`context_length=524288`、`max_req_input_len=524282`。
- `model_path`、`tokenizer_path`：`<REMOTE_USER_HOME>/models/DeepSeek-V4-Flash`（<MODEL_SERVICE_HOST> 上的路徑，不是 T2 本機路徑）。
- SGLang `0.5.16`、`tp_size=2`、`max_running_requests=1`。
- `reasoning_parser=deepseek-v4`、`tool_call_parser=deepseekv4`。
- `allow_auto_truncate=false`，`status=ready`。
- `revision=null`、`weight_version=default` 不構成不可變模型版本，正式 run 還需要 tokenizer / config / checkpoint identity 的證據。

### Qwen 9B

- `/v1/models` 宣告 `max_model_len=262144`。
- 但 `/get_server_info` 的 `max_req_input_len=107351`，`max_total_num_tokens=107357`，`context_length=null`。
- 因此不能把模型宣告的 262144 當成本服務可直接接受的輸入上限；第一版仍選 DeepSeek。

### 已確認與未確認的區別

- 已確認：T2 到兩個服務的 HTTP metadata GET 成功，不帶 Authorization header。
- 未確認：推論是否需要認證、messages/chat template 完整相容性、JSON 操作輸出、tokenizer 精確計量、長軌跡輸入、實際 agent tool/action 路徑。
- 本次沒有送 inference 或大 context 測試，沒有啟停、部署或修改 GPU 服務。
- ready、GPU 某一刻利用率 0% 不等於服務專用或歷史上沒有其他負载；正式跑之前再確認。

## 3. 記錄来源與保存範圍

1. 舊 task：`開始 CODESKILL reproduction`，task ID `01a066f6-1b26-74b2-be16-62dfe2b4e2fc`。
2. 歷史連線線索：<MODEL_SERVICE_HOST> 上 `<REMOTE_USER_HOME>/codeskill/scripts/codeskill_sglang.sbatch` 曾用 port 31000、DeepSeek-V4-Flash。這是定位線索；本次沒有複製／執行該檔，不以歷史設定代替現況。
3. 本次 live evidence：`<PROJECT_ROOT>/evidence/connection/metadata-20260905.json`，在 `2026-09-05T05:08:05.346108+00:00` 從 T2 取得。
4. 可用設定：`<PROJECT_ROOT>/configs/model-endpoints.json`。

保存內容採白名單：endpoint、model ID、context、tokenizer path、公開服務參數及驗證狀態。沒有 API token、private key 或原始 `/get_server_info` 的其他敏感欄位。若未來需要認證，使用受控環境變數／既有安全憑證位置，不提交到本專案。

## 4. 唯讀重新確認方式（在 T2 執行）

```bash
curl --fail --silent --show-error --max-time 10 http://<MODEL_SERVICE_HOST>:31000/v1/models
curl --fail --silent --show-error --max-time 10 http://<MODEL_SERVICE_HOST>:30002/v1/models
```

不要把完整 `/get_server_info` 原文直接貼進報告或 log；只輸出既定白名單欄位。
