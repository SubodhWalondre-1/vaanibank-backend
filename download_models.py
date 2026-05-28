import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("model_downloader")

def download_models():
    logger.info("Using Gemini API for RAG embeddings. Skipping heavy model downloads.")

if __name__ == "__main__":
    download_models()

