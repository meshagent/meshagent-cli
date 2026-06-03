ARG MESHAGENT_IMAGE_PREFIX=__MESHAGENT_IMAGE_PREFIX__
FROM ${MESHAGENT_IMAGE_PREFIX}python-sdk-slim:__MESHAGENT_CLIENT_VERSION__ AS build
WORKDIR /app
COPY . .
RUN mkdir -p /out \
  && cp -a /opt/venv/lib/python3.13/site-packages/. /out/ \
  && python -m pip install --no-cache-dir --target /out --no-deps . \
  && python -m pip install --no-cache-dir --target /out --upgrade aiofiles~=24.1 "aiohttp[speedups]~=3.13.0"

FROM scratch
LABEL meshagent.runtime=python
WORKDIR /app
COPY --from=build /out /app
EXPOSE 8000
CMD ["-m", "server"]
