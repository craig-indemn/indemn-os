# indemn-ui — static React app served by lightweight Node server
FROM node:22-slim AS build

WORKDIR /app

# Install dependencies
COPY ui/package.json ui/package-lock.json ./
RUN npm ci

# Copy source and build
COPY ui/ ./
RUN npm run build

# Production image — serve with lightweight static server
FROM node:22-slim

WORKDIR /app
RUN npm install -g serve

COPY --from=build /app/dist ./dist

# Railway sets PORT automatically
CMD ["serve", "-s", "dist", "-l", "tcp://0.0.0.0:${PORT:-3000}"]
