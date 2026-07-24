"""
Enfrasys Pre-Sales Agent - Blob Download Service
Simple FastAPI app to download opportunity documents from Azure Blob Storage.
"""

import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest, DocumentContentFormat
from azure.core.credentials import AzureKeyCredential
from dotenv import load_dotenv
import io

# Load environment variables from .env file
load_dotenv()

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------
AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER_NAME = os.getenv("CONTAINER_NAME", "dev-opportunity-documents")
DOC_INTELLIGENCE_ENDPOINT = os.getenv("DOC_INTELLIGENCE_ENDPOINT")
DOC_INTELLIGENCE_KEY = os.getenv("DOC_INTELLIGENCE_KEY")

if not AZURE_STORAGE_CONNECTION_STRING:
    raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is not set in .env")

if not DOC_INTELLIGENCE_ENDPOINT or not DOC_INTELLIGENCE_KEY:
    raise RuntimeError("Document Intelligence credentials are not set in .env")

# Initialize Blob Service Client once at startup
blob_service_client = BlobServiceClient.from_connection_string(
    AZURE_STORAGE_CONNECTION_STRING
)

# Initialize Document Intelligence once at startup
doc_intelligence_client = DocumentIntelligenceClient(
    endpoint=DOC_INTELLIGENCE_ENDPOINT,
    credential=AzureKeyCredential(DOC_INTELLIGENCE_KEY),
)

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
