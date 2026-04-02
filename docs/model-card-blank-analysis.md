# 模型卡片空白问题 — 根因分析与产品化解决方案

## 一、问题概述

模型卡片在特定场景下出现空白内容（轮次已创建、badge 显示"已完成"或"失败"，但内容区无任何文字）。本文从「实时流式」和「刷新重放」两条路径，逐层拆解全部可能原因，并给出产品化解决方案。

---

## 二、数据完整链路

```
LLM API → llm_client.py (提取 delta)
       → chat_service.py (过滤/发送/持久化)
       → WebSocket (传输)
       → websocket.js (事件分发)
       → nodes.js (创建轮次/追加文本/渲染)
       → DOM 显示

刷新后:
       database.py → /api/canvases/{id}/state
       → clusters.js replayCluster() → nodes.js 重建
```

---

## 三、11 类根因分析

### 路径 A：实时流式阶段（用户在线观看时）

#### 1. LLM API 返回空响应

| 场景 | 触发条件 | 表现 |
|------|---------|------|
| 模型拒绝回答 | 安全策略触发、上下文超限 | 流开始后立刻结束，0 个 delta |
| API 兼容问题 | 第三方 OpenAI 兼容端点，delta 格式不标准 | `_extract_openai_delta_text()` 提取到空串 |
| Anthropic 事件跳过 | 只有 `message_start`/`message_delta`，无 `content_block_delta` | `__anext__()` 循环至 `StopAsyncIteration` |

**代码位置**: `llm_client.py:188-191`（OpenAI）, `llm_client.py:400-405`（Anthropic）

**后果**: 后端 `if delta_text:` 过滤掉所有空 delta → 前端从未收到任何 `stream_delta` 事件 → 卡片只有轮次 tab 但内容为空。

#### 2. 后端超时（70 秒）

**代码位置**: `chat_service.py:170`

```python
full_text, input_tokens, output_tokens = await asyncio.wait_for(
    self._collect_model_stream(...), timeout=70.0
)
```

**后果**: 超时后进入 except 分支，已流式到前端的部分 delta 已渲染，但 `record_model_result()` 只存 `error_text`，**content 为 NULL**。刷新页面后部分内容丢失。

#### 3. WebSocket 传输丢失

| 场景 | 触发条件 |
|------|---------|
| 连接断开 | 网络切换、服务器重启、标签页后台休眠 |
| 重连间隙 | 断开→重连的 1-30 秒内后端仍在推送 |
| 消息队列溢出 | 多模型并发推送，单连接瓶颈 |

**代码位置**: `websocket.js:40-46`（重连后 reload），`chat_service.py:_send_event()`（发送锁）

**后果**: 前端丢失部分/全部 delta → 卡片内容不完整或空白。

#### 4. 前端渲染器未初始化

**代码位置**: `nodes.js:78-116`（ensureModelTurn）

`turn.mdEl = panel.querySelector('.md')` 在 panel 刚通过 `innerHTML` 创建时执行。理论上 DOM 同步创建不会为 null，但如果模块加载顺序异常或 `window.renderMarkdown` 未就绪，渲染可能输出空 HTML。

#### 5. 用户手动取消请求

**代码位置**: `chat_service.py:105-108`

取消后，已启动的模型任务被 cancel，`record_model_result()` 不被调用。前端卡片已创建但停留在"生成中..."状态（非空白，但可视为异常空白）。

---

### 路径 B：刷新页面重放阶段

#### 6. 数据库 content 为 NULL

**代码位置**: `database.py:500-512`

```sql
content TEXT,  -- 允许 NULL
```

以下情况 content 为 NULL:
- 模型 API 错误（429/500/网络异常）
- 超时（70 秒）
- 任何非 `status='success'` 的结果

**写入对比**:
```python
# 成功时 (chat_service.py:213)
record_model_result(content=full_text)   # full_text 可能是空串 ""

# 失败时 (chat_service.py:298)
record_model_result(error_text=str(e))   # content 参数不传 → NULL
```

#### 7. 重放条件跳过空内容

**代码位置**: `clusters.js:395`

```javascript
if (rd.content) {  // NULL、""、undefined 全部跳过
    appendTurnText(request_id, model, rd.round, rd.content);
    flushTurnRender(request_id, model, rd.round);
}
```

**后果**: `ensureModelTurn()` 创建了轮次（有 tab），但 `turn.raw` 始终为初始值 `''`，mdEl 内容为空。Badge 显示"第 N 轮完成"或"失败"，但内容区完全空白。

#### 8. 错误结果无可视信息

失败轮次：
- 前端显示 `setTurnState('失败')` — 仅在 `.turn-state` 小标签中
- `.md` 区域（主内容区）无任何内容
- 用户看到空白卡片 + 右上角"失败"二字

---

### 路径 C：竞态与边界场景

#### 9. 多轮讨论中间轮全部失败

**代码位置**: `chat_service.py:119-130`

如果某轮所有模型返回空 → `round_inputs` 为空 → 讨论停止。但该轮的卡片已经创建，内容全部为空。

#### 10. 分支对话来源轮为空

父轮 content 为 NULL 时，分支对话正常创建但参考上下文不存在。

#### 11. 流式过程中浏览器标签页被后台挂起

现代浏览器对后台标签页限制定时器和网络活动。WebSocket 可能被暂停，恢复后前端时间线与后端不同步。

---

## 四、影响面评估

| 场景 | 发生频率 | 用户感知 | 数据可恢复性 |
|------|---------|---------|------------|
| API 返回空 | 低（取决于模型） | 高：完全空白 | 不可恢复 |
| 超时 70s | 中（慢模型常见） | 高：刷新后丢内容 | 部分可恢复（前端有缓存） |
| 网络断连 | 中 | 高：内容中断 | 刷新可部分恢复 |
| 刷新后重放空内容 | 高（每次刷新必现） | 高：已完成的卡片变空 | 不可恢复（DB 无数据） |
| 模型全部失败 | 低 | 高：空白 + "失败" | 不可恢复 |
| 浏览器标签后台 | 中（移动端常见） | 中：恢复后部分缺失 | 刷新可恢复 |

---

## 五、产品化解决方案

### 方案 1：空白卡片占位状态（必做，前端）

**目标**: 任何情况下卡片都不应该是纯空白，应该有明确的状态指示。

**设计**:
```
┌─────────────────────────────────┐
│  GLM-5              第 2 轮失败  │
│  ─────────────────────────────  │
│  第 1 轮 | [第 2 轮] | 第 3 轮  │
│  ─────────────────────────────  │
│                                 │
│    ⚠ 该轮未获取到模型回复        │
│                                 │
│    错误原因：请求超时 (70s)      │
│    建议：点击"重试当前"重新请求   │
│                                 │
│  [预览] [复制当前] [重试当前]     │
└─────────────────────────────────┘
```

**规则**:
| 条件 | 显示内容 |
|------|---------|
| `turn.raw === '' && status === '已完成'` | "该轮回复内容为空。模型可能因内容策略未生成回复。" |
| `turn.raw === '' && status === '失败'` | "该轮请求失败：{error_text}。点击重试。" |
| `turn.raw === '' && status === '生成中...'` | 骨架屏 Loading 动画 |
| `turn.raw === '' && status === '等待中'` | "等待模型响应..." |

**涉及文件**: `nodes.js`（`flushTurnRender`, `setTurnState`）, `styles.css`

---

### 方案 2：超时/失败时保存部分内容（推荐，后端）

**目标**: 即使模型超时或出错，也保存已接收到的部分文本。

**改动**:
```python
# chat_service.py — _stream_single_model()

# 在 try 块外提前声明 partial_chunks
partial_chunks = []

try:
    full_text, input_tokens, output_tokens = await asyncio.wait_for(
        self._collect_model_stream(stream, websocket, request_id, model,
                                    round_number, total_rounds,
                                    partial_chunks=partial_chunks),  # 传入引用
        timeout=70.0
    )
except asyncio.TimeoutError:
    partial_text = "".join(partial_chunks)
    await self.database.record_model_result(
        request_id=request_id,
        model=model,
        round_number=round_number,
        status="error",
        content=partial_text or None,   # 保存已收到的部分内容
        error_text=f"模型响应超时 (70秒)，已收到 {len(partial_text)} 字符",
    )
```

**影响**: 刷新页面后，超时卡片仍能显示已接收的部分内容 + 超时标记。

**涉及文件**: `chat_service.py`

---

### 方案 3：重放时显示错误信息（推荐，前端）

**目标**: 重放 content 为 NULL 的轮次时，显示 error_text 而非空白。

**改动**:
```javascript
// clusters.js — replayCluster()

for (const rd of sorted) {
    ensureModelTurn(request_id, model, rd.round);
    if (rd.content) {
        appendTurnText(request_id, model, rd.round, rd.content);
        flushTurnRender(request_id, model, rd.round);
    } else if (rd.error_text) {
        // 显示错误信息作为内容
        appendTurnText(request_id, model, rd.round,
            `> ⚠ 该轮请求未成功\n>\n> ${rd.error_text}`);
        flushTurnRender(request_id, model, rd.round);
    }
    // ...
}
```

**涉及文件**: `clusters.js`

---

### 方案 4：WebSocket 断线恢复机制（可选，前后端）

**目标**: 重连后恢复断线期间丢失的 delta。

**方案 A（简单）**: 重连后自动 reload 当前画布（已实现，`websocket.js:44`）。
- 缺点：丢失流式过程中的实时进度。

**方案 B（完善）**: 后端为每个 WebSocket 会话维护消息序号，重连时客户端发送最后收到的序号，后端重发缺失的消息。
- 复杂度高，需要消息队列缓存。

**推荐**: 保持方案 A，但在重连后增加"已自动刷新画布"的 toast 提示。

---

### 方案 5：流式结束时的兜底检查（推荐，后端）

**目标**: 当整个流结束但 `full_text` 为空时，记录有意义的占位内容。

**当前代码**: `chat_service.py:172-173`
```python
if not full_text.strip():
    full_text = "[模型未返回文本内容]"
```

**问题**: 这个兜底已存在，但仅在 `_collect_model_stream()` 返回后才执行。如果函数因异常退出，兜底不会执行。

**改进**: 在 `_stream_single_model()` 的 `except` 分支也添加兜底。

---

### 方案 6：卡片空白检测与自动修复（可选，前端）

**目标**: 流式结束后自动检测卡片是否为空，主动提示。

**设计**:
```javascript
// 在 stream_end 事件处理中
case 'done':
    flushTurnRender(requestId, model, round);
    const turn = getTurn(requestId, model, round);
    if (turn && !turn.raw.trim()) {
        // 自动设置空白提示
        turn.mdEl.innerHTML = '<div class="empty-response-hint">'
            + '<p>该轮模型未返回有效内容</p>'
            + '<p class="hint-sub">可能原因：内容策略限制、上下文超限、API 异常</p>'
            + '</div>';
    }
    break;
```

**涉及文件**: `websocket.js`, `styles.css`

---

### 方案 7：预览按钮的空内容保护（已部分实现）

**当前代码**: `nodes.js:386`
```javascript
if (!turn || !turn.raw) return;  // 空内容时按钮无反应
```

**改进**: 空内容时给出视觉反馈。
```javascript
if (!turn || !turn.raw) {
    flashButtonLabel(button, '无内容');
    return;
}
```

---

## 六、推荐实施优先级

| 优先级 | 方案 | 工作量 | 效果 |
|--------|------|--------|------|
| **P0** | 方案 1: 空白卡片占位状态 | 2h | 所有空白场景都有明确提示 |
| **P0** | 方案 3: 重放时显示错误信息 | 0.5h | 刷新后失败卡片不再空白 |
| **P1** | 方案 2: 超时保存部分内容 | 1h | 超时卡片保留已接收内容 |
| **P1** | 方案 5: 流式结束兜底检查 | 0.5h | 全链路空内容兜底 |
| **P1** | 方案 6: 自动空白检测 | 1h | 实时流式的空白自动标记 |
| **P2** | 方案 7: 预览按钮保护 | 0.5h | 交互细节完善 |
| **P3** | 方案 4: 断线恢复机制 | 3h+ | 根本解决重连丢数据 |

---

## 七、空白场景覆盖矩阵

实施全部 P0+P1 方案后的覆盖情况:

| 场景 | 方案 1 | 方案 2 | 方案 3 | 方案 5 | 方案 6 | 覆盖 |
|------|--------|--------|--------|--------|--------|------|
| API 返回空 | ✅ 占位 | - | - | ✅ 兜底 | ✅ 检测 | 三重 |
| 超时 70s | ✅ 占位 | ✅ 保存 | ✅ 重放 | - | ✅ 检测 | 四重 |
| 模型失败 | ✅ 占位 | - | ✅ 重放 | - | - | 双重 |
| 刷新后空内容 | ✅ 占位 | ✅ 数据 | ✅ 重放 | - | - | 三重 |
| 全部模型失败 | ✅ 占位 | - | ✅ 重放 | - | - | 双重 |
| 用户取消 | ✅ 占位 | - | - | - | - | 单重 |
| 网络断连 | ✅ 占位 | - | - | - | - | 单重 |
