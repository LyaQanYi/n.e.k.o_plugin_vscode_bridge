# Changelog

## 0.1.3

- Unlimited steps and no default overall task timeout
- Live runtime phases, step/elapsed, tool events and activity feed in the panel
- Direct coding task entry in the panel
- Chat coding takeover, pre-turn tool rule, factual result bubbles, false-edit claim guard

## 0.1.2

- Start the WebSocket bridge on the persistent command/UI loop instead of the temporary startup loop
- Auto-recover when the listener is bound to a stale event loop
- Read the plugin version from the installed manifest for discovery/hello

## 0.1.1

- Three coding permission modes: ask, auto_edit, full_access
- Redesigned Hosted UI panel with tabs, status badges, tables and progress
- Chinese localization for panel, config and VSCode command surfaces

## 0.1.0

- Local WebSocket bridge plugin
- Discovery file and token authentication
- Hosted UI panel with connection/model/task controls
- `@llm_tool` status, coding task, cancel, and main-mode granular tools
- Dedicated coding agent using the plugin model gateway
- Diff approval with snapshot/rollback
