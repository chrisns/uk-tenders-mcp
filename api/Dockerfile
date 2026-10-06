# UK Tenders MCP — API image (build context: api/)
FROM node:24-slim@sha256:b96009b6b18dc15ae52781abe71029198436d556149f441a11a44a706055c3a5 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY tsconfig.json ./
COPY src ./src
RUN npm run build

FROM node:24-slim@sha256:b96009b6b18dc15ae52781abe71029198436d556149f441a11a44a706055c3a5
WORKDIR /app
ENV NODE_ENV=production
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY --from=build /app/dist ./dist
USER node
EXPOSE 8080
CMD ["node", "dist/index.js"]
