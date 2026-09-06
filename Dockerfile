FROM python:3.13-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Volatile release metadata belongs after dependency/application layers so a
# new build timestamp does not invalidate the expensive FFmpeg/pip cache.
ARG VCS_REF=dev
ARG BUILD_DATE=unknown
LABEL org.opencontainers.image.revision=$VCS_REF \
      org.opencontainers.image.created=$BUILD_DATE \
      org.opencontainers.image.source="https://github.com/mikheooo/reels_bot"
ENV APP_GIT_SHA=$VCS_REF \
    APP_BUILD_DATE=$BUILD_DATE

ENV PYTHONPATH=/app
