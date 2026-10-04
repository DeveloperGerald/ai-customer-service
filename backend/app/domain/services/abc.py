from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.core.errors import ErrorCode


class BaseTicketService(ABC):
    """通用售后工单与状态查询服务（预留扩展点，MVP 不实现）。

    完整实现思路（T14 后分期任务池 E3）：

    1. 数据域：新增 `tickets` 表（tenant_id/ticket_id/order_id/user_id/
       type[exchange|repair|补发|general]/status/priority/assignee_staff_id/
       created_at/updated_at）+ `ticket_events` 审计。
    2. 业务动作：
       - `create_ticket(ctx, order_id, type, description)`：幂等创建，绑定订单归属校验
       - `list_my_tickets(ctx, status_filter)`：按消费者分页，强制 tenant+user 过滤
       - `get_ticket_detail(ctx, ticket_id)`：staff 跨用户看同租户；consumer 只看自己
       - `staff_append_note(ctx, ticket_id, note)` + `change_status(ctx, ticket_id, new_status)`
    3. Agent 工具：注册 `TicketQueryTool`、`TicketCreateTool`；创建前仍需 confirm 流程。
    4. 权限：admin/staff 可指派；consumer 不能改状态或改归属。
    5. 测试：跨租户查 ticket_id 必须不可见；staff 看他人同租户可访问；
       状态流转不允许跳步（如 open -> closed 必须经过处理中或 staff 权限）。
    """

    @abstractmethod
    async def create_ticket(
        self, ctx: Any, *, order_id: str, ticket_type: str, description: str
    ) -> Any:
        """创建售后工单。MVP 未实现，抛 BUSINESS_RULE 错误占位。

        Raises:
            BusinessRuleError: MVP 未启用工单模块。
        """
        raise NotImplementedError(ErrorCode.RESOURCE_NOT_FOUND)

    @abstractmethod
    async def get_ticket(self, ctx: Any, ticket_id: str) -> Any:
        """按 ID 查询工单。MVP 未实现。"""
        raise NotImplementedError(ErrorCode.RESOURCE_NOT_FOUND)


class BaseHumanAgentProtocol(ABC):
    """真人接管消息、结束与交还 Agent 协议（预留扩展点，MVP 不实现）。

    完整实现思路（任务池 E8）：

    1. 会话状态新增 `human_active`；真人进入时写 handoffs 表交接 + 状态锁。
    2. Websocket 通道（独立于 SSE）把 staff 消息入 messages 表；agent 不自动 run。
    3. staff 提交 `end_handoff(handoff_id, resolution_summary, hand_back=True/False)`；
       hand_back=True 时把会话状态切回 active，生成一轮 system 提示词供 Agent 续接上下文。
    4. 权限：仅 staff/admin 角色可接入；消息写入强制校验 staff.tenant_id == session.tenant_id。
    5. SSE 事件：新增 `human_message`、`human_joined`、`human_left`，前端区分样式。
    """

    @abstractmethod
    async def staff_join(self, ctx: Any, *, handoff_id: str) -> Any:
        """客服进入交接会话。MVP 未实现。"""
        raise NotImplementedError(ErrorCode.RESOURCE_NOT_FOUND)

    @abstractmethod
    async def staff_send(self, ctx: Any, *, handoff_id: str, message: str) -> Any:
        """客服发送消息给消费者。MVP 未实现。"""
        raise NotImplementedError(ErrorCode.RESOURCE_NOT_FOUND)


class BaseLongTermUserProfile(ABC):
    """长期用户偏好与摘要优化（预留扩展点，MVP 不实现）。

    完整实现思路（任务池 E6）：

    1. 表 `user_profiles`：tenant_id/user_id（复合主键）/preferences JSONB/
       summary_version/updated_at；版本号避免并发覆盖丢失。
    2. 摘要生成：会话 closed 后由异步 worker 调用 LLM，按长度阈值提取偏好；
       明确让模型输出 JSON schema 的偏好键列表，拒绝自由文本写库。
    3. 写入安全：摘要只写 `profile.preferences`，**不能**覆盖订单、地址等结构化事实；
       写入前用规则过滤如"用户不想被电话联系"等风险偏好，避免营销滥用。
    4. 访问边界：仅 Agent 生成节点读取；工具层、外部 API 不直接暴露读取接口。
    """

    @abstractmethod
    async def get_preferences(self, ctx: Any, *, tenant_id: str, user_id: str) -> Any:
        """读取长期用户偏好。MVP 返回空占位。"""
        raise NotImplementedError(ErrorCode.RESOURCE_NOT_FOUND)
