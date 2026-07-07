# Autonomous Document Agent 
## 1. Setup (2 minutes)

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env, paste in a free key from https://console.groq.com/keys
uvicorn app.main:app --reload
```

In a second terminal:
```bash
python3 test_requests.py
```

This hits `POST /agent` with both required test cases and saves the resulting `.docx` files locally.

Open the browser UI at:
```bash
http://127.0.0.1:8000/
```

The frontend is served directly by FastAPI, so one process powers both the API and the public interface.

Manual test:
```bash
curl -X POST http://127.0.0.1:8000/agent \
  -H "Content-Type: application/json" \
  -d '{"request": "Create meeting minutes for a sprint planning session"}'
```

---

## 2. Architecture

```
Request ──▶ Planner (LLM, JSON mode) ──▶ [TaskStep, TaskStep, ...]
                                                │
                                                ▼
                                         Executor (dispatch loop)
                                     ┌──────────┼──────────┐
                                     ▼          ▼          ▼
                              gather_info  draft_section  generate_docx
                              (mock tool)   (LLM call)   (python-docx)
                                                │
                                                ▼
                                     Reflector (LLM, independent pass)
                                                │
                                     needs_retry? ──yes──▶ one bounded
                                                │           retry pass
                                                no
                                                │
                                                ▼
                                        Final .docx + JSON response
```

**Files:**
- `app/schemas.py` — Pydantic contracts (request/response/plan/reflection)
- `app/tools.py` — `mock_data_lookup` (stand-in for a real system-of-record) and `generate_docx` (python-docx builder: title page, headings, bullets, tables)
- `app/agent.py` — planner, executor dispatch, reflector, bounded-retry orchestration
- `app/main.py` — FastAPI routes: `POST /agent`, `GET /agent/download/{filename}`, `GET /health`

**Why this shape:**
- The plan is LLM-generated but **schema-constrained** (JSON mode + Pydantic validation), not parsed from free-form ReAct text. Autonomy in *what* gets planned; determinism in *how* it's structured and logged.
- Reflection is a **separate LLM call that only sees the draft output**, not the planner's reasoning — so it can't just rubber-stamp its own logic.
- Retry is **bounded to one pass**. This is a deliberate production concern, not a shortcut: unbounded self-correction loops are a classic agent failure mode (cost/latency blow up with no guaranteed convergence).

---

## 3. The mandatory improvement: Reflection / Self-Check with Bounded Retry

**What:** After the executor drafts all sections, a second LLM call (`reflect()`) compares the draft against the *original* request and flags concrete, fixable gaps. If it finds one, the agent runs exactly one additional `draft_section` step to patch it, then regenerates the docx.

**Why this over tool-calling/RAG/memory:** For a document-generation agent, the failure mode that actually costs you in production isn't "couldn't call an API" — it's "produced a plausible-looking document that quietly missed something the user asked for." Reflection targets that failure mode directly. Tool-calling and RAG add capability; reflection adds a correctness check on output the agent already has full information to self-verify.

**How it improves the agent:** In the complex/ambiguous test case (see below), the reflector caught that a banking-related report was missing any compliance/security mention — something the planner's initial section list didn't surface — and the retry added an "Additional Considerations" section addressing it, without needing a second full run.

---

## 4. Two test inputs

**Standard:** `"Create a project plan for launching a mobile banking app for a mid-size retail bank."`
Clean happy path — planner produces gather_info + draft_section steps, no ambiguity, reflection typically passes without retry.

**Complex/ambiguous:** `"Write a business report for our client about our Q3 performance — it needs to be detailed but I don't have the final numbers yet, and the client also asked for it to cover both technical progress and budget in the same document."`
Forces the agent to: (a) notice missing data and record an explicit **assumption** rather than asking the user, (b) merge two conflicting structural asks (technical + budget) into one coherent plan, (c) let reflection catch anything the planner under-scoped.

---

## 5. Debugging insight (real issue you can describe on camera)

**Issue:** Early on, `_call_llm_json` intermittently threw `json.JSONDecodeError` even with `response_format={"type": "json_object"}` set.

**Root cause:** The planner prompt didn't pin down the exact key names strongly enough, so occasionally the model wrapped valid JSON in a markdown code fence (```json ... ```) despite JSON mode — an artifact of the fine-tuning behind JSON mode, not a hard guarantee.

**Fix:** Added a defensive second-pass parse in `_call_llm_json` that strips wrapping fences and retries `json.loads` once before failing. This is the kind of fix that matters more than it looks — it's the difference between "works in the demo" and "works when the model has an off day."

---

## 6. Tradeoff discussion

**Autonomous planning vs. deterministic workflow.**
I let the LLM generate the plan freely (autonomy), but constrain its *shape* with a JSON schema and force the last step to always be `generate_docx` (determinism). Pure LLM autonomy (no schema) would be more flexible for edge-case requests but far less reliable to execute and debug under a time constraint. Pure determinism (a fixed template per doc type) would be more reliable but wouldn't demonstrate actual planning/reasoning — which is the point of the exercise. The schema-constrained middle ground gets both: the agent still *decides* how many sections, what data to gather, and what assumptions to make, but the executor never receives a step shape it can't handle.

The cost of this tradeoff: the agent can't invent a genuinely novel action type outside `{gather_info, draft_section, generate_docx}` — extensibility requires adding a new action to both the prompt and the executor's dispatch table. That's an acceptable ceiling for a 60-minute build; a production version would likely register tools dynamically instead of hardcoding the dispatch `if/elif`.

---

## 7. Scaling this beyond the assignment

- Swap `mock_data_lookup` for real connectors (CRM, finance system) — the executor interface doesn't change.
- Swap the flat `if/elif` dispatch in `execute_step` for a registered tool schema (already stubbed as `TOOL_REGISTRY` in `tools.py`) if the action set grows.
- Add conversation memory by persisting `context` per session ID if this needs to support follow-up refinement requests ("now make it more formal") rather than one-shot generation.
