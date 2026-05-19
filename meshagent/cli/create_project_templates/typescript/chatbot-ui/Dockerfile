FROM node:22-alpine
WORKDIR /app
ENV HOSTNAME=0.0.0.0
ENV PORT=3000
COPY package*.json .npmrc ./
RUN npm install
COPY . .
RUN npm run build
EXPOSE 3000
CMD ["sh", "-c", "npm start -- -H 0.0.0.0 -p ${PORT:-3000}"]
