FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STOCKLY_ENV=production \
    STOCKLY_HOST=0.0.0.0 \
    STOCKLY_PORT=5001 \
    STOCKLY_DATA_DIR=/app/data \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5001/api/health')" || exit 1

CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
