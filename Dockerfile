FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir fastapi uvicorn openai pydantic httpx

# Copy all project files
COPY . .

# Expose the application port
EXPOSE 8766

# Run the API server
CMD ["python", "api.py"]
