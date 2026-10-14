FROM python:3.13-slim
RUN pip install --no-cache-dir cue-notify==0.5.1
ENV CUE_API_KEY=ck_introspection_only \
    CUE_URL=http://127.0.0.1:8000
ENTRYPOINT ["cue-notify", "mcp"]
