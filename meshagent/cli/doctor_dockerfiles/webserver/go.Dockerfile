FROM golang:1.24-alpine
WORKDIR /app
COPY server.go .
RUN go build -o server server.go
EXPOSE 8001
CMD ["./server"]
