FROM python:3.13-alpine
WORKDIR /app
COPY app.py ./
RUN adduser -D -u 10001 app && mkdir /data && chown app:app /data
USER app
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=2s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=1)"
CMD ["python", "app.py"]
