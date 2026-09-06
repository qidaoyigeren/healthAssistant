# 用药协管员 · 前端

单照护者、单患者的本地照护记录 Web 应用。连接真实后端(`stage0.server:app`,FastAPI + SQLite),
所有业务记录来自真实提交与后端计算;没有 mock 数据、没有演示患者。

## 开发启动

```powershell
# 仓库根目录:后端(项目实际 Python 环境;单写者进程,禁止 --workers >1)
python -m pip install -r stage0/requirements-stage1.txt
python -m pip install -r stage0/requirements-stage10.txt
python -m uvicorn stage0.server:app --host 127.0.0.1 --port 8000

# 另一个终端:前端
cd frontend
npm install
npm run dev
```

打开 http://localhost:5173 。开发模式下 `/v1` 由 Vite 代理转发到 `http://127.0.0.1:8000`
(`frontend/vite.config.ts`)。需要指向其他后端时设置 `VITE_API_BASE`(见 `.env.example`)。

> 指定数据库:后端启动前设置 `STAGE0_DB_PATH`。首次使用空库即可,应用会显示空状态引导登记,
> 不写入任何示例数据。

## 生产构建

```powershell
cd frontend
npm run build      # tsc -b + vite build → dist/
npm run preview    # 本地预览构建产物
```

部署:`dist/` 是纯静态文件。用任意静态服务器托管,并把 `/v1` 反向代理到后端 FastAPI;
SPA 深链接需回退到 `index.html`(大多数静态服务器对不存在的路径默认回退,或显式配置)。
前后端同源时无需 CORS;跨源部署需在后端为明确 origin 配置 CORS(不要用 `*`)。

## 目录结构

```text
src/
  app/            路由、全局错误边界、提交完成后的查询失效
  api/            HTTP 客户端、DTO、幂等提交引擎(submissions.ts)
  features/       overview / profile / medications / alerts / conflicts / history / assistant / settings
  components/     基础 UI、证据联动(记录→检查结果→来源)、安全 Markdown
  hooks/          界面偏好(字号/时区)、会话与提交任务 hooks
```

## 行为要点(与后端契约对应)

- 提交一律走 `POST /v1/events`(异步 202)+ 轮询 `GET /v1/events/{key}`;每次提交一个
  UUID 幂等键,重发复用同键同内容;失败可「服务端重试(保留原记录)」或作为新提交重填。
- 刷新后凭 sessionStorage 中的提交标识恢复轮询;会话历史从 `GET /v1/sessions/{id}/events`
  恢复,未保存的助手正文如实显示为未保存。
- localStorage/sessionStorage 只保存字号、时区、会话号与未完成任务标识;患者数据一律以服务端
  SQLite 为权威来源。

## 已知边界

- 单照护者单患者;后端 `local-demo` 鉴权模式没有真实认证,请勿暴露到公网。
- 大字模式、时区显示为界面偏好;时间以 ISO 8601 存储,显示时按所选时区渲染。
