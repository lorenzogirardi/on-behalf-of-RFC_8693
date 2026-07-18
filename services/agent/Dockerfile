# Build context is ./services so common/obs.py can be shared.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common/obs.py .
COPY agent/*.py .
RUN useradd --system --uid 10001 app
USER 10001
EXPOSE 8082
CMD ["python", "agent.py"]
