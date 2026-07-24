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
# Extract document content using Document Intelligence Layout
# ---------------------------------------------------------------------
@app.get("/extract-document/{opportunity_id}/{file_name}")
def extract_document(opportunity_id: str, file_name: str):
    """
    Download document from Blob → send to Document Intelligence Layout →
    return extracted markdown content ready for Foundry Agent.

    Uses prebuilt-layout model with markdown output for LLM-friendly results.
    """
    blob_path = f"{opportunity_id}/{file_name}"

    try:
        # Step 1: Download blob from storage
        blob_client = blob_service_client.get_blob_client(
            container=CONTAINER_NAME,
            blob=blob_path,
        )
        blob_data = blob_client.download_blob().readall()

        # Step 2: Send to Document Intelligence (Layout model, Markdown output)
        poller = doc_intelligence_client.begin_analyze_document(
            model_id="prebuilt-layout",
            body=AnalyzeDocumentRequest(bytes_source=blob_data),
            output_content_format=DocumentContentFormat.MARKDOWN,
        )
        result = poller.result()

        # Step 3: Build response
        return {
            "opportunity_id": opportunity_id,
            "file_name": file_name,
            "page_count": len(result.pages) if result.pages else 0,
            "table_count": len(result.tables) if result.tables else 0,
            "markdown_content": result.content,
        }

    except ResourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Document not found: {blob_path}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error extracting document: {str(e)}",
        )


# ---------------------------------------------------------------------
# Request body model for /analyze-opportunity
# ---------------------------------------------------------------------
class AnalyzeRequest(BaseModel):
    opportunity_id: str
    file_name: str
    markdown_content: str

# Simple in-memory job store (fine for dev/testing).
# For production, replace with Cosmos DB or Azure Table Storage.
JOBS: dict = {}


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
        Source File: {payload.file_name}

        ===== EXTRACTED DOCUMENT CONTENT =====

        {payload.markdown_content}

        ===== END OF DOCUMENT =====
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
# Helper: Map file extension to MIME type
# ---------------------------------------------------------------------
def _get_content_type(file_name: str) -> str:
    ext = file_name.lower().rsplit(".", 1)[-1] if "." in file_name else ""
    mapping = {
        "pdf": "application/pdf",
        "zip": "application/zip",
        "json": "application/json",
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
