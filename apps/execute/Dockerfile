# syntax=docker/dockerfile:1.7
#
# Execute console. Built from the repository root because the app shares a workspace with
# @finops/shared; the build context is the monorepo, not this directory.
#
#   docker build -f apps/execute/Dockerfile .
FROM node:22-alpine AS builder
WORKDIR /app

COPY package.json package-lock.json ./
COPY packages/shared/package.json packages/shared/
COPY apps/console/package.json apps/console/
COPY apps/execute/package.json apps/execute/
RUN npm ci --no-audit --no-fund

COPY packages ./packages
COPY apps/execute ./apps/execute

ARG VITE_API_BASE_URL=""
ARG VITE_EXECUTE_AGENTS="customer_service,aml_investigation"
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
ENV VITE_EXECUTE_AGENTS=$VITE_EXECUTE_AGENTS
RUN npm run build --workspace @finops/execute

FROM nginx:1.27-alpine AS runtime
RUN rm /etc/nginx/conf.d/default.conf
COPY --from=builder /app/apps/execute/dist /usr/share/nginx/html
COPY apps/execute/nginx.conf /etc/nginx/conf.d/finops.conf
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=4s CMD wget -qO- http://localhost:8080/healthz || exit 1
CMD ["nginx", "-g", "daemon off;"]
