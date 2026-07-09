ARG FLUTTER_IMAGE_PLATFORM=linux/amd64
FROM --platform=${FLUTTER_IMAGE_PLATFORM} ubuntu:24.04 AS build

ARG FLUTTER_VERSION=3.44.6
ARG FLUTTER_SHA256=a6320fd72e9a2690c08e2a6a70874a30cb120dee7c78f49d2c628bd7c9e20525
ENV PATH="/opt/flutter/bin:/opt/flutter/bin/cache/dart-sdk/bin:${PATH}"

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl git libglu1-mesa unzip xz-utils zip \
  && rm -rf /var/lib/apt/lists/* \
  && curl -fsSL --retry 3 \
    "https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/flutter_linux_${FLUTTER_VERSION}-stable.tar.xz" \
    -o /tmp/flutter.tar.xz \
  && echo "${FLUTTER_SHA256}  /tmp/flutter.tar.xz" | sha256sum -c - \
  && tar -xJf /tmp/flutter.tar.xz -C /opt \
  && rm /tmp/flutter.tar.xz
RUN git config --system --add safe.directory /opt/flutter
WORKDIR /app
COPY pubspec.yaml ./
RUN flutter pub get
COPY lib ./lib
COPY web ./web
RUN flutter build web --release

FROM nginx:1.27-alpine
COPY --from=build /app/build/web /usr/share/nginx/html
RUN rm -f /etc/nginx/conf.d/default.conf && printf '%s\n' \
  'pid /data/nginx/nginx.pid;' \
  'events {}' \
  'http {' \
  '  include /etc/nginx/mime.types;' \
  '  client_body_temp_path /data/nginx/client_temp;' \
  '  proxy_temp_path /data/nginx/proxy_temp;' \
  '  fastcgi_temp_path /data/nginx/fastcgi_temp;' \
  '  uwsgi_temp_path /data/nginx/uwsgi_temp;' \
  '  scgi_temp_path /data/nginx/scgi_temp;' \
  '  server { listen 80; location = /health { return 200 "ok\n"; } location = /status { return 200 "ready\n"; } location = /api/ping { default_type application/json; return 200 "{\"pong\":true}\n"; } location / { try_files $uri $uri/ /index.html; } }' \
  '}' > /etc/nginx/nginx.conf
EXPOSE 80
CMD ["sh", "-c", "mkdir -p /data/nginx/client_temp /data/nginx/proxy_temp /data/nginx/fastcgi_temp /data/nginx/uwsgi_temp /data/nginx/scgi_temp && nginx -c /etc/nginx/nginx.conf -g 'daemon off;'"]
