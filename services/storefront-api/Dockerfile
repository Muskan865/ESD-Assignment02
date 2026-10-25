FROM python:3.11-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py openapi.yaml ./
COPY app ./app

# 8000: HTTP API. The same image also runs the gRPC order service
# (docker-compose `order-grpc`): 50051 gRPC, 9102 its Prometheus metrics.
EXPOSE 8000 50051 9102

# Single worker on purpose: prometheus_client keeps its registry in-process, so
# N workers would expose N partial views of every counter.
# --no-access-log: our JSON middleware already emits exactly one line per
# request. Leaving uvicorn's plain-text access log on would double every
# event in Elasticsearch and add lines that decode_json_fields cannot parse.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
