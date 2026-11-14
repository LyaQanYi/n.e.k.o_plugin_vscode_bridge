# N.E.K.O. VSCode Bridge Protocol

Protocol version: **1**

Transport: WebSocket over `ws://127.0.0.1:<port>`, JSON text frames, UTF-8.
The N.E.K.O. plugin hosts the server. The VSCode extension is the client.

## 1. Discovery

The plugin writes one file per instance:

```text
~/.neko-vscode/ide/neko-<pid>.json
```

```json
{
  "v": 1,
  "protocol": 1,
  "port": 45821,
  "token": "...",
  "pid": 12345,
  "instance_id": "...",
  "plugin_version": "0.1.0",
  "started_at": "2026-10-18T12:00:00Z",
  "updated_at": "2026-10-18T12:00:15Z",
  "remote_authority": null,
  "external_uri": null
}
```

The extension scans the directory, ignores files whose `updated_at` is older than
45 seconds, sorts by `updated_at` descending, and tries to connect. Manual
`neko.bridge.url` + `neko.bridge.token` overrides discovery.

## 2. Handshake

Client → server:

```json
{
  "type": "hello",
  "v": 1,
  "token": "...",
  "client": {"name": "vscode", "version": "0.1.0", "platform": "darwin"},
  "workspace": {
    "id": "...",
    "name": "project-a",
    "roots": [{"name": "project-a", "uri": "file:///...", "path": "/..."}],
    "workspaceFile": null,
    "isTrusted": true,
    "remoteName": null
  }
}
```

Server → client:

```json
{
  "type": "hello_ack",
  "v": 1,
  "server": {"name": "neko-vscode-bridge", "version": "0.1.0", "pluginVersion": "0.1.0"},
  "sessionId": "...",
  "capabilities": ["read", "write", "search", "diagnostics", "diff", "terminal", "multiRoot"]
}
```

Failure:

```json
{"type": "hello_error", "code": "AUTH_FAILED", "message": "..."}
```

The server closes the socket after a handshake failure. The token is compared
with a constant-time comparison. The server binds only `127.0.0.1`.

## 3. RPC

Request:

```json
{"type": "request", "id": "r1", "method": "file.read", "params": {}, "workspaceId": "..."}
```

Success:

```json
{"type": "response", "id": "r1", "ok": true, "result": {}}
```

Failure:

```json
{"type": "response", "id": "r1", "ok": false, "error": {"code": "STALE_FILE", "message": "...", "details": {}}}
```

Cancellation:

```json
{"type": "cancel", "id": "r1"}
```

The client may send several requests concurrently. The server accepts at most 32
pending requests per connection. A request that is not answered within its
timeout is cancelled.

## 4. Events

```json
{"type": "event", "event": "window.focus", "data": {"workspaceId": "...", "timestamp": 1760000000.0}}
{"type": "event", "event": "task.progress", "data": {"taskId": "...", "status": "running", "phase": "thinking", "step": 3, "maxSteps": 0, "message": "第 3 步：模型正在思考", "elapsedSeconds": 42, "lastTool": "read_file"}}
{"type": "event", "event": "changes.decision", "data": {"changeSetId": "...", "status": "applied", "files": []}}
```

Heartbeat:

```json
{"type": "ping", "ts": 1760000000.0}
{"type": "pong", "ts": 1760000000.0}
```

The plugin sends `ping` every 15 seconds. The extension answers with `pong`.
The plugin closes a connection that has not answered for 45 seconds.

## 5. Methods

### `workspace.info`

Params: none.

Result:

```json
{
  "name": "project-a",
  "workspaceFile": null,
  "roots": [{"name": "project-a", "uri": "file:///...", "path": "/..."}],
  "isTrusted": true,
  "remoteName": null,
  "vscodeVersion": "1.95.0",
  "extensionVersion": "0.1.0"
}
```

### `workspace.listFiles`

Params: `glob?` (string, default `**/*`), `exclude?` (string[]), `maxResults?` (int, default 5000).

Result: `{"files": [{"path": "...", "root": "..."}], "truncated": false}`

### `workspace.activeEditor`

Params: none.

Result: `{"path": "...", "languageId": "typescript", "selection": {...}, "visibleRanges": [...], "text": "...", "truncated": false}` or `null`.

### `workspace.saveAll`

Params: none. Result: `{"saved": true}`.

### `file.read`

Params: `path` (string, workspace-relative or absolute inside a root), `startLine?` (1-based), `endLine?`, `maxChars?` (default 200000).

Result:

```json
{
  "path": "src/App.tsx",
  "text": "...",
  "hash": "sha256...",
  "totalLines": 120,
  "languageId": "typescript",
  "size": 4096,
  "mtime": 1760000000.0,
  "truncated": false,
  "binary": false
}
```

### `file.stat`

Params: `path`.

Result: `{"path": "...", "exists": true, "isFile": true, "size": 4096, "mtime": 1760000000.0, "hash": "...", "binary": false}`.

### `search.text`

Params: `query` (string), `regex?` (bool, default false), `include?` (string), `exclude?` (string[]), `maxResults?` (int, default 200), `contextLines?` (int, default 1).

Result:

```json
{
  "matches": [
    {"path": "src/App.tsx", "line": 12, "column": 4, "preview": "...", "context": ["..."]}
  ],
  "truncated": false,
  "filesScanned": 42
}
```

### `diagnostics.get`

Params: `path?`, `severity?` (`error|warning|info|hint|all`), `maxResults?` (default 100).

Result:

```json
{
  "errors": 0,
  "warnings": 2,
  "infos": 0,
  "items": [
    {"path": "src/App.tsx", "line": 12, "character": 4, "severity": "error", "message": "...", "source": "ts", "code": "2322"}
  ]
}
```

### `changes.propose`

Params:

```json
{
  "changeSetId": "cs_...",
  "summary": "Update login page colors",
  "wait": true,
  "waitMs": 900000,
  "autoApply": false,
  "files": [
    {
      "path": "src/App.tsx",
      "operation": "update",
      "baseHash": "sha256...",
      "newText": "...",
      "summary": "..."
    }
  ]
}
```

`operation` is `update`, `create`, `delete`, or `rename`. For `rename`, use
`fromPath` and `toPath`; `path` is the destination.

If `wait` is true, the request stays open until the user applies or rejects the
change set, or until `waitMs` expires. If `wait` is false, the result is returned
immediately. `autoApply` applies the change set without opening the diff approval
flow; the plugin sets it only when the task approval mode is `auto`.

Result: `{"changeSetId": "...", "status": "pending|applied|rejected|stale", "files": [{"path": "...", "status": "..."}]}`.

### `changes.apply` / `changes.reject`

Params: `changeSetId`. Result: `{"changeSetId": "...", "status": "applied|rejected"}`.

### `changes.decide`

Params: `changeSetId`, `apply` (bool). Idempotent. Result:
`{"changeSetId": "...", "status": "applied|rejected", "alreadyDecided": false}`.

### `changes.status`

Params: `changeSetId`. Result: `{"changeSetId": "...", "status": "...", "files": [...]}`.

### `changes.snapshot` / `changes.rollback`

`changes.snapshot` params: `changeSetId`. Result: `{"snapshotId": "...", "path": "..."}`.
`changes.rollback` params: `snapshotId?` (default latest). Result: `{"restored": ["..."]}`.

### `command.run`

Params: `command` (string), `args?` (string[]), `cwd?` (string), `timeoutSeconds?` (default 120), `reason` (string).

The extension asks the user for confirmation before running. The command is
executed without a shell by default. Result:

```json
{"exitCode": 0, "signal": null, "stdout": "...", "stderr": "...", "durationMs": 1234, "truncated": false}
```

### `editor.open`

Params: `path`, `startLine?`, `endLine?`. Result: `{"opened": true}`.

### `task.cancel`

Params: `taskId?`. Result: `{"cancelled": true}`.

### `task.progress`

This is usually sent as an event, not a request. If sent as a request:
params `{taskId, status, phase, step, maxSteps, message, elapsedSeconds, phaseElapsedSeconds, lastTool, filesChanged}`; result `{"ok": true}`. `maxSteps = 0` means unlimited; `activity` is available in the plugin UI context, not in this event.

## 6. Error codes

| Code | Meaning |
|---|---|
| `AUTH_FAILED` | Token mismatch |
| `PROTOCOL_MISMATCH` | Unsupported protocol version |
| `WORKSPACE_NOT_FOUND` | Unknown workspace |
| `WORKSPACE_BUSY` | A write task is already running in this workspace |
| `PATH_OUTSIDE_WORKSPACE` | Path escapes all workspace roots |
| `FILE_NOT_FOUND` | File does not exist |
| `FILE_TOO_LARGE` | File exceeds the read limit |
| `BINARY_FILE` | Binary file rejected |
| `STALE_FILE` | `baseHash` no longer matches |
| `APPROVAL_REJECTED` | User rejected the change |
| `APPROVAL_TIMEOUT` | Approval wait expired |
| `ALREADY_DECIDED` | Change set was already decided |
| `COMMAND_DENIED` | Command rejected by policy or user |
| `COMMAND_TIMEOUT` | Command exceeded its timeout |
| `CANCELLED` | Request or task cancelled |
| `UNTRUSTED_WORKSPACE` | Workspace is not trusted |
| `MODEL_NOT_BOUND` | Coding model usage is not bound |
| `MODEL_CAPABILITY_MISMATCH` | Bound model lacks `tool_calling` |
| `MODEL_TIMEOUT` | Model request timed out |
| `MODEL_ERROR` | Model request failed |
| `INTERNAL_ERROR` | Unexpected server error |

## 7. Compatibility

Both repositories keep a copy of this document and a `protocol-version.json`.
The `protocol` number must match on both sides. The handshake rejects a
mismatch with `PROTOCOL_MISMATCH`. Additive methods/fields are allowed within a
protocol version; removing or changing existing fields requires a version bump.
