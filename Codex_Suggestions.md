# Codex Suggestions – Entity History Tool Follow-ups

- **Exercise async_call paths**  
  Add unit tests for `EntityHistoryTool.async_call()` covering happy flow, bad entity IDs, invalid datetime input, and `since_entity_state` lookups. This keeps regressions from creeping into the async history retrieval and formatting code.

- **Graceful recorder fallback**  
  When the recorder integration is disabled or unavailable, `async_get_history` will raise. Catch this scenario and return a user-friendly error like “History recorder is not enabled,” matching other HA tooling.

- **Parameter validation polish**  
  Consider normalising boolean inputs (e.g. string `"false"`) before applying the summarisation logic; LLM callers often send strings and we currently treat any truthy value as “summarise”.

- **Time window UX**  
  Allow shorthand offsets such as `"last_24_hours"` or numeric hours to reduce the burden on prompts that need specific ISO timestamps.

- **Docs refresh**  
  Update `README.md` / `GLM_Changes.md` to reflect the new recorder dependency details and clarify that Home Assistant’s history/recorder integration must be active for the tool to respond.
