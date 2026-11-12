FROM node:24-alpine AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY package.json package-lock.json ./
RUN npm ci
COPY app ./app
COPY lib ./lib
COPY next.config.mjs ./
RUN npm run build

FROM node:24-alpine
WORKDIR /app
ARG APP_VERSION=v1
ENV NODE_ENV=production HOSTNAME=0.0.0.0 PORT=3000 APP_VERSION=$APP_VERSION NEXT_TELEMETRY_DISABLED=1
COPY --from=build --chown=10001:10001 /app/.next/standalone ./
COPY --from=build --chown=10001:10001 /app/.next/static ./.next/static
COPY --chown=10001:10001 migrate.mjs ./
USER 10001:10001
CMD ["node", "server.js"]
