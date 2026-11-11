FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY comtrade_imports.py fetch_prices.py voorbeeld_invoer.xlsx ./
COPY app ./app

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000
# One worker on purpose: running jobs and their files are kept in memory.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
