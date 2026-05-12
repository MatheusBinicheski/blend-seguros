FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN apt-get update && apt-get install -y xvfb && rm -rf /var/lib/apt/lists/*

CMD Xvfb :99 -screen 0 1280x900x24 -nolisten tcp & sleep 1 && DISPLAY=:99 uvicorn main_api:app --host 0.0.0.0 --port ${PORT:-8000}
