FROM python:3.12-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev make zlib1g-dev libbz2-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r /tmp/requirements.txt

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DELTA_CONTRACTS_DIR=/app/vendor/contracts PYTHONPATH=/app/src \
    MPLCONFIGDIR=/tmp/matplotlib
WORKDIR /app
COPY --from=build /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links /wheels -r requirements.txt && rm -rf /wheels
COPY vendor/contracts/ vendor/contracts/
COPY src/ src/
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin delta
USER 1000
CMD ["python", "-m", "delta_worker"]
