# UK Tenders MCP — API image (build context: api/)
FROM node:26-slim@sha256:712b5d4c10e5262e60d8c027f7eb84065d3fa6a6e8fe9001d0d877bdb030b7f7 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY tsconfig.json ./
COPY src ./src
RUN npm run build

FROM node:26-slim@sha256:712b5d4c10e5262e60d8c027f7eb84065d3fa6a6e8fe9001d0d877bdb030b7f7
WORKDIR /app
ENV NODE_ENV=production
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY --from=build /app/dist ./dist
USER node
EXPOSE 8080
CMD ["node", "dist/index.js"]
