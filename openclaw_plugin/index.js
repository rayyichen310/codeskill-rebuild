import { appendFileSync, chmodSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { writeLifecycleRecord } from "./lifecycle-record.js";

function audit(pluginConfig, kind, value) {
  const auditPath = typeof pluginConfig?.auditPath === "string" ? pluginConfig.auditPath : undefined;
  if (!auditPath) return;
  mkdirSync(dirname(auditPath), { recursive: true });
  appendFileSync(auditPath, `${JSON.stringify({ time_ms: Date.now(), kind, ...value })}\n`, "utf8");
  chmodSync(auditPath, 0o640);
}

function writeNativeSummaryPermit(pluginConfig, sessionId) {
  const record = writeLifecycleRecord(pluginConfig, sessionId, "codeskill_native_summary_permit", "native-summary");
  audit(pluginConfig, "native_summary_permit_written", {
    session_id: sessionId,
    filename: record.filename,
    payload_or_prompt_heuristic: "not_used",
  });
  return record;
}

function writeNormalCallBoundary(pluginConfig, sessionId) {
  const record = writeLifecycleRecord(pluginConfig, sessionId, "codeskill_normal_call_boundary", "normal-call");
  audit(pluginConfig, "normal_call_boundary_written", {
    session_id: sessionId,
    filename: record.filename,
    payload_or_prompt_heuristic: "not_used",
  });
}

export default definePluginEntry({
  id: "codeskill-r012-sidecar",
  name: "CODESKILL R012 sidecar",
  description: "CODESKILL native-summary permit sidecar.",
  register(api) {
    const pluginConfig = api.pluginConfig;
    // Some supported releases pass public StreamOptions.sessionId for normal
    // solver calls but omit it for their own native compaction stream. This is
    // an explicit lifecycle arm, not an inspection of stream content.
    let armedNativeSummary = null;
    audit(pluginConfig, "plugin_registered", {});

    api.on("before_compaction", (event, context) => {
      audit(pluginConfig, "before_compaction", {
        session_file: event.sessionFile,
        session_id: context?.sessionId,
        session_key: context?.sessionKey,
        message_count: event.messageCount,
      });
      if (typeof context?.sessionId === "string") {
        try {
          const record = writeNativeSummaryPermit(pluginConfig, context.sessionId);
          armedNativeSummary = { sessionId: context.sessionId, nonce: record.nonce, remainingAttempts: 4 };
        } catch (error) {
          audit(pluginConfig, "permit_not_written", {
            reason: error instanceof Error ? error.message : String(error),
            session_id: context.sessionId,
          });
        }
      } else {
        audit(pluginConfig, "permit_not_written", { reason: "before_compaction_hook_has_no_session_id" });
      }
    });
    api.on("after_compaction", (event, context) => {
      audit(pluginConfig, "after_compaction", {
        session_file: event.sessionFile,
        session_id: context?.sessionId,
        session_key: context?.sessionKey,
        compacted_count: event.compactedCount,
      });
    });

    api.registerProvider({
      id: "codeskill-r012",
      label: "CODESKILL R012 normal-call boundary",
      auth: [],
      wrapStreamFn: ({ streamFn }) => {
        audit(pluginConfig, "normal_call_wrapper_created", {});
        if (typeof streamFn !== "function") return undefined;
      return (model, context, options) => {
        const sessionId = options?.sessionId;
        if (typeof sessionId === "string") {
          // A positively identified ordinary solver call always disarms a
          // pending native-summary handoff before reaching the proxy.
          armedNativeSummary = null;
          writeNormalCallBoundary(pluginConfig, sessionId);
          return streamFn(model, context, options);
        }
        if (armedNativeSummary && armedNativeSummary.remainingAttempts > 0) {
          armedNativeSummary.remainingAttempts -= 1;
          audit(pluginConfig, "native_summary_stream_without_session_accepted", {
            session_id: armedNativeSummary.sessionId,
            nonce: armedNativeSummary.nonce,
            remaining_attempts: armedNativeSummary.remainingAttempts,
            payload_or_prompt_heuristic: "not_used",
          });
          return streamFn(model, context, options);
        }
        throw new Error("CODESKILL unbound stream lacks public StreamOptions.sessionId");
      };
      },
    });
  },
});
