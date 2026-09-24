#!/usr/bin/env python3
"""Aster-only Claude Read hook. No context-window or cache manipulation."""
import json
import os
import sys

HINT = "除非已明确目标行，后续查看文件建议一次读取 300～500 行，以保持上下文完整并减少交互轮次。"


def active_model(data):
    # An inherited default subagent env var does not identify the current agent.
    model = data.get("model") or data.get("agent_model")
    if model:
        return str(model).lower()
    transcript = data.get("transcript_path")
    if transcript:
        try:
            with open(transcript, "rb") as stream:
                stream.seek(max(0, os.fstat(stream.fileno()).st_size - 65536))
                for line in reversed(stream.read().splitlines()):
                    try:
                        item = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if item.get("type") == "assistant":
                        return str(item.get("message", {}).get("model", "")).lower()
        except OSError:
            pass
    return ""


def handle(data):
    if os.environ.get("AI_SWITCH_MODE") not in (None, "aster"):
        return None
    if "gemini" not in active_model(data) or data.get("tool_name") not in ("Read", "View"):
        return None
    inp = data.get("tool_input") or {}
    event = data.get("hook_event_name")
    limit, offset = inp.get("limit"), inp.get("offset")
    if event == "PreToolUse" and type(offset) is int and offset > 0:
        path = inp.get("file_path")
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, "rb") as stream:
                lines = sum(1 for _ in stream)
        except OSError:
            return None
        if lines and offset > lines:
            size = limit if type(limit) is int and limit > 0 else 100
            start = max(1, lines - size + 1)
            return {"hookSpecificOutput": {"hookEventName": event,
                    "updatedInput": dict(inp, offset=start, limit=lines-start+1)}}
    if event == "PostToolUse" and type(limit) is int and 0 < limit < 200:
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": HINT}}
    return None


if __name__ == "__main__":
    try:
        result = handle(json.load(sys.stdin))
        if result:
            print(json.dumps(result, ensure_ascii=False))
    except (ValueError, TypeError, OSError):
        pass
