---
name: wxstatus
description: Check whether the background wxbot WeChat auto-reply process is running. Use when the user invokes /wxstatus or asks for this project's automatic reply status.
---

# Check wxbot status

Run this exact command from the repository root:

```powershell
.\.venv\Scripts\wxbot.exe status
```

Return the command's Chinese status message. Do not inspect files under `data/` directly.
