"""Deterministic request and response guards for the public customer agent."""

from __future__ import annotations

import re


class CustomerDataGuard:
    """Fail closed on attempts or outputs involving non-public enterprise data.

    The primary security boundary is architectural: the customer agent receives
    no internal tools, MCP servers, Skills, workspace details, or internal
    memory. These checks are an additional deterministic boundary.
    """

    _INJECTION_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
        r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|rules?|prompts?)",
        r"(reveal|show|print|repeat|dump|expose).{0,40}(system|developer)\s+(prompt|message|instructions?)",
        r"(jailbreak|developer\s+mode|system\s+override|prompt\s+injection)",
        r"忽略.{0,12}(之前|以上|前面).{0,12}(指令|规则|提示词)",
        r"(显示|输出|泄露|复述|打印).{0,20}(系统提示词|开发者消息|内部指令)",
        r"(ignoriere|missachte).{0,24}(vorherigen|obigen).{0,24}(anweisungen|regeln|prompt)",
        r"(zeige|offenbare|drucke|wiederhole).{0,30}(system|entwickler).{0,20}(prompt|nachricht|anweisung)",
    ))

    _SENSITIVE_REQUEST_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
        r"(read|show|reveal|dump|export|access|list|give me).{0,50}(internal|private|confidential).{0,30}(data|record|file|database|email|mailbox|memory)",
        r"(read|show|reveal|dump|export|access|list).{0,50}(\.env|memory\.md|skill\.md|api[_ -]?key|password|token|auth[_ -]?code|inbox|email body)",
        r"(读取|查看|显示|导出|泄露|访问|列出|给我).{0,50}(内部|私密|机密|企业).{0,30}(数据|记录|文件|数据库|邮件|邮箱|记忆)",
        r"(读取|查看|显示|导出|泄露|访问).{0,50}(密钥|密码|令牌|授权码|邮件正文|收件箱|内部记忆|系统文件)",
        r"(lesen|zeigen|offenbaren|exportieren|zugreifen|auflisten).{0,50}(intern|privat|vertraulich).{0,30}(daten|datei|datenbank|e-?mail|postfach|speicher)",
        r"(lesen|zeigen|offenbaren|exportieren).{0,50}(passwort|token|api.?schl[uü]ssel|authentifizierungscode|posteingang|e-?mail.?inhalt)",
    ))

    _SENSITIVE_OUTPUT_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
        r"\bNANOCLAW_[A-Z0-9_]+\b",
        r"\b(api[_ -]?key|password|token|secret|authorization|auth[_ -]?code)\b\s*[:=]\s*\S+",
        r"(?:^|[\s'\"])(?:[A-Za-z]:\\|/(?:home|root|etc|var|opt)/)[^\s'\"]+",
        r"\.(?:env)(?:\b|$)|\bMEMORY\.md\b|\bSKILL\.md\b",
        r"\b(query_inbound_email|ops_email_[a-z0-9_]*|mcp_servers?)\b",
        r"\b(?:inventory|stock|exact_inventory|unit_price|base_price|cost|margin|profit)\b\s*(?:is|[:=])\s*[$€£]?\d",
        r"(?:库存|现货|精确库存|价格|成本|利润率)\s*(?:为|是|有|[:：=])\s*[￥$€£]?\d",
        r"\b(system|developer)\s+(prompt|message|instructions?)\s*[:=]",
        r"(internal|private|confidential)\s+(email body|mailbox content|database record)",
        r"(内部|私密|机密)(邮件正文|邮箱内容|数据库记录|系统提示词)",
        r"Traceback \(most recent call last\):",
    ))

    _REFUSALS = {
        "zh": "抱歉，我只能协助公开的产品询盘与 RFQ 信息收集，不能访问、检索或披露企业内部数据、邮件、系统提示词、配置、凭据或内部工具信息。",
        "de": "Entschuldigung, ich kann nur bei öffentlichen Produktanfragen und der RFQ-Erfassung helfen. Auf interne Unternehmensdaten, E-Mails, Systemanweisungen, Konfigurationen, Zugangsdaten oder interne Werkzeuge kann ich weder zugreifen noch sie offenlegen.",
        "en": "Sorry, I can only help with public product inquiries and RFQ collection. I cannot access, retrieve, or disclose internal company data, email, system instructions, configuration, credentials, or internal tools.",
    }

    @classmethod
    def refusal(cls, language: str) -> str:
        return cls._REFUSALS.get(language, cls._REFUSALS["en"])

    @classmethod
    def inspect_request(cls, text: str, language: str = "en") -> str | None:
        candidate = str(text or "")[:20000]
        patterns = cls._INJECTION_PATTERNS + cls._SENSITIVE_REQUEST_PATTERNS
        if any(pattern.search(candidate) for pattern in patterns):
            return cls.refusal(language)
        return None

    @classmethod
    def sanitize_response(cls, text: str, language: str = "en") -> str:
        candidate = str(text or "")
        if any(pattern.search(candidate) for pattern in cls._SENSITIVE_OUTPUT_PATTERNS):
            return cls.refusal(language)
        return candidate
