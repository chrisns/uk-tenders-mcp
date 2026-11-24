# UK Tenders MCP — API image (build context: api/)
FROM node:26-slim@sha256:0c6375bcc1ee170c22bd04e11b7c1ff9afc3bb16a865a2629308ceb4b72380ff AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY tsconfig.json ./
COPY src ./src
RUN npm run build

FROM node:26-slim@sha256:0c6375bcc1ee170c22bd04e11b7c1ff9afc3bb16a865a2629308ceb4b72380ff
WORKDIR /app
ENV NODE_ENV=production
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY --from=build /app/dist ./dist
USER node
EXPOSE 8080
CMD ["node", "dist/index.js"]
