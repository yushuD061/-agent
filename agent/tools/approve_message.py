"""
审批外发工具

所有对外发送的内容必须经过审批。
未审批外发阻断率要求 100%。

三步流程：request(提交审批) → approve(通过) / reject(驳回)
"""

import json

from agent.business.database import create_approval, approve, get_quote, update_quote_status


def approve_outbound_message_impl(
    quote_id: int,
    action: str = "request",
    reviewer: str = "业务员",
    comment: str = "",
    version: int = 1,
) -> str:
    """审批外发内容

    Args:
        quote_id: 报价单 ID
        action: 动作 (request=申请审批 / approve=通过 / reject=驳回)
        reviewer: 审批人姓名
        comment: 审批意见
        version: 报价版本号

    Returns:
        审批结果
    """
    quote = get_quote(quote_id)
    if not quote:
        return json.dumps({"error": f"报价单 #{quote_id} 不存在"}, ensure_ascii=False, indent=2)

    if action == "request":
        approval_id = create_approval(quote_id, version)
        update_quote_status(quote_id, "pending_approval")
        result = {
            "approval_id": approval_id,
            "quote_id": quote_id,
            "version": version,
            "status": "pending_approval",
            "message": f"报价单 #{quote_id} (v{version}) 已提交审批，等待审核",
        }

    elif action in ("approve", "reject"):
        # 查找最近的待审批记录
        from agent.business.database import tx
        with tx() as cur:
            cur.execute(
                "SELECT id FROM approval_records WHERE quote_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
                (quote_id,),
            )
            row = cur.fetchone()
        if not row:
            return json.dumps({"error": "未找到待审批记录，请先提交审批申请"}, ensure_ascii=False, indent=2)

        approval_id = row["id"]
        is_approved = action == "approve"
        approve(approval_id, reviewer, comment, approved=is_approved)
        status_text = "已审批通过" if is_approved else "已驳回"

        result = {
            "approval_id": approval_id,
            "quote_id": quote_id,
            "version": version,
            "status": "approved" if is_approved else "rejected",
            "message": f"报价单 #{quote_id} (v{version}) {status_text}",
            "reviewer": reviewer,
            "comment": comment,
        }
    else:
        return json.dumps({"error": f"无效动作 '{action}'，应为 request/approve/reject"}, ensure_ascii=False, indent=2)

    return json.dumps(result, ensure_ascii=False, indent=2)
