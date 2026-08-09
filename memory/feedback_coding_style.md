---
name: feedback-coding-style
description: User's preferences for how to write and present code changes in this project
metadata:
  type: feedback
---

No test sections in PR descriptions — user finds them unnecessary.

**Why:** User pushes and merges frequently; test checklists add noise with no value in this workflow.

**How to apply:** When creating PRs with `gh pr create`, omit the "Test plan" section entirely. Summary bullets only.

---

No trailing summaries at end of responses after completing a task.

**Why:** User can read the diff/output themselves.

**How to apply:** After a tool call completes (commit, edit, etc.), one short sentence at most — not a recap of everything that changed.

---

Utilities should not commit — callers own transactions.

**Why:** A prior bug caused non-atomic commits when `sql_add_user_to_access_group` committed internally before the caller could add related records.

**How to apply:** Helper functions in `utils/` should `session.add()` but never `session.commit()`. Always add explicit `session.commit()` at the call site.
