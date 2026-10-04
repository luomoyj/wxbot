---
name: wxreset
description: Preview and, only after explicit second confirmation, clear all locally stored wxbot AI state while preserving WeChat login, checkpoints, task records, model settings, metrics, and project files. Use when the user invokes /wxreset, $wxreset, asks to clear all AI state, or confirms a reset preview.
---

# Reset wxbot AI state

From the repository root, preview the exact reset scope:

```powershell
.\.venv\Scripts\wxbot.exe reset-ai-state
```

Return the Chinese preview and wait. Do not reset anything during this step.

Only when the user explicitly confirms after that preview, run:

```powershell
.\.venv\Scripts\wxbot.exe reset-ai-state --confirm
```

Return the command's Chinese status message. Do not inspect, print, or manually delete files under `data/`.
