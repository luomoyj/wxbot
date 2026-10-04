---
name: wxclear
description: Clear the current wxbot Codex Thread and fallback conversation history while preserving other conversations, the paired whitelist user, and duplicate-message records. Use when the user invokes /wxclear or asks to reset the WeChat AI conversation context.
---

# Clear wxbot conversation history

Run this exact command from the repository root:

```powershell
.\.venv\Scripts\wxbot.exe context clear
```

Return the command's Chinese status message. Do not inspect or print files under `data/`.
