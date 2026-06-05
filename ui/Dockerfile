# ── Stage 1: build ────────────────────────────────────────────────────────────
FROM node:26-alpine AS builder

# Accept the API base URL at build time so nginx can proxy to it.
ARG API_URL=http://localhost:8000

WORKDIR /app

# Install dependencies first (layer-cache friendly).
COPY package.json yarn.lock* package-lock.json* ./
RUN npm install --frozen-lockfile 2>/dev/null || npm install

# Copy source and build.
COPY . .
RUN npm run build


# ── Stage 2: serve ────────────────────────────────────────────────────────────
FROM nginx:1.31-alpine AS runner

# Remove the default nginx welcome page.
RUN rm -rf /usr/share/nginx/html/*

# Copy built assets from the builder stage.
COPY --from=builder /app/dist /usr/share/nginx/html

# Copy nginx config (written below via heredoc into the image).
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Substitute the API_URL placeholder in the nginx config at container start.
ARG API_URL=http://localhost:8000
ENV API_URL=${API_URL}

# Use a small entrypoint script to do runtime env substitution on API_URL.
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
