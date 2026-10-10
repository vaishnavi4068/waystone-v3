# Waystone HQ sentiment jobs (Cloud Run): free feeds -> FinBERT -> gates -> Cloud SQL.
# Built from the repo root: gcloud builds submit --config deploy/sentiment/cloudbuild.yaml .
FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# CPU-only torch keeps the image near 1.5 GB instead of pulling CUDA wheels.
RUN uv pip install --python /app/.venv/bin/python --no-cache \
        --index-url https://download.pytorch.org/whl/cpu "torch>=2.3,<3" \
    && uv pip install --python /app/.venv/bin/python --no-cache "transformers>=4.44,<5"

# Bake the model in so a job never depends on the Hugging Face Hub at run time.
ENV HF_HOME=/app/hf
RUN /app/.venv/bin/python -c "from transformers import pipeline; \
p = pipeline('text-classification', model='ProsusAI/finbert', top_k=None); \
print(p(['Fed holds rates steady'])[0])"

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    WAYSTONE_SENTIMENT_SCORER=finbert

ENTRYPOINT ["/app/.venv/bin/waystone3", "sentiment"]
CMD ["--job", "daily"]
