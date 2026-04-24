FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

COPY aletheia ./aletheia

EXPOSE 8080

CMD ["uvicorn", "aletheia.app.main:app", "--host", "0.0.0.0", "--port", "8080"]
