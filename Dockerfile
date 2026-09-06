FROM python:3.12-slim

# libgl1/libglib2.0-0: required by mediapipe/opencv-style native deps at import time.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-vision.txt requirements-backend.txt ./
# torch/torchvision from the CPU-only wheel index first: the default PyPI wheels
# pull ~2GB of NVIDIA CUDA runtime packages as dependencies even on machines
# with no NVIDIA GPU (this host has none). Installing the CPU build up front
# satisfies requirements-vision.txt's version pin so the next step won't
# re-resolve a GPU build.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch torchvision
RUN pip install --no-cache-dir -r requirements.txt \
    -r requirements-vision.txt \
    -r requirements-backend.txt

COPY deepcheck ./deepcheck
COPY backend ./backend

# Face-crop model (see README.md "얼굴 crop 모델 받기"). Best-effort: face.py
# already degrades to full-frame analysis if this file is missing, so a network
# hiccup during build must not fail the whole image.
RUN mkdir -p deepcheck/models && \
    curl -fsSL -o deepcheck/models/blaze_face_short_range.tflite \
      https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite \
    || echo "face model download failed at build time, will run without face-crop"

EXPOSE 8000

# NOTE: exactly one uvicorn worker process. Hermes (backend/harness.py) keeps
# job state in an in-process dict — a second process would not see the same
# jobs, so GET /api/jobs/{id} would 404 at random depending on which worker
# handled which request.
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
