FROM dart:stable
WORKDIR /app
COPY pubspec.yaml ./
RUN dart pub get
COPY bin ./bin
RUN dart compile exe bin/server.dart -o /app/server
EXPOSE 8081
CMD ["/app/server"]
