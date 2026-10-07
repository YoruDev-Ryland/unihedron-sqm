FROM python:3.12-slim

ARG BUILD_VERSION=dev
ARG VCS_REF=unknown

LABEL org.opencontainers.image.title="Unihedron SQM Collector" \
      org.opencontainers.image.description="A lightweight collector, dashboard, discovery tool, and log importer for Ethernet and USB Unihedron SQM meters" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WEB_PORT=7942

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sqm_service ./sqm_service
COPY LICENSE ./

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin sqm \
    && usermod --append --groups dialout sqm \
    && mkdir -p /data /imports \
    && chown sqm:sqm /data /imports

USER sqm

VOLUME ["/data"]
EXPOSE 7942

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('WEB_PORT','7942')+'/health', timeout=3)"

CMD ["sh", "-c", "exec uvicorn sqm_service.main:app --host 0.0.0.0 --port \"${WEB_PORT:-7942}\""]
