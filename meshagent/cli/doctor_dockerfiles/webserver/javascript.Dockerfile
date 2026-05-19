ARG MESHAGENT_IMAGE_PREFIX=__MESHAGENT_IMAGE_PREFIX__
FROM ${MESHAGENT_IMAGE_PREFIX}node-sdk:__MESHAGENT_CLIENT_VERSION__ AS build
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY server.js ./
RUN npm run build

FROM scratch
LABEL meshagent.runtime=node
WORKDIR /app
COPY --from=build /app/dist/index.js /app/index.js
EXPOSE 3000
CMD ["index.js"]
