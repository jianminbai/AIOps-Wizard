"""
bot.py — Feishu / DingTalk / WeCom Bot Integration

Supports:
  - Feishu (Lark) custom bot webhook
  - DingTalk custom bot webhook
  - WeCom (企业微信) bot webhook

Usage:
  POST /webhook/feishu  →  Feishu event → auto-analyze → reply
  POST /webhook/dingtalk → DingTalk event → auto-analyze → reply

Or manually send analysis to a chat:
  from bot import send_to_feishu, send_to_dingtalk
"""

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

import httpx

logger = logging.getLogger("aiops-bot")

# ── Configuration ─────────────────────────────────────────────────────

FEISHU_WEBHOOK_URL = os.environ.get("FEISHU_WEBHOOK_URL", "")
FEISHU_SECRET = os.environ.get("FEISHU_SECRET", "")  # Signing secret

DINGTALK_WEBHOOK_URL = os.environ.get("DINGTALK_WEBHOOK_URL", "")
DINGTALK_SECRET = os.environ.get("DINGTALK_SECRET", "")

WECOM_WEBHOOK_URL = os.environ.get("WECOM_WEBHOOK_URL", "")


# ── Feishu / Lark ─────────────────────────────────────────────────────

def _feishu_sign(timestamp: int) -> tuple[str, str]:
    """Generate Feishu signing signature."""
    secret = FEISHU_SECRET or os.environ.get("FEISHU_WEBHOOK_SECRET", "")
    if not secret:
        return "", ""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = hmac_code.hex()
    return sign, str(timestamp)


def send_to_feishu(
    title: str,
    content: str,
    webhook_url: str = "",
    msg_type: str = "interactive",
) -> bool:
    """Send a message to Feishu/Lark bot.

    Args:
        title: Message title
        content: Message body (supports markdown in interactive cards)
        webhook_url: Override default webhook URL
        msg_type: 'text' or 'interactive' (card)
    """
    url = webhook_url or FEISHU_WEBHOOK_URL
    if not url:
        logger.warning("Feishu webhook not configured; skipping")
        return False

    timestamp = int(time.time())
    sign, ts_str = _feishu_sign(timestamp)

    if msg_type == "interactive":
        payload = {
            "timestamp": ts_str,
            "sign": sign,
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": "red" if "P0" in title or "P1" in title else "blue",
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": content[:4000],  # Feishu limit
                    }
                ],
            },
        }
    else:
        payload = {
            "timestamp": ts_str,
            "sign": sign,
            "msg_type": "text",
            "content": {"text": f"{title}\n\n{content[:20000]}"},
        }

    try:
        resp = httpx.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") == 0:
            logger.info("Feishu message sent: title=%s", title)
            return True
        else:
            logger.error("Feishu send failed: %s", result)
            return False
    except Exception as e:
        logger.error("Feishu send error: %s", e)
        return False


# ── DingTalk ──────────────────────────────────────────────────────────

def _dingtalk_sign(timestamp: int) -> str:
    """Generate DingTalk signing signature."""
    secret = DINGTALK_SECRET or os.environ.get("DINGTALK_WEBHOOK_SECRET", "")
    if not secret:
        return ""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return hmac_code.hex()


def send_to_dingtalk(
    title: str,
    content: str,
    webhook_url: str = "",
) -> bool:
    """Send a markdown message to DingTalk bot."""
    url = webhook_url or DINGTALK_WEBHOOK_URL
    if not url:
        logger.warning("DingTalk webhook not configured; skipping")
        return False

    timestamp = int(time.time() * 1000)
    sign = _dingtalk_sign(timestamp)

    # Append signature to URL if set
    if sign:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}timestamp={timestamp}&sign={sign}"

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": f"## {title}\n\n{content[:20000]}",
        },
    }

    try:
        resp = httpx.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if result.get("errcode") == 0:
            logger.info("DingTalk message sent: title=%s", title)
            return True
        else:
            logger.error("DingTalk send failed: %s", result)
            return False
    except Exception as e:
        logger.error("DingTalk send error: %s", e)
        return False


# ── WeCom (企业微信) ──────────────────────────────────────────────────

def send_to_wecom(
    title: str,
    content: str,
    webhook_url: str = "",
) -> bool:
    """Send a markdown message to WeCom bot."""
    url = webhook_url or WECOM_WEBHOOK_URL
    if not url:
        logger.warning("WeCom webhook not configured; skipping")
        return False

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": f"## {title}\n{content[:4000]}",
        },
    }

    try:
        resp = httpx.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if result.get("errcode") == 0:
            logger.info("WeCom message sent: title=%s", title)
            return True
        else:
            logger.error("WeCom send failed: %s", result)
            return False
    except Exception as e:
        logger.error("WeCom send error: %s", e)
        return False


# ── Unified helper ────────────────────────────────────────────────────

def send_alert(
    title: str,
    content: str,
    channels: list[str] | None = None,
) -> dict[str, bool]:
    """Send alert to configured channels.

    Args:
        title: Alert title
        content: Alert body (markdown)
        channels: List of 'feishu', 'dingtalk', 'wecom'. Default: all configured.

    Returns:
        Dict of channel → success
    """
    if channels is None:
        channels = []
        if FEISHU_WEBHOOK_URL:
            channels.append("feishu")
        if DINGTALK_WEBHOOK_URL:
            channels.append("dingtalk")
        if WECOM_WEBHOOK_URL:
            channels.append("wecom")

    results = {}
    if "feishu" in channels:
        results["feishu"] = send_to_feishu(title, content)
    if "dingtalk" in channels:
        results["dingtalk"] = send_to_dingtalk(title, content)
    if "wecom" in channels:
        results["wecom"] = send_to_wecom(title, content)

    return results


# ── Format analysis for chat ──────────────────────────────────────────

def format_analysis_for_chat(analysis: dict, route: str, score: float, kb_id: str = "") -> str:
    """Format an analysis result as a readable chat message (markdown)."""
    lines = []

    classification = analysis.get("classification", "unknown")
    severity = analysis.get("severity_assessment", "unknown")
    emoji = "🔴" if severity == "true_fault" else "🟡" if severity == "needs_investigation" else "🟢"

    lines.append(f"**路由**: {route} (置信度: {score:.0%})")
    if kb_id:
        lines.append(f"**知识库匹配**: {kb_id}")
    lines.append(f"**分类**: {classification} | **严重度**: {severity} {emoji}")
    lines.append("")

    # Root cause hypotheses
    hypotheses = analysis.get("root_cause_hypotheses", [])
    if hypotheses:
        lines.append("### 根因假设")
        for h in hypotheses[:3]:
            prob = h.get("probability", "unknown")
            lines.append(f"- [{prob}] **{h.get('cause', '?')}**")
            evidence = h.get("evidence", "")
            if evidence:
                lines.append(f"  > {evidence}")

    # Commands
    commands = analysis.get("commands", {})
    if commands.get("check"):
        lines.append("\n### 排查命令")
        for cmd in commands["check"][:5]:
            lines.append(f"```bash\n{cmd}\n```")
    if commands.get("fix"):
        lines.append("\n### 修复命令")
        for cmd in commands["fix"][:3]:
            lines.append(f"```bash\n{cmd}\n```")

    # Immediate actions
    actions = analysis.get("immediate_actions", [])
    if actions:
        lines.append("\n### 建议操作")
        for a in actions[:3]:
            lines.append(f"- {a}")

    return "\n".join(lines)
