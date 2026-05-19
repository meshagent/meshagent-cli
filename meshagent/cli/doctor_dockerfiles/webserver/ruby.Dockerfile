FROM ruby:3.4-alpine
WORKDIR /app
COPY server.rb .
EXPOSE 4567
CMD ["ruby", "server.rb"]
