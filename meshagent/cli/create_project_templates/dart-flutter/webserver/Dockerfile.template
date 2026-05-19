FROM ghcr.io/cirruslabs/flutter:stable AS build
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
