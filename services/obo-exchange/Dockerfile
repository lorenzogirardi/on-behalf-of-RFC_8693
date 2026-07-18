# Build context is ./services so common/obs.py can be shared.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY obo-exchange/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common/obs.py .
COPY obo-exchange/server.py .
RUN useradd --system --uid 10001 app
USER 10001
EXPOSE 8081
CMD ["python", "server.py"]
