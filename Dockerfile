FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# `recon` is a namespace package (no __init__.py at its root), so the app
# code is copied under /app/recon and imported as `recon.app.main`.
COPY app ./recon/app
COPY migrations ./recon/migrations

EXPOSE 8000

CMD ["uvicorn", "recon.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
