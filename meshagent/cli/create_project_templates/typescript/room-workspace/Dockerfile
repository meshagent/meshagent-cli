ARG MESHAGENT_IMAGE_PREFIX=__MESHAGENT_IMAGE_PREFIX__
FROM ${MESHAGENT_IMAGE_PREFIX}node-sdk:__MESHAGENT_CLIENT_VERSION__ AS build
WORKDIR /app
COPY package*.json .npmrc tsconfig.json next.config.ts next-env.d.ts ./
RUN npm install
COPY app ./app
RUN npm run build

FROM scratch
LABEL meshagent.runtime=node
WORKDIR /app
ENV HOSTNAME=0.0.0.0
ENV PORT=3000
COPY --from=build /app/.next/standalone /app
COPY --from=build /app/.next/static /app/.next/static
EXPOSE 3000
CMD ["server.js"]
