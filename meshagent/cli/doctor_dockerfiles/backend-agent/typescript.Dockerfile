ARG MESHAGENT_IMAGE_PREFIX=__MESHAGENT_IMAGE_PREFIX__
FROM ${MESHAGENT_IMAGE_PREFIX}node-sdk:__MESHAGENT_CLIENT_VERSION__ AS build
WORKDIR /app
COPY package*.json tsconfig.json ./
RUN npm install
COPY src ./src
RUN npm run build

FROM scratch
LABEL meshagent.runtime=node
WORKDIR /app
COPY --from=build /app/dist/index.js /app/index.js
CMD ["index.js"]
