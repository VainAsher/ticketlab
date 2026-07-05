# ticketlab — production image for the local-apps VM.
# Python/uvicorn app serving its own single-file frontend on 8090.
FROM python:3.12-slim

WORKDIR /app

COPY deploy/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY run.py run.py
COPY ticketlab/ ticketlab/
COPY scenarios/ scenarios/
COPY frontend/ frontend/

EXPOSE 8090

# run.py binds 127.0.0.1 unless told otherwise; the reverse proxy needs 0.0.0.0.
ENV TICKETLAB_HOST=0.0.0.0 \
    TICKETLAB_PORT=8090 \
    TICKETLAB_DB=/data/ticketlab.db

CMD ["python", "run.py"]
