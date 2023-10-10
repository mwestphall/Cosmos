import os, sys
import tempfile
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Request, APIRouter
from fastapi.logger import logger
from fastapi.responses import FileResponse, Response
from fastapi.middleware.gzip import GZipMiddleware
import uuid
from typing import List
import torch
import asyncio
from db.processing_session_types import CosmosSessionJob
from db.db import SessionLocal, get_job_details, get_cached_job_for_pdf
from scheduler import scheduler
from util.cosmos_output_utils import extract_file_from_job, convert_parquet_to_json, replace_url_suffix, read_job_zip_file
from model.models import *
from healthcheck.annotation_metrics import *
from zipfile import ZipFile
import shutil

prefix_url = os.environ.get('API_PREFIX','/cosmos_service')

app = FastAPI(title="COSMOS Service", docs_url=f"{prefix_url}/docs")

prefix_router = APIRouter(prefix=prefix_url)
app.add_middleware(GZipMiddleware)

queue = asyncio.Queue()
workers : List[asyncio.Task] = None

# Approximate memory consumed by a single cosmos pipeline, used to calculate available
# concurrency

# TODO this only works assuming the cosmos api is the only process using the GPU, which
# is usually not the case
GPU_MEM_PER_WORKER = 4e9 # 4GB
COSMOS_SCRIPT = 'process.py'

# if a task fails due to the GPU being out of memory, wait for a while and then try again
OOM_SLEEP_TIME = 30
OOM_ERROR_EXIT_CODE = 2



@prefix_router.get("/version_info")
def get_version_info():
    """Return the API version and git hash of the running API"""
    return {
        "version": os.environ.get("API_VERSION"),
        "git_hash": os.environ.get("GIT_HASH"),
    }

async def _cosmos_worker(work_queue: asyncio.Queue):
    """
    Cosmos worker process. Continually poll from the work queue for new parameters to the pipeline,
    and run the cosmos pipeline in a separate process. A separate process is necessary to avoid I/O 
    blocking issues in Python's async framework
    """
    while True:
        (job_output_dir, job_id, compress_images) = await work_queue.get()
        proc = await asyncio.create_subprocess_exec(sys.executable, COSMOS_SCRIPT, job_output_dir, job_id, str(compress_images))
        result = await proc.wait()
        queue.task_done()
        
        if result == OOM_ERROR_EXIT_CODE:
            await asyncio.sleep(OOM_SLEEP_TIME)
            await queue.put((job_output_dir, job_id, compress_images))

def _build_process_response(message, job_id, request_url):
    """Return the ID of a created job alongside the URLs that a client can use to query that job's status"""
    return JobCreationResponse(
        message=message,
        job_id=job_id,
        status_endpoint=replace_url_suffix(request_url, f"{job_id}/status"),
        result_endpoint=replace_url_suffix(request_url, f"{job_id}/result")
    )

def _save_request_pdf(job_id: str, pdf: UploadFile):
    """Make a non-temporary directory to store the request PDF and job output.
    Must be cleaned up in a separate job due to asynchronous processing and retrieval
    """
    job_output_dir = f"{tempfile.gettempdir()}/{job_id}"
    os.mkdir(job_output_dir)
    try:
        with open(f"{job_output_dir}/{pdf.filename}", "wb") as f:
            shutil.copyfileobj(pdf.file, f)
    except Exception:
        raise HTTPException("Unable to save PDF for processing")
    finally:
        pdf.file.close()
    
    return job_output_dir

@prefix_router.post("/process/", status_code=202)
async def process_document( 
    request: Request, 
    pdf: UploadFile = File(..., description="The document to process with COSMOS", media_type="application/pdf"), 
    compress_images: bool = Form(True, description="Whether to generate compressed or full-resolution images of extractions"), 
    use_cache: bool = Form(True, description="Whether to reuse cached results for the given PDF, if present")
    ) -> JobCreationResponse:
    """
    Accept a new PDF document for COSMOS processing. Saves the PDF to disk, 
    then adds it to a queue for subsequent processing
    """
    if not pdf.file or not pdf.filename:
        raise HTTPException(status_code=400, detail="Poorly constructed form upload")

    # Check for whether a copy of the cached PDF already exists
    pdf_hash, pdf_len, existing_job_id = get_cached_job_for_pdf(pdf.file)
    if use_cache and existing_job_id is not None:
        return _build_process_response("Existing PDF Processing Job Found", existing_job_id, request.url)

    job_id = str(uuid.uuid4())

    job_output_dir = _save_request_pdf(job_id, pdf)

    # populate the job in its default state (not started)
    with SessionLocal() as session:
        session.add(CosmosSessionJob(job_id, pdf.filename.replace('.pdf', ''), pdf_hash, pdf_len, job_output_dir))
        session.commit()

    await queue.put((job_output_dir, job_id, compress_images))

    return _build_process_response("PDF Processing in Background", job_id, request.url)

@prefix_router.get("/process/{job_id}/status")
def get_processing_status(job_id: str) -> JobStatus:
    """
    Return the current status of a given pdf in the COSMOS processing queue. If `job.is_completed`,
    then the results of the processing can be retrieved from `/process/{job_id}/result`
    """
    with SessionLocal() as session:
        job : CosmosSessionJob = session.get(CosmosSessionJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        
        return JobStatus(
            job_started=job.is_started,
            job_completed=job.is_completed,
            time_in_queue=job.time_in_queue,
            time_processing=job.time_processing,
            error=job.error
        )

@prefix_router.get("/process/{job_id}/result")
def get_processing_result(job_id: str) -> FileResponse:
    """
    Return the zip file containing the results of a completed COSMOS pipeline. Return status 400 if the 
    job is in a not-complete state
    """
    job = get_job_details(job_id)
    output_file = f"{job.pdf_name}_cosmos_output.zip"
    return FileResponse(f"{job.output_dir}/{output_file}", filename=output_file)

@prefix_router.get("/process/{job_id}/result/text")
def get_processing_result_text_segments(job_id: str, request: Request) -> List[CosmosJSONTextResponse]:
    """
    Return the text segments extracted by COSMOS and their bounding boxes as a list of JSON objects
    """
    job = get_job_details(job_id)
    response_json = convert_parquet_to_json(job, f'{job.pdf_name}.parquet', request)
    return [ CosmosJSONTextResponse(**p) for p in response_json ]

@prefix_router.get("/process/{job_id}/result/extractions/{extraction_type}")
def get_processing_result_extraction(job_id: str, extraction_type: ExtractionType, request: Request) -> List[CosmosJSONImageResponse]:
    """
    Return COSMOS figure/table/equation extractions and their bounding boxes as a list of JSON objects, 
    as well as links to their images
    """
    job = get_job_details(job_id)
    response_json = convert_parquet_to_json(job, f'{job.pdf_name}_{extraction_type.value}.parquet', request)
    return [ CosmosJSONImageResponse(**p) for p in response_json ]


@prefix_router.get("/process/{job_id}/result/images/{image_path}")
def get_processing_result_image(job_id: str, image_path: str) -> Response:
    """
    Extract a single image from the zip output of the given job and return it with the appropriate mimetype
    """
    job = get_job_details(job_id)
    mime_type = 'image/png' if image_path.endswith('.png') else 'image/jpeg'
    with extract_file_from_job(job, image_path) as image:
        return Response(content=image.read(), media_type=mime_type)


@prefix_router.post("healthcheck/evaluate/{job_id}")
def evaluate_results(job_id: str, expected_bounds: list[AnnotationBounds]) -> List[DocumentAnnotationComparison]:
    """
    Evaluate the results of a COSMOS job against a list of expected region bounding boxes
    """
    job = get_job_details(job_id)
    comparator = AnnotationComparator(read_job_zip_file(job), expected_bounds)
    return [comparator.compare_for_label(l) for l in DEFAULT_REGION_TYPES]


app.include_router(prefix_router)

def get_max_processes_per_gpu():
    """
    Approximately calculate the amount of cosmos pipelines that can be run in parallel based on
    the amount of memory available per GPU.
    TODO This assumes the COSMOS pipeline will be the only thing running on the GPU, which is
    not necessarily the case
    """
    if not torch.cuda.is_available():
        return 1
    max_mem = torch.cuda.get_device_properties(0).total_memory
    return int(max_mem / GPU_MEM_PER_WORKER)

@app.on_event("startup")
async def startup_event():
    global workers
    """
    Initialize FastAPI and add variables
    """
    max_worker_count = get_max_processes_per_gpu()
    logger.info(f"Creating {max_worker_count} work queues for COSMOS processing")
    workers = [asyncio.create_task(_cosmos_worker(queue)) for _ in range(max_worker_count)]

    asyncio.create_task(scheduler.serve())

