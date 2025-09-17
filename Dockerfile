# --- Rocky Soulmode Dockerfile ---
FROM python:3.11-slim

# set workdir
WORKDIR /app

# install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy project files
COPY . .

# expose FastAPI default port
EXPOSE 8000

# run uvicorn with autoreload disabled (prod mode)
CMD ["uvicorn", "rocky_soulmode_api:app", "--host", "0.0.0.0", "--port", "8000"]
