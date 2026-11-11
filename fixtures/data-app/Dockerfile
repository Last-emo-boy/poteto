FROM postgres:16-alpine
RUN apk add --no-cache python3
WORKDIR /app
COPY --chown=10001:10001 . .
USER 10001:10001
ENTRYPOINT ["python3"]
CMD ["app.py"]
