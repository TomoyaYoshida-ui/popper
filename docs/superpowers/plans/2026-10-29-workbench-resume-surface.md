# Workbench Resume Surface Implementation Plan

> **For agentic workers:** Execute task-by-task. Steps use checkbox syntax.

**Goal:** Surface same-run_dir resume fingerprint on workbench `status_brief` / LoopView, sharing CLI brief fields.

**Architecture:** Inject `project_resume_context` in `read_research_state` before `summarize_research_status`; LoopView conditionally renders checkpoint line.

**Tech Stack:** Python readout/demo; React LoopView + vitest; existing `resume_context`.

## Global Constraints

- Checkpoint stays unverified session notes; never evidence.
- No new API; no UI write form; no cross-study memory.

---

### Task 1: readout + Python test

- [ ] Inject resume in `readout.py` before summarize
- [ ] Assert checkpoint appears in `status_brief.resume` when recorded
- [ ] Run related Python tests

### Task 2: frontend

- [ ] Extend `api.ts` status_brief.resume type
- [ ] LoopView muted resume line; vitest with/without checkpoint

### Task 3: docs + ship

- [ ] Mark spec implemented; capability-boundary one sentence
- [ ] Commit and push
