import { chmodSync, mkdirSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";

export const LIFECYCLE_DIRECTORY_MODE = 0o2770;
export const LIFECYCLE_RECORD_MODE = 0o640;

export function writeLifecycleRecord(pluginConfig, sessionId, kind, filenamePrefix) {
  const permitDirectory =
    typeof pluginConfig?.permitDirectory === "string" ? pluginConfig.permitDirectory : undefined;
  const trialId = typeof pluginConfig?.trialId === "string" ? pluginConfig.trialId : undefined;
  const expectedSessionId = typeof pluginConfig?.sessionId === "string" ? pluginConfig.sessionId : undefined;
  if (!permitDirectory || !trialId || !expectedSessionId) {
    throw new Error("CODESKILL sidecar is missing required plugin configuration");
  }
  if (sessionId !== expectedSessionId) {
    throw new Error("CODESKILL sidecar session does not match the isolated plugin configuration");
  }
  const nonce = randomUUID().replaceAll("-", "");
  // The session identifier stays in the JSON evidence; never place it in a
  // filesystem path supplied to the public plugin runtime.
  const filename = `${filenamePrefix}-${nonce}.json`;
  const target = join(permitDirectory, filename);
  const temporary = join(permitDirectory, `.${filename}.tmp`);
  const record = {
    schema_version: 1,
    kind,
    trial_id: trialId,
    session_id: sessionId,
    issued_at_unix_ms: Date.now(),
    nonce,
  };
  // The directory is a bind mount created by the host-side driver. setgid
  // preserves that directory's GID for records written by a root container,
  // while 0640 lets the host sidecar's group read the record without making
  // it world-readable or changing task workspace ownership.
  mkdirSync(permitDirectory, { recursive: true, mode: LIFECYCLE_DIRECTORY_MODE });
  chmodSync(permitDirectory, LIFECYCLE_DIRECTORY_MODE);
  writeFileSync(temporary, JSON.stringify(record), {
    encoding: "utf8",
    mode: LIFECYCLE_RECORD_MODE,
  });
  chmodSync(temporary, LIFECYCLE_RECORD_MODE);
  renameSync(temporary, target);
  return { filename, nonce };
}
