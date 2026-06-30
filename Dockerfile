# FOCCUS German Bight demonstrator — containerized Streamlit app.
#
# Build:  docker build -t foccus-esc1-gb .
# Run:    docker run --rm -p 8501:8501 foccus-esc1-gb
# Open:   http://localhost:8501
#
# Scientific data (PMTiles / NetCDF) is fetched at runtime from the public
# Edito MinIO bucket, so no data needs to be baked into the image.

# Python 3.13 matches the local "geo-app" env and Streamlit Cloud runtime.
FROM python:3.13-slim

# Avoid interactive prompts, ensure stdout/stderr are unbuffered for logs.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Minimal system libraries. The geospatial wheels (rasterio, netCDF4, h5py,
# shapely) bundle their own GDAL/HDF5/GEOS, so only a few shared libs and
# curl (for the healthcheck) are needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        libexpat1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first to leverage Docker layer caching.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy the application code and static assets (markdown, images, PDFs, logos).
COPY . .

# Streamlit serves on 8501 by default.
EXPOSE 8501

# Bind to all interfaces and disable the usage-stats prompt for headless use.
ENV STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl --fail http://localhost:8501/_stcore/health || exit 1

# Two-page entry point (About + Dashboard).
ENTRYPOINT ["streamlit", "run", "app_pmtiles_assessment_two_paged_s3.py"]
