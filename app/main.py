"""
FastAPI entrypoint.

API design notes (say this on camera):
- POST /agent does the full plan->execute->reflect->generate loop and returns
  metadata + a download URL, rather than streaming the binary file directly.
  This keeps the response JSON-serializable and lets a frontend show the plan
  and reflection notes before the user downloads the doc.
- GET /agent/download/{filename} is a separate, cacheable endpoint for the
  actual binary — standard separation of "operation result" vs "artifact".
"""
import os
import logging
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.schemas import AgentRequest, AgentResponse, ReflectionResult
from app.agent import run_agent
from app.tools import OUTPUT_DIR

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

app = FastAPI(
    title="Autonomous Document Agent",
    description="Plans, executes, reflects, and produces a polished .docx from a natural language request.",
    version="1.0.0",
)


@app.post("/agent", response_model=AgentResponse)
def run_agent_endpoint(payload: AgentRequest):
    try:
        result = run_agent(payload.request)
    except RuntimeError as e:
        # e.g. missing API key — a config error, not a server crash
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.exception("Agent run failed")
        raise HTTPException(status_code=500, detail=f"Agent execution failed: {e}")

    filename = os.path.basename(result["docx_path"])
    plan = result["plan"]

    summary = (
        f"Generated a document covering {len(plan) - 1} planned steps. "
        + ("One gap was found and patched via a reflection retry." if result["retried"]
           else "Reflection found no gaps; no retry needed.")
    )

    return AgentResponse(
        request=payload.request,
        plan=plan,
        assumptions_made=result["assumptions"],
        reflection=result["reflection"],
        retried=result["retried"],
        document_filename=filename,
        download_url=f"/agent/download/{filename}",
        summary=summary,
    )


@app.get("/agent/download/{filename}")
def download_document(filename: str):
    filepath = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        filepath,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
    )
