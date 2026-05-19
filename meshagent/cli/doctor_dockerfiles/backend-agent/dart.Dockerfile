FROM dart:stable
WORKDIR /app
COPY pubspec.yaml ./
RUN dart pub get
COPY bin ./bin
RUN dart compile exe bin/server.dart -o /app/server
CMD ["/app/server"]
