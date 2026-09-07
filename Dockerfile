FROM python:3.11-slim

# fonts-dejavu-core gives the ₹ glyph in generated PDF invoices.
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY scripts ./scripts

# The store's books and generated artifacts — mount a volume to persist them.
VOLUME ["/app/data"]
ENV KIRANA_DB_PATH=/app/data/kirana.db \
    PYTHONUNBUFFERED=1

CMD ["python", "-m", "src.kirana.main"]
