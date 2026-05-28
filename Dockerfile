FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies and system libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Download Indic and Latin fonts for PDF reports
RUN mkdir -p /app/fonts && \
    curl -sL "https://github.com/google/fonts/raw/main/ofl/notosansdevanagari/NotoSansDevanagari%5Bwdth%2Cwght%5D.ttf" -o /app/fonts/NotoSansDevanagari.ttf && \
    curl -sL "https://github.com/google/fonts/raw/main/ofl/notosanstamil/NotoSansTamil%5Bwdth%2Cwght%5D.ttf" -o /app/fonts/NotoSansTamil.ttf && \
    curl -sL "https://github.com/google/fonts/raw/main/ofl/notosans/NotoSans%5Bital%2Cwdth%2Cwght%5D.ttf" -o /app/fonts/NotoSans.ttf

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Final production stage
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies (ffmpeg is required for audio conversions)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /root/.local /root/.local
COPY --from=builder /app/fonts /app/fonts

# Copy application source code
COPY . /app

# Add local bin to path
ENV PATH=/root/.local/bin:$PATH
ENV PYTHONUNBUFFERED=1
ENV APP_ENV=production

# Expose port (Hugging Face routes to 7860 by default)
EXPOSE 7860

# Make start script executable (we also run it with 'sh' to bypass any execution bit issues)
RUN chmod +x start.sh 2>/dev/null || true

# Run startup script
CMD ["sh", "start.sh"]
