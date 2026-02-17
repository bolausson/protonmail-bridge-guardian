FROM alpine:3

RUN apk add --no-cache python3 py3-pip docker-cli

WORKDIR /app
COPY guardian.py /app/guardian.py

CMD ["python3", "/app/guardian.py"]
