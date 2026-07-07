"""
Agent core: Planner -> Executor -> Reflector.
"""
import os
import json
import logging
from groq import Groq
from dotenv import load_dotenv

from app.tools import mock_data_lookup, generate_docx
from app.schemas import TaskStep, ReflectionResult

load_dotenv()
logger = logging.getLogger("agent")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None


PLANNER_SYSTEM_PROMPT = """You are the planning module of an autonomous document-generation agent.

Given a user's natural language request, produce a JSON execution plan: a list of steps.
Each step has this exact shape:
{"id": <int>, "action": "<gather_info|draft_section|generate_docx>", "description": "<string>", "inputs": {<object>}}

Rules:
- Use "gather_info" for any step that needs supporting data (budget, risks, stakeholders, timelines, etc).
  For these steps, inputs must include {"topic": "<short topic string>"}.
- Use "draft_section" for each section of the final document that needs to be written.
  For these steps, inputs must include {"heading": "<section title>"}.
- The LAST step must always be {"action": "generate_docx", "inputs": {"doc_type": "<Proposal|Meeting Minutes|Project Plan|Business Report|Technical Design|SOP|Product Specification>", "title": "<document title>"}}.
- If the request is ambiguous or missing information, do NOT ask the user questions.
  Instead, make and record ONE reasonable assumption per gap, and add a "gather_info" step to
  fill it with a sensible default. List assumptions in an extra top-level field "assumptions".
- Output ONLY valid JSON: {"steps": [...], "assumptions": [...]}. No prose, no markdown fences.
"""

DRAFTER_SYSTEM_PROMPT = """You are the content-drafting module of a document agent.
Given a section heading, the original user request, and any supporting mock data,
write the section content for a professional business document.

Output ONLY valid JSON in this shape:
{"heading": "<same heading>", "body": "<1-3 sentence paragraph>", "bullets": ["<point>", ...] }
Keep it concrete and specific to the request — do not write generic filler.
"""

REFLECTOR_SYSTEM_PROMPT = """You are a quality-control reviewer for an AI document-generation agent.
You will be given the ORIGINAL user request and the DRAFT section contents that were produced.
Check: (1) does the draft actually address what was asked, (2) are there obvious gaps or
missing sections a professional document of this type would need, (3) is anything contradictory.

Output ONLY valid JSON: {"needs_retry": <bool>, "issues": ["<issue>", ...], "notes": "<short string>"}
Only set needs_retry=true if there is a real, specific, fixable gap. Do not nitpick style.
"""


def _call_llm_json(system_prompt: str, user_content: str, temperature: float = 0.3) -> dict:
    """Wraps a Groq chat completion call, enforcing JSON output with a defensive parse."""
    if client is None:
        raise RuntimeError(
            "GROQ_API_KEY not set. Get a free key at https://console.groq.com/keys "
            "and put it in a .env file (see .env.example)."
        )
    resp = client.chat.completions.create(
        model=MODEL,
        temperature=temperature,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    raw = resp.choices[0].message.content
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Defensive fallback: strip common wrapping artifacts and retry parse once.
        cleaned = raw.strip().strip("```").replace("json\n", "", 1)
        return json.loads(cleaned)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------
def create_plan(user_request: str) -> tuple[list[TaskStep], list[str]]:
    result = _call_llm_json(PLANNER_SYSTEM_PROMPT, user_request, temperature=0.2)
    steps_raw = result.get("steps", [])
    assumptions = result.get("assumptions", [])
    steps = [TaskStep(**s) for s in steps_raw]
    return steps, assumptions


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------
def execute_step(step: TaskStep, user_request: str, context: dict) -> dict:
    if step.action == "gather_info":
        topic = step.inputs.get("topic", step.description)
        data = mock_data_lookup(topic)
        context.setdefault("gathered_data", {})[topic] = data
        return {"type": "gather_info", "topic": topic, "data": data}

    elif step.action == "draft_section":
        heading = step.inputs.get("heading", step.description)
        supporting = json.dumps(context.get("gathered_data", {}))
        user_content = (
            f"ORIGINAL REQUEST: {user_request}\n"
            f"SECTION HEADING: {heading}\n"
            f"SUPPORTING DATA: {supporting}"
        )
        section = _call_llm_json(DRAFTER_SYSTEM_PROMPT, user_content, temperature=0.6)
        context.setdefault("sections", []).append(section)
        return {"type": "draft_section", "section": section}

    elif step.action == "generate_docx":
        doc_type = step.inputs.get("doc_type", "Business Report")
        title = step.inputs.get("title", "Untitled Document")
        sections = context.get("sections", [])
        filepath = generate_docx(title=title, doc_type=doc_type, sections=sections)
        context["docx_path"] = filepath
        context["doc_type"] = doc_type
        return {"type": "generate_docx", "path": filepath}

    else:
        return {"type": "unknown_action", "action": step.action}


# ---------------------------------------------------------------------------
# Reflector
# ---------------------------------------------------------------------------
def reflect(user_request: str, context: dict) -> ReflectionResult:
    draft_summary = json.dumps(context.get("sections", []))
    user_content = f"ORIGINAL REQUEST: {user_request}\nDRAFT SECTIONS: {draft_summary}"
    result = _call_llm_json(REFLECTOR_SYSTEM_PROMPT, user_content, temperature=0.1)
    return ReflectionResult(**result)


def add_recovery_section(context: dict, issues: list[str], user_request: str) -> None:
    """One bounded retry: draft a single extra section addressing flagged issues."""
    heading = "Additional Considerations"
    user_content = (
        f"ORIGINAL REQUEST: {user_request}\n"
        f"SECTION HEADING: {heading}\n"
        f"The reviewer flagged these gaps to address: {json.dumps(issues)}\n"
        f"SUPPORTING DATA: {json.dumps(context.get('gathered_data', {}))}"
    )
    section = _call_llm_json(DRAFTER_SYSTEM_PROMPT, user_content, temperature=0.5)
    context.setdefault("sections", []).append(section)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_agent(user_request: str) -> dict:
    context: dict = {}

    plan, assumptions = create_plan(user_request)
    logger.info("Plan created with %d steps", len(plan))

    for step in plan:
        execute_step(step, user_request, context)

    reflection = reflect(user_request, context)
    retried = False

    if reflection.needs_retry and reflection.issues:
        logger.info("Reflection flagged issues, running one bounded retry: %s", reflection.issues)
        add_recovery_section(context, reflection.issues, user_request)
        # Overwrite the SAME file rather than minting a new one
        filepath = generate_docx(
            title=next((s.inputs.get("title") for s in plan if s.action == "generate_docx"), "Document"),
            doc_type=context.get("doc_type", "Business Report"),
            sections=context.get("sections", []),
            filepath=context["docx_path"],
        )
        context["docx_path"] = filepath
        retried = True

    return {
        "plan": plan,
        "assumptions": assumptions,
        "reflection": reflection,
        "retried": retried,
        "docx_path": context["docx_path"],
    }
