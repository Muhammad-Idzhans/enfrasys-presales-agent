"""
Enfrasys Pre-Sales Agent - Blob Download Service
Simple FastAPI app to download opportunity documents from Azure Blob Storage.
"""

import os
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest, DocumentContentFormat
from azure.core.credentials import AzureKeyCredential
from dotenv import load_dotenv
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from pydantic import BaseModel
from jinja2 import Template
from typing import List

import io
import time
import json
import uuid

# Load environment variables from .env file
load_dotenv()

# Blob Storage Configuration
AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER_NAME = os.getenv("CONTAINER_NAME", "dev-opportunity-documents")

# Document Intelligence Configuration
DOC_INTELLIGENCE_ENDPOINT = os.getenv("DOC_INTELLIGENCE_ENDPOINT")
DOC_INTELLIGENCE_KEY = os.getenv("DOC_INTELLIGENCE_KEY")

# Microsoft Foundry Agentic Configuration
FOUNDRY_PROJECT_ENDPOINT = os.getenv("FOUNDRY_PROJECT_ENDPOINT")
PRESALES_ANALYST_AGENT = os.getenv("PRESALES_ANALYST_AGENT")
HTML_JSON_FORMATTER_AGENT = os.getenv("HTML_JSON_FORMATTER_AGENT")


# Azure Blob Storage Validation
if not AZURE_STORAGE_CONNECTION_STRING:
    raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is not set in .env")

# Document Intelligence Validation
if not DOC_INTELLIGENCE_ENDPOINT or not DOC_INTELLIGENCE_KEY:
    raise RuntimeError("Document Intelligence credentials are not set in .env")

# Microsoft Foundry Agentic Validation
if not all([FOUNDRY_PROJECT_ENDPOINT, PRESALES_ANALYST_AGENT, HTML_JSON_FORMATTER_AGENT]):
    raise RuntimeError("Foundry endpoint or Agent names are not set in .env")

# Initialize Blob Service Client once at startup
blob_service_client = BlobServiceClient.from_connection_string(
    AZURE_STORAGE_CONNECTION_STRING
)

# Initialize Document Intelligence once at startup
doc_intelligence_client = DocumentIntelligenceClient(
    endpoint=DOC_INTELLIGENCE_ENDPOINT,
    credential=AzureKeyCredential(DOC_INTELLIGENCE_KEY),
)

# Initialize Foundry project client once at startup
project_client = AIProjectClient(
    endpoint=FOUNDRY_PROJECT_ENDPOINT,
    credential=DefaultAzureCredential(),
)

# Get the OpenAI-compatible client (new Foundry Responses API)
openai_client = project_client.get_openai_client()

# ---------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------
app = FastAPI(
    title="Enfrasys Pre-Sales Agent - Blob Service",
    description="Download opportunity documents from Azure Blob Storage",
    version="0.1.0",
)


@app.get("/")
def root():
    """Health check endpoint."""
    return {
        "service": "Enfrasys Pre-Sales Agent",
        "status": "running",
        "container": CONTAINER_NAME,
    }


@app.get("/health")
def health():
    """Simple health check."""
    return {"status": "healthy"}


# ---------------------------------------------------------------------
# Main endpoint: Download a document from blob storage
# ---------------------------------------------------------------------
@app.get("/download-document/{opportunity_id}/{file_name}")
def download_document(opportunity_id: str, file_name: str):
    """
    Download a document from Azure Blob Storage.

    Blob path convention: {opportunity_id}/{file_name}
    Example: 56/tender.pdf  →  blob at 'opportunity-inputs/56/tender.pdf'

    Returns the file as a streaming download.
    """
    blob_path = f"{opportunity_id}/{file_name}"

    try:
        # Get blob client
        blob_client = blob_service_client.get_blob_client(
            container=CONTAINER_NAME,
            blob=blob_path,
        )

        # Download blob content into memory
        blob_data = blob_client.download_blob()
        content = blob_data.readall()

        # Determine content type based on file extension
        content_type = _get_content_type(file_name)

        # Return as streaming response so Postman can save/preview it
        return StreamingResponse(
            io.BytesIO(content),
            media_type=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{file_name}"',
                "X-Opportunity-Id": opportunity_id,
                "X-Blob-Path": blob_path,
            },
        )

    except ResourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Document not found: {blob_path}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error downloading document: {str(e)}",
        )


# ---------------------------------------------------------------------
# Bonus endpoint: Get blob metadata without downloading
# ---------------------------------------------------------------------
@app.get("/document-info/{opportunity_id}/{file_name}")
def get_document_info(opportunity_id: str, file_name: str):
    """
    Retrieve blob metadata (size, content type, last modified) without downloading.
    Useful for verifying a blob exists before triggering full processing.
    """
    blob_path = f"{opportunity_id}/{file_name}"

    try:
        blob_client = blob_service_client.get_blob_client(
            container=CONTAINER_NAME,
            blob=blob_path,
        )
        props = blob_client.get_blob_properties()

        return {
            "opportunity_id": opportunity_id,
            "file_name": file_name,
            "blob_path": blob_path,
            "size_bytes": props.size,
            "content_type": props.content_settings.content_type,
            "last_modified": props.last_modified.isoformat(),
            "etag": props.etag,
        }

    except ResourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Document not found: {blob_path}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error retrieving document info: {str(e)}",
        )


# ---------------------------------------------------------------------
# Bonus endpoint: List documents for an opportunity
# ---------------------------------------------------------------------
@app.get("/list-documents/{opportunity_id}")
def list_documents(opportunity_id: str):
    """List all files/blobs under a given opportunity folder."""
    try:
        container_client = blob_service_client.get_container_client(CONTAINER_NAME)
        blobs = container_client.list_blobs(name_starts_with=f"{opportunity_id}/")

        files = [
            {
                "name": blob.name.replace(f"{opportunity_id}/", "", 1),
                "size_bytes": blob.size,
                "last_modified": blob.last_modified.isoformat(),
            }
            for blob in blobs
        ]

        return {
            "opportunity_id": opportunity_id,
            "file_count": len(files),
            "files": files,
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error listing documents: {str(e)}",
        )


# ---------------------------------------------------------------------
# Helper: list all file names under an opportunity folder
# ---------------------------------------------------------------------
def _list_opportunity_files(opportunity_id: str) -> List:
    """Return file names (without the folder prefix) inside an opportunity folder."""
    container_client = blob_service_client.get_container_client(CONTAINER_NAME)
    blobs = container_client.list_blobs(name_starts_with=f"{opportunity_id}/")
    return [
        b.name.replace(f"{opportunity_id}/", "", 1)
        for b in blobs
        if not b.name.endswith("/")          # skip folder placeholders
    ]


# ---------------------------------------------------------------------
# Helper: extract ONE blob via Document Intelligence (in-memory)
# ---------------------------------------------------------------------
def _extract_single(opportunity_id: str, file_name: str) -> dict:
    """Download one blob into memory and run Document Intelligence Layout."""
    blob_path = f"{opportunity_id}/{file_name}"
    blob_client = blob_service_client.get_blob_client(
        container=CONTAINER_NAME,
        blob=blob_path,
    )
    blob_data = blob_client.download_blob().readall()

    poller = doc_intelligence_client.begin_analyze_document(
        model_id="prebuilt-layout",
        body=AnalyzeDocumentRequest(bytes_source=blob_data),
        output_content_format=DocumentContentFormat.MARKDOWN,
    )
    result = poller.result()

    return {
        "page_count": len(result.pages) if result.pages else 0,
        "table_count": len(result.tables) if result.tables else 0,
        "markdown": result.content,
    }


# ---------------------------------------------------------------------
# DEBUG ONLY: extract a single document (not used by Power Automate)
# ---------------------------------------------------------------------
@app.get("/extract-single/{opportunity_id}/{file_name}")
def extract_single_document(opportunity_id: str, file_name: str):
    """Extract one document. Useful for inspecting DI output for one file."""
    try:
        extracted = _extract_single(opportunity_id, file_name)
        return {
            "opportunity_id": opportunity_id,
            "file_name": file_name,
            "page_count": extracted["page_count"],
            "table_count": extracted["table_count"],
            "markdown_length": len(extracted["markdown"]),
            "markdown_content": extracted["markdown"],
        }
    except ResourceNotFoundError:
        raise HTTPException(status_code=404, detail=f"Document not found: {opportunity_id}/{file_name}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error extracting document: {str(e)}")


# ---------------------------------------------------------------------
# Background worker: extract every document in the opportunity folder
# ---------------------------------------------------------------------
def process_extraction_job(job_id: str, opportunity_id: str):
    try:
        EXTRACT_JOBS[job_id]["status"] = "extracting"

        file_names = _list_opportunity_files(opportunity_id)
        if not file_names:
            raise RuntimeError(f"No documents found in folder: {opportunity_id}")

        EXTRACT_JOBS[job_id]["file_names"] = file_names
        EXTRACT_JOBS[job_id]["file_count"] = len(file_names)

        parts, total_pages, total_tables = [], 0, 0

        for idx, fname in enumerate(file_names, start=1):
            EXTRACT_JOBS[job_id]["current_file"] = fname
            EXTRACT_JOBS[job_id]["files_completed"] = idx - 1

            extracted = _extract_single(opportunity_id, fname)
            total_pages += extracted["page_count"]
            total_tables += extracted["table_count"]

            parts.append(
                f"===== DOCUMENT {idx} OF {len(file_names)}: {fname} =====\n\n"
                f"{extracted['markdown']}\n\n"
                f"===== END OF DOCUMENT {idx}: {fname} ====="
            )

        EXTRACT_JOBS[job_id].update({
            "status": "completed",
            "current_file": None,
            "files_completed": len(file_names),
            "page_count": total_pages,
            "table_count": total_tables,
            "markdown_content": "\n\n".join(parts),
        })

    except Exception as e:
        EXTRACT_JOBS[job_id]["status"] = "failed"
        EXTRACT_JOBS[job_id]["error"] = f"Extraction error: {str(e)}"


# ---------------------------------------------------------------------
# EXTRACT STEP A: start extraction for the whole opportunity folder
# ---------------------------------------------------------------------
@app.post("/extract-document/{opportunity_id}")
def start_extraction(opportunity_id: str, background_tasks: BackgroundTasks):
    """Extract every document in the folder. Returns a job_id immediately."""
    job_id = str(uuid.uuid4())
    EXTRACT_JOBS[job_id] = {
        "status": "queued",
        "opportunity_id": opportunity_id,
        "file_names": [],
        "file_count": 0,
        "files_completed": 0,
        "current_file": None,
        "page_count": 0,
        "table_count": 0,
        "markdown_content": None,
        "error": None,
    }
    background_tasks.add_task(process_extraction_job, job_id, opportunity_id)
    return {"job_id": job_id, "status": "queued", "opportunity_id": opportunity_id}

# ---------------------------------------------------------------------
# EXTRACT STEP B: poll extraction progress
# ---------------------------------------------------------------------
@app.get("/extract-document/{job_id}/status")
def get_extraction_status(job_id: str):
    """Return progress of an extraction job."""
    job = EXTRACT_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Extraction job not found: {job_id}")
    return {
        "job_id": job_id,
        "status": job["status"], # queued | extracting | completed | failed
        "opportunity_id": job["opportunity_id"],
        "file_count": job["file_count"],
        "files_completed": job["files_completed"],
        "current_file": job["current_file"],
        "error": job["error"],
    }


# ---------------------------------------------------------------------
# EXTRACT STEP C: fetch combined markdown once completed
# ---------------------------------------------------------------------
@app.get("/extract-document/{job_id}/result")
def get_extraction_result(job_id: str):
    """Return the combined markdown for all documents in the folder."""
    job = EXTRACT_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Extraction job not found: {job_id}")
    if job["status"] == "failed":
        raise HTTPException(status_code=500, detail=job["error"])
    if job["status"] != "completed":
        raise HTTPException(status_code=409, detail=f"Not ready. Current status: {job['status']}")
    return {
        "job_id": job_id,
        "opportunity_id": job["opportunity_id"],
        "file_names": job["file_names"],
        "file_count": job["file_count"],
        "page_count": job["page_count"],
        "table_count": job["table_count"],
        "markdown_length": len(job["markdown_content"] or ""),
        "markdown_content": job["markdown_content"],
    }


# ---------------------------------------------------------------------
# Request body model for /analyze-opportunity
# ---------------------------------------------------------------------
class AnalyzeRequest(BaseModel):
    opportunity_id: str
    file_names: List[str] = []
    markdown_content: str

# Simple in-memory job store (fine for dev/testing).
# For production, replace with Cosmos DB or Azure Table Storage.
JOBS: dict = {}
# Separate job store for folder-based extraction jobs.
EXTRACT_JOBS: dict = {}


# ---------------------------------------------------------------------
# Helper: Run a Foundry agent and wait for its text response
# ---------------------------------------------------------------------
def run_agent(agent_ref: str, user_message: str, timeout_seconds: int = 420) -> str:
    """
    Call a published Foundry agent using the DDG-proven pattern:
    agent_ref format is 'agent-name:version' (e.g. 'presales-analyst-agent:4').
    """
    parts = agent_ref.split(":")
    agent_name = parts[0]
    agent_version = parts[1] if len(parts) > 1 else "1"

    response = openai_client.responses.create(
        input=[
            {"type": "message", "role": "user", "content": user_message}
        ],
        extra_body={
            "agent_reference": {
                "name": agent_name,
                "version": agent_version,
                "type": "agent_reference"
            }
        }
    )
    return response.output_text


# ---------------------------------------------------------------------
# Background worker: runs the two-agent pipeline
# ---------------------------------------------------------------------
def process_analysis_job(job_id: str, payload: AnalyzeRequest):
    try:
        JOBS[job_id]["status"] = "analyzing"

        # ---- Agent 1: Presales Analyst ----
        presalesAnalystAgent_input = f"""
        Analyze the following tender/RFP content and produce your full 5-step presales assessment. Ground all Microsoft claims with web search.

        Opportunity ID: {payload.opportunity_id}
        Source Files ({len(payload.file_names)}): {", ".join(payload.file_names)}

        NOTE: The content below may contain MULTIPLE documents, each wrapped in
        DOCUMENT markers. Treat them as ONE tender package and produce a SINGLE
        combined assessment.

        ===== EXTRACTED DOCUMENT CONTENT =====

        {payload.markdown_content}

        ===== END OF EXTRACTED CONTENT =====
        """
        presalesAnalystAgent_output = run_agent(PRESALES_ANALYST_AGENT, presalesAnalystAgent_input)

        JOBS[job_id]["status"] = "formatting"

        # ---- Agent 2: JSON Formatter ----
        htmlJsonFormatterAgent_input = f"""
        Convert the following presales analysis into the required JSON output.

        Opportunity ID: {payload.opportunity_id}

        ===== PRESALES ANALYSIS =====

        {presalesAnalystAgent_output}

        ===== END OF ANALYSIS =====
        """
        htmlJsonFormatterAgent_output = run_agent(HTML_JSON_FORMATTER_AGENT, htmlJsonFormatterAgent_input)

        # ---- Parse Agent 2 JSON (strip code fences if any) ----
        cleaned = htmlJsonFormatterAgent_output.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()

        email_json = json.loads(cleaned)

        JOBS[job_id]["status"] = "completed"
        JOBS[job_id]["email_json"] = email_json

    except json.JSONDecodeError as e:
        JOBS[job_id]["status"] = "failed"
        JOBS[job_id]["error"] = f"Agent 2 returned invalid JSON: {str(e)}"
    except Exception as e:
        JOBS[job_id]["status"] = "failed"
        JOBS[job_id]["error"] = f"Pipeline error: {str(e)}"


# ---------------------------------------------------------------------
# STEP A: Kick off the job — returns instantly with a job_id
# ---------------------------------------------------------------------
@app.post("/analyze-opportunity")
def start_analysis(payload: AnalyzeRequest, background_tasks: BackgroundTasks):
    """Start the analysis in the background. Returns a job_id immediately."""
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "status": "queued",
        "opportunity_id": payload.opportunity_id,
        "email_json": None,
        "error": None,
    }
    background_tasks.add_task(process_analysis_job, job_id, payload)
    return {"job_id": job_id, "status": "queued"}


# ---------------------------------------------------------------------
# STEP B: Poll this to check progress (Power Automate polls every ~15s)
# ---------------------------------------------------------------------
@app.get("/analyze-opportunity/{job_id}/status")
def get_analysis_status(job_id: str):
    """Return the current status of an analysis job."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return {
        "job_id": job_id,
        "status": job["status"],   # queued | analyzing | formatting | completed | failed
        "opportunity_id": job["opportunity_id"],
        "error": job["error"],
    }


# ---------------------------------------------------------------------
# STEP C: Fetch the final result once status == completed
# ---------------------------------------------------------------------
@app.get("/analyze-opportunity/{job_id}/result")
def get_analysis_result(job_id: str):
    """Return the final email_json once the job is completed."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    if job["status"] == "failed":
        raise HTTPException(status_code=500, detail=job["error"])
    if job["status"] != "completed":
        raise HTTPException(status_code=409, detail=f"Job not ready. Current status: {job['status']}")
    return {
        "job_id": job_id,
        "status": "completed",
        "opportunity_id": job["opportunity_id"],
        "email_json": job["email_json"],
    }


# ---------------------------------------------------------------------
# Request body model for /convert-html
# ---------------------------------------------------------------------
class ConvertHtmlRequest(BaseModel):
    email_json: dict
    file_name: str = "Not specified"


# ---------------------------------------------------------------------
# STEP D: Convert email_json → final email envelope (subject + HTML body)
# ---------------------------------------------------------------------
@app.post("/convert-html")
def convert_html(payload: ConvertHtmlRequest):
    """
    Takes the email_json (from /analyze-opportunity result) and returns the
    final email envelope: opportunity_id, file_name, email_subject, email_body (HTML).
    """
    try:
        return build_email_payload(payload.email_json, payload.file_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"HTML conversion error: {str(e)}")


# ---------------------------------------------------------------------
# Email HTML Template (Jinja2) — edit the design here
# ---------------------------------------------------------------------
EMAIL_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="margin:0; padding:0; background-color:#f4f5f7; font-family:'Segoe UI', Arial, sans-serif; color:#1f2933;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f5f7; padding:24px 0;">
<tr><td align="center">
  <table role="presentation" width="680" cellpadding="0" cellspacing="0" style="background-color:#ffffff; border-radius:10px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.08);">

    <tr><td style="background-color:#0f4c81; padding:24px 32px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
        <td style="color:#ffffff; font-size:20px; font-weight:700;">Enfrasys Presales Assessment</td>
        <td align="right" style="color:#cfe0f1; font-size:13px;">Opportunity #{{ opportunity_id[:8] }}</td>
      </tr></table>
    </td></tr>

    <tr><td style="background-color:{{ verdict_hex }}; padding:16px 32px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
        <td style="color:#ffffff; font-size:18px; font-weight:700;">Verdict: {{ verdict }}</td>
        <td align="right" style="color:#ffffff; font-size:13px; font-weight:600;">Confidence: {{ confidence_level }}</td>
      </tr></table>
    </td></tr>

    <tr><td style="padding:24px 32px 8px 32px;">
      <h1 style="margin:0 0 6px 0; font-size:19px; color:#0f4c81;">{{ opportunity_title }}</h1>
      <p style="margin:0; font-size:14px; color:#52606d;"><strong>{{ client_name }}</strong></p>
      <p style="margin:4px 0 0 0; font-size:13px; color:#52606d;">{{ industry }} &nbsp;|&nbsp; {{ segment }}</p>
      <p style="margin:4px 0 0 0; font-size:12px; color:#9aa5b1;">Submitted: {{ submitted_date }}</p>
    </td></tr>

    <tr><td style="padding:16px 32px 8px 32px;">
      <h2 style="margin:0 0 8px 0; font-size:13px; text-transform:uppercase; letter-spacing:0.5px; color:#0f4c81;">Executive Summary</h2>
      <p style="margin:0; font-size:14px; line-height:1.6;">{{ executive_summary }}</p>
    </td></tr>

    <tr><td style="padding:8px 32px 16px 32px;">
      <h2 style="margin:0 0 8px 0; font-size:13px; text-transform:uppercase; letter-spacing:0.5px; color:#0f4c81;">Feasibility</h2>
      <p style="margin:0; font-size:14px; line-height:1.6;">{{ feasibility_summary }}</p>
    </td></tr>

    <tr><td style="padding:8px 32px 16px 32px;">
      <h2 style="margin:0 0 8px 0; font-size:13px; text-transform:uppercase; letter-spacing:0.5px; color:#0f4c81;">Recommended Tech Stack</h2>
      <p style="margin:0 0 6px 0; font-size:13px; font-weight:600; color:#334e68;">Microsoft-first:</p>
      <ul style="margin:0 0 12px 18px; padding:0; font-size:14px; line-height:1.7;">
        {% for item in recommended_stack_microsoft %}<li>{{ item }}</li>{% endfor %}
      </ul>
      <p style="margin:0 0 6px 0; font-size:13px; font-weight:600; color:#334e68;">External:</p>
      {% if recommended_stack_external %}
      <ul style="margin:0 0 0 18px; padding:0; font-size:14px; line-height:1.7;">
        {% for item in recommended_stack_external %}<li>{{ item }}</li>{% endfor %}
      </ul>
      {% else %}<p style="margin:0; font-size:14px; color:#52606d;">None required.</p>{% endif %}
    </td></tr>

    <tr><td style="padding:8px 32px 16px 32px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0f4f8; border-radius:6px;"><tr>
        <td style="padding:12px 16px; font-size:14px; line-height:1.5;">
          <strong>Partnership required:</strong> {{ partnership_required }}<br>
          <span style="color:#52606d;">{{ partnership_details }}</span>
        </td>
      </tr></table>
    </td></tr>

    <tr><td style="padding:8px 32px 16px 32px;">
      <h2 style="margin:0 0 8px 0; font-size:13px; text-transform:uppercase; letter-spacing:0.5px; color:#0f4c81;">Estimated Cost</h2>
      <p style="margin:0 0 10px 0; font-size:16px; font-weight:700; color:#0f4c81;">{{ estimated_cost_range_myr }}</p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse; font-size:13px;">
        <tr style="background-color:#0f4c81; color:#ffffff;">
          <td style="padding:8px 12px;">Category</td><td style="padding:8px 12px;">Amount (RM)</td><td style="padding:8px 12px;">Notes</td>
        </tr>
        {% for row in cost_breakdown %}
        <tr style="border-bottom:1px solid #e4e7eb; {% if loop.index is even %}background-color:#f7f9fb;{% endif %}">
          <td style="padding:8px 12px;">{{ row.category }}</td>
          <td style="padding:8px 12px; white-space:nowrap;">{{ row.amount_myr }}</td>
          <td style="padding:8px 12px; color:#52606d;">{{ row.notes }}</td>
        </tr>
        {% endfor %}
      </table>
    </td></tr>

    <tr><td style="padding:8px 32px 16px 32px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr valign="top">
        <td width="50%" style="padding-right:10px;">
          <h2 style="margin:0 0 8px 0; font-size:13px; text-transform:uppercase; letter-spacing:0.5px; color:#2e7d32;">Reasons to Pursue</h2>
          <ul style="margin:0 0 0 18px; padding:0; font-size:13.5px; line-height:1.6;">
            {% for r in top_reasons_to_pursue %}<li>{{ r }}</li>{% endfor %}
          </ul>
        </td>
        <td width="50%" style="padding-left:10px;">
          <h2 style="margin:0 0 8px 0; font-size:13px; text-transform:uppercase; letter-spacing:0.5px; color:#c62828;">Key Risks</h2>
          <ul style="margin:0 0 0 18px; padding:0; font-size:13.5px; line-height:1.6;">
            {% for r in top_risks %}<li>{{ r }}</li>{% endfor %}
          </ul>
        </td>
      </tr></table>
    </td></tr>

    <tr><td style="padding:8px 32px 16px 32px;">
      <h2 style="margin:0 0 8px 0; font-size:13px; text-transform:uppercase; letter-spacing:0.5px; color:#0f4c81;">Next Steps</h2>
      <ol style="margin:0 0 0 18px; padding:0; font-size:14px; line-height:1.7;">
        {% for step in next_steps %}<li>{{ step }}</li>{% endfor %}
      </ol>
    </td></tr>

    <tr><td style="padding:8px 32px 24px 32px;">
      <h2 style="margin:0 0 8px 0; font-size:12px; text-transform:uppercase; letter-spacing:0.5px; color:#9aa5b1;">References</h2>
      <ul style="margin:0 0 0 18px; padding:0; font-size:12.5px; line-height:1.6;">
        {% for ref in references %}<li><a href="{{ ref.url }}" style="color:#0f4c81;">{{ ref.title }}</a></li>{% endfor %}
      </ul>
    </td></tr>

    <tr><td style="background-color:#f0f4f8; padding:16px 32px; text-align:center; font-size:11px; color:#9aa5b1;">
      This assessment was generated by the Enfrasys Presales AI Agent for internal review.<br>
      Please validate all figures before client submission.
    </td></tr>

  </table>
</td></tr>
</table>
</body>
</html>"""


# ---------------------------------------------------------------------
# Function 1: Render email_json into full HTML
# ---------------------------------------------------------------------
def render_html(email_json: dict) -> str:
    """Inject the email_json values into the HTML template. Returns full HTML string."""
    # Map verdict_color word -> hex for the banner
    color_map = {"green": "#2e7d32", "amber": "#f9a825", "red": "#c62828"}
    verdict_hex = color_map.get(email_json.get("verdict_color", ""), "#0f4c81")

    # Safe defaults so the template never crashes on a missing key
    safe_data = {
        "opportunity_id": email_json.get("opportunity_id", ""),
        "opportunity_title": email_json.get("opportunity_title", "Presales Assessment"),
        "client_name": email_json.get("client_name", "Not specified"),
        "industry": email_json.get("industry", "Not specified"),
        "segment": email_json.get("segment", "Not specified"),
        "submitted_date": email_json.get("submitted_date", "Not specified"),
        "verdict": email_json.get("verdict", "N/A"),
        "confidence_level": email_json.get("confidence_level", "N/A"),
        "executive_summary": email_json.get("executive_summary", ""),
        "feasibility_summary": email_json.get("feasibility_summary", ""),
        "recommended_stack_microsoft": email_json.get("recommended_stack_microsoft", []),
        "recommended_stack_external": email_json.get("recommended_stack_external", []),
        "partnership_required": email_json.get("partnership_required", "Not specified"),
        "partnership_details": email_json.get("partnership_details", ""),
        "estimated_cost_range_myr": email_json.get("estimated_cost_range_myr", "Not specified"),
        "cost_breakdown": email_json.get("cost_breakdown", []),
        "top_reasons_to_pursue": email_json.get("top_reasons_to_pursue", []),
        "top_risks": email_json.get("top_risks", []),
        "next_steps": email_json.get("next_steps", []),
        "references": email_json.get("references", []),
    }

    template = Template(EMAIL_HTML_TEMPLATE)
    return template.render(verdict_hex=verdict_hex, **safe_data)


# ---------------------------------------------------------------------
# Function 2: Build the final email envelope for Power Automate
# ---------------------------------------------------------------------
def build_email_payload(email_json: dict, file_name: str) -> dict:
    """Wrap the rendered HTML into the final email envelope."""
    html_body = render_html(email_json)
    verdict = email_json.get("verdict", "")
    title = email_json.get("opportunity_title", "Presales Assessment")

    return {
        "opportunity_id": email_json.get("opportunity_id", ""),
        "file_name": file_name,
        "email_subject": f"[AI Presales Review] {title}",
        "email_body": html_body,
    }


# ---------------------------------------------------------------------
# Helper: Map file extension to MIME type
# ---------------------------------------------------------------------
def _get_content_type(file_name: str) -> str:
    ext = file_name.lower().rsplit(".", 1)[-1] if "." in file_name else ""
    mapping = {
        "pdf": "application/pdf",
        "zip": "application/zip",
        "json": "application/json",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xls": "application/vnd.ms-excel",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "tiff": "image/tiff",
        "tif": "image/tiff",
        "bmp": "image/bmp",
        "txt": "text/plain",
    }
    return mapping.get(ext, "application/octet-stream")





# ---------------------------------------------------------------------
# Local run: python app_dev.py
# ---------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app_dev:app", host="0.0.0.0", port=8000, reload=True)
