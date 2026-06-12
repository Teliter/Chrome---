FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py dashboard.html ./
ENV CHROME_MANAGER_DB=/data/cloud.db
ENV CHROME_MANAGER_KEY_FILE=/data/cloud-data.key

VOLUME ["/data"]
EXPOSE 8787
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8787"]

