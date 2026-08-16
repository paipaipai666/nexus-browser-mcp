# Glama 发布/健康检查用: MCP server 以 stdio 运行; 内省(tools/list)无需浏览器,
# 但预装 chromium 使容器开箱即可真实操控浏览器。
FROM python:3.13-slim-bookworm

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && playwright install --with-deps chromium

ENV BROWSER_HEADLESS=true

CMD ["nexus-browser-mcp"]
