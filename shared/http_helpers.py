"""Lightweight HTTP helpers using stdlib urllib."""

import json
import urllib.request
import urllib.error


def urllib_post(url: str, data: dict, headers: dict, timeout: int = 30):
    """POST a JSON body and return (status_code, parsed_json_or_dict)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"error": body}


def urllib_get_binary(url: str, headers: dict, timeout: int = 30):
    """GET and return (status_code, content_type, raw_bytes)."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, "", body
