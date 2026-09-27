FROM python:3.12-slim

RUN apt-get update && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir sherpa-onnx==1.13.8 "numpy==2.*"

RUN useradd --create-home --shell /usr/sbin/nologin stt

WORKDIR /app
COPY server.py /app/server.py

ENV PYTHONUNBUFFERED=1
EXPOSE 9100

USER stt
CMD ["python", "/app/server.py"]
