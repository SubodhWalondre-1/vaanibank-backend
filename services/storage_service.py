"""
VaaniBank AI — Cloudflare R2 Storage Service
PSBs Hackathon 2026 | Team Vectora

Handles file storage for customer speech recordings, TTS audio files, and summary PDFs.
Provides a thread-safe S3 client targeting Cloudflare R2 if configured,
with a seamless local filesystem fallback if R2 credentials are missing or invalid.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import aiofiles

from config import settings

logger = logging.getLogger("vaanibank.storage")

class StorageService:
    def __init__(self) -> None:
        self.r2_configured = (
            bool(settings.R2_ACCOUNT_ID) and
            bool(settings.R2_ACCESS_KEY_ID) and
            bool(settings.R2_SECRET_ACCESS_KEY) and
            bool(settings.R2_BUCKET_NAME)
        )
        self._s3_client = None

        if self.r2_configured:
            try:
                # Cloudflare R2 S3 endpoint format: https://<account_id>.r2.cloudflarestorage.com
                endpoint_url = f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
                
                # Configure custom client timeout and retries for production grade reliability
                config = Config(
                    retries={"max_attempts": 3, "mode": "standard"},
                    connect_timeout=5.0,
                    read_timeout=10.0
                )
                
                self._s3_client = boto3.client(
                    service_name="s3",
                    endpoint_url=endpoint_url,
                    aws_access_key_id=settings.R2_ACCESS_KEY_ID,
                    aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
                    config=config,
                    # R2 does not support S3 styling for bucket hosting unless configured
                    region_name="auto",
                )
                logger.info(
                    "Cloudflare R2 storage initialized | Bucket: %s | Endpoint: %s",
                    settings.R2_BUCKET_NAME,
                    endpoint_url,
                )
            except Exception as e:
                logger.error("Failed to initialize Cloudflare R2 S3 client: %s. Falling back to local storage.", e)
                self.r2_configured = False
        else:
            logger.info("Cloudflare R2 environment variables not complete. Local fallback storage active.")

    def is_configured(self) -> bool:
        """Returns True if Cloudflare R2 is configured and client successfully created."""
        return self.r2_configured

    async def upload_audio_bytes(
        self,
        audio_bytes: bytes,
        filename: str,
        content_type: str = "audio/wav",
    ) -> str:
        """
        Uploads audio bytes to Cloudflare R2 if configured, otherwise saves to local filesystem.
        Returns the public URL (R2 CDN URL or local mounted static URL).
        """
        if self.is_configured():
            try:
                # Key under which the object is stored
                key = f"audio/{filename}"
                
                # Upload asynchronously in an executor thread since boto3 is synchronous
                await asyncio.to_thread(
                    self._s3_client.put_object,
                    Bucket=settings.R2_BUCKET_NAME,
                    Key=key,
                    Body=audio_bytes,
                    ContentType=content_type,
                )
                
                # Generate public R2 URL
                public_url_base = settings.R2_PUBLIC_URL.rstrip("/")
                public_url = f"{public_url_base}/{key}"
                logger.info("Uploaded audio to R2 successfully: %s", public_url)
                return public_url
            except Exception as e:
                logger.error("Failed to upload audio to Cloudflare R2: %s. Falling back to local disk.", e)
                # Fall through to local fallback

        # Local storage fallback
        local_dir = Path(settings.AUDIO_STORAGE_PATH)
        local_dir.mkdir(parents=True, exist_ok=True)
        local_path = local_dir / filename
        
        async with aiofiles.open(str(local_path), "wb") as f:
            await f.write(audio_bytes)
            
        logger.info("Saved audio locally: %s", local_path)
        return f"/audio/{filename}"

    async def upload_pdf_file(
        self,
        local_path: Path,
        filename: str,
        content_type: str = "application/pdf",
    ) -> str:
        """
        Uploads a generated PDF file from a local path to Cloudflare R2 if configured.
        Deletes the local copy to conserve space after successful upload.
        If R2 is not configured, returns the local static URL.
        """
        if not local_path.exists():
            raise FileNotFoundError(f"Local PDF file not found at {local_path}")

        if self.is_configured():
            try:
                key = f"summaries/{filename}"
                
                # Read file bytes
                async with aiofiles.open(str(local_path), "rb") as f:
                    pdf_bytes = await f.read()

                # Upload asynchronously
                await asyncio.to_thread(
                    self._s3_client.put_object,
                    Bucket=settings.R2_BUCKET_NAME,
                    Key=key,
                    Body=pdf_bytes,
                    ContentType=content_type,
                )

                # Keep local file for dual storage (local + cloud)
                logger.info("Kept local copy of PDF summary for dual storage: %s", local_path)

                public_url_base = settings.R2_PUBLIC_URL.rstrip("/")
                public_url = f"{public_url_base}/{key}"
                logger.info("Uploaded PDF summary to R2 successfully: %s", public_url)
                return public_url
            except Exception as e:
                logger.error("Failed to upload PDF summary to Cloudflare R2: %s. Serving locally.", e)
                # Fall through to local fallback

        # If R2 is not configured, we keep the file locally and return the local path
        logger.info("Serving PDF summary locally: %s", local_path)
        return f"/summaries/{filename}"

    def upload_pdf_file_sync(
        self,
        local_path: Path,
        filename: str,
        content_type: str = "application/pdf",
    ) -> str:
        """
        Synchronous version of upload_pdf_file.
        Uploads a generated PDF file from a local path to Cloudflare R2 if configured.
        Deletes the local copy to conserve space after successful upload.
        If R2 is not configured, returns the local static URL.
        """
        if not local_path.exists():
            raise FileNotFoundError(f"Local PDF file not found at {local_path}")

        if self.is_configured():
            try:
                key = f"summaries/{filename}"
                
                # Read file bytes synchronously
                with open(local_path, "rb") as f:
                    pdf_bytes = f.read()

                # Upload synchronously
                self._s3_client.put_object(
                    Bucket=settings.R2_BUCKET_NAME,
                    Key=key,
                    Body=pdf_bytes,
                    ContentType=content_type,
                )

                # Keep local file for dual storage (local + cloud)
                logger.info("Kept local copy of PDF summary for dual storage: %s", local_path)

                public_url_base = settings.R2_PUBLIC_URL.rstrip("/")
                public_url = f"{public_url_base}/{key}"
                logger.info("Uploaded PDF summary to R2 successfully: %s", public_url)
                return public_url
            except Exception as e:
                logger.error("Failed to upload PDF summary to Cloudflare R2: %s. Serving locally.", e)
                # Fall through to local fallback

        # If R2 is not configured, we keep the file locally and return the local path
        logger.info("Serving PDF summary locally: %s", local_path)
        return f"/summaries/{filename}"

# Module-level singleton
storage_service = StorageService()

