ARG MESHAGENT_IMAGE_PREFIX=__MESHAGENT_IMAGE_PREFIX__
FROM ${MESHAGENT_IMAGE_PREFIX}python-sdk-slim:__MESHAGENT_CLIENT_VERSION__ AS build
WORKDIR /app
COPY . .
RUN python -m pip install --no-cache-dir --target /out .

FROM scratch
LABEL meshagent.runtime=python
WORKDIR /app
COPY --from=build /out /app
EXPOSE 8000
CMD ["-m", "server"]
