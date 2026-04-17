# indemn-ui — static React app served by lightweight Node server
FROM node:22-slim AS build

WORKDIR /app

# Install dependencies
COPY ui/package.json ui/package-lock.json ./
RUN npm ci

# Copy source and build — Vite embeds env vars at build time
COPY ui/ ./
ARG VITE_API_URL=https://indemn-api-production.up.railway.app
ARG VITE_CHAT_HARNESS_WS_URL=wss://indemn-runtime-chat-production.up.railway.app/ws/chat
ENV VITE_API_URL=$VITE_API_URL
ENV VITE_CHAT_HARNESS_WS_URL=$VITE_CHAT_HARNESS_WS_URL
RUN npm run build

# Production image — serve with lightweight static server
FROM node:22-slim

WORKDIR /app
RUN npm install -g serve

COPY --from=build /app/dist ./dist

# Railway sets PORT env var — use shell form for variable expansion
CMD ["sh", "-c", "serve -s dist -l tcp://0.0.0.0:${PORT:-3000}"]
