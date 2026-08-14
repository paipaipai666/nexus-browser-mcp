# 接入指南:Claude Code / Pi Coding Agent

同一份 `nexus-browser-mcp` 服务器,三个 agent 通过 stdio MCP 接入,只换配置。发布到 PyPI 后全部改用 `uvx nexus-browser-mcp`。

> 尚未发布 PyPI 时的本地路径接入见各节"未发布时"。

## Claude Code

### 推荐:CLI 命令(官方首选方式)

```bash
# 项目级 (写入 .mcp.json, 团队共享)
claude mcp add --scope project --transport stdio browser -- uvx nexus-browser-mcp

# 用户级 (所有项目可用)
claude mcp add --scope user --transport stdio browser -- uvx nexus-browser-mcp
```

> `--` 之后是服务器自身命令,必须保留。

### 或:手写 `.mcp.json`(项目根,官方格式)

```json
{
  "mcpServers": {
    "browser": {
      "type": "stdio",
      "command": "uvx",
      "args": ["nexus-browser-mcp"],
      "env": {}
    }
  }
}
```

未发布时(本地路径,同样需 `type: stdio`):

```json
{
  "mcpServers": {
    "browser": {
      "type": "stdio",
      "command": "D:\\code\\nexus-browser-mcp\\.venv\\Scripts\\nexus-browser.exe",
      "args": [],
      "env": {}
    }
  }
}
```

字段备忘:`.mcp.json` 支持 `${VAR}`/`${VAR:-default}` 环境变量展开;服务器进程里可读 `CLAUDE_PROJECT_DIR`(项目根)。三个 scope:项目(`.mcp.json`)、local(`~/.claude.json`,默认)、user(`~/.claude.json` 全局)。

可用性:`~/.claude/skills/browser-agent/SKILL.md` 已放好,Claude Code 会自动扫描该目录。

## Pi Coding Agent

Pi 读取的是**标准 MCP 配置路径**,优先级从高到低:`.pi/mcp.json`(项目覆盖) → `.mcp.json`(项目共享) → `~/.pi/agent/mcp.json`(Pi 全局) → `~/.agents/mcp.json` → `~/.config/mcp/mcp.json`(用户全局)。

### 项目级 `.mcp.json`(推荐,标准格式)

```json
{
  "mcpServers": {
    "browser": {
      "command": "uvx",
      "args": ["nexus-browser-mcp"]
    }
  }
}
```

未发布时(本地路径):

```json
{
  "mcpServers": {
    "browser": {
      "command": "D:\\code\\nexus-browser-mcp\\.venv\\Scripts\\nexus-browser.exe",
      "args": []
    }
  }
}
```

### 或 Pi 全局:用户全局共享 `~/.config/mcp/mcp.json`

```json
{
  "mcpServers": {
    "browser": {
      "command": "uvx",
      "args": ["nexus-browser-mcp"],
      "lifecycle": "lazy"
    }
  }
}
```

说明:
- stdio 是默认 transport,可省 `transport` 字段;显式写 `"transport": "stdio"` 亦可。
- `lifecycle`: `lazy`(默认,手动 `/mcp:start`)或 `eager`(会话启动自动连)。
- 配置后运行 `/reload`(或重启)生效;用 `/mcp` 面板查看连接状态。
- 若已有 `.mcp.json`,Pi 直接用,无需改任何 Pi 专属文件。

## 验证接入

任一 agent 里让它:"用浏览器打开 https://example.com 然后告诉我页面标题"。成功标准:
1. `browser_navigate` 返回"已导航至 ... 标题: Example Domain"
2. `browser_snapshot` 返回结构化元素
3. `browser_tasks` 能看到 `default` task

## 多 agent 并发注意

每个 agent 连接得到独立 session(服务器进程各自启动),互不干扰。同一 agent 内多任务用不同 `task_id` 隔离。Cookie 互不共享(除 CDP 模式连同一浏览器)。