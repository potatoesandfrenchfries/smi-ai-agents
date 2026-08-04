# syntax=docker/dockerfile:1
#
# Runs the Vite dev server rather than a production nginx build — this image
# is for local trial/testing (docker-compose up), where fast rebuilds and
# unminified stack traces matter more than a production bundle. Point a real
# deployment at a separate `vite build` + static-host step instead.

FROM node:22-alpine
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
