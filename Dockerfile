FROM python:3.13-alpine

WORKDIR /app
COPY exporter.py .

USER 65532:65532
EXPOSE 9488

ENTRYPOINT ["python", "/app/exporter.py"]
