"""Dry-run and opt-in router profile synchronization."""

import json
import urllib.error
import urllib.request

from .config import load
from .policy import allowed, provider


def _entries(catalog, model_ids, tier):
    return [
        item
        for item in catalog
        if item.get("enabled", True)
        and item.get("modelId") in model_ids
        and allowed(
            provider(f"{item.get('platform')}/{item.get('modelId')}", catalog),
            tier,
            load("providers.yaml"),
        )
    ]


def diff(catalog, profiles):
    wanted = load("chains.yaml")
    existing = {item["name"]: item for item in profiles}
    changes = []
    for lane, model_ids in wanted.items():
        targets = [(lane, "public"), (lane + "-biz", "business")]
        if lane == "local":
            targets = [(lane, "private")]
        if lane == "local-small":
            targets = [(lane, "private")]
        for name, tier in targets:
            rows = _entries(catalog, model_ids, tier)
            rows.sort(
                key=lambda item: model_ids.index(item["modelId"])
            )  # chain order, not catalog
            if name == "local":
                rows = [
                    item
                    for item in rows
                    if provider(
                        f"{item.get('platform')}/{item.get('modelId')}", catalog
                    )
                    == "thor-local"
                ]
            if name == "local-small":
                rows = [item for item in rows if item.get("modelId") == "qwen3:8b"]
            ids = (
                ", ".join(f"{item['platform']}/{item['modelId']}" for item in rows)
                or "(empty)"
            )
            profile = existing.get(name)
            if not profile:
                changes.append(
                    {
                        "method": "POST",
                        "path": "/api/profiles",
                        "body": {"name": name, "empty": True},
                        "description": f"create {name} -> {ids}",
                    }
                )
                profile_id = f"<new:{name}>"
            else:
                profile_id = profile["id"]
            rows.sort(
                key=lambda item: model_ids.index(item["modelId"])
            )  # chain order, not catalog order
            body = [
                {"modelDbId": item["id"], "priority": i + 1, "enabled": True}
                for i, item in enumerate(rows)
            ]
            changes.append(
                {
                    "method": "PUT",
                    "path": f"/api/profiles/{profile_id}/reorder",
                    "body": body,
                    "description": f"profile {name} -> {ids}",
                }
            )
    for item in catalog:
        if item.get("platform") == "cloudflare" and item.get("enabled", True):
            changes.append(
                {
                    "method": "PATCH",
                    "path": f"/api/models/{item['id']}",
                    "body": {"enabled": False, "fallbackEnabled": False},
                    "description": f"disable cloudflare/{item['modelId']} (id {item['id']})",
                }
            )
    return changes


def apply(router, changes):
    results = []
    created = {}
    for change in changes:
        path = change["path"]
        if change["method"] == "POST" and path == "/api/profiles":
            name = change["body"]["name"]
            path = "/api/profiles"
        else:
            name = None
            for key, value in created.items():
                path = path.replace(f"<new:{key}>", str(value))
        request = urllib.request.Request(
            router.BASE + path,
            json.dumps(change["body"]).encode(),
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + router.admin_token(),
            },
            method=change["method"],
        )
        try:
            response = urllib.request.urlopen(request, timeout=20)
        except (
            urllib.error.HTTPError
        ) as error:  # stop with the router's reason; a re-run resumes idempotently
            detail = error.read().decode("utf-8", "replace")[:300]
            raise SystemExit(
                f"{change['method']} {path} failed: HTTP {error.code} {detail}"
            ) from None
        with response:
            results.append(response.status)
            if name:
                created[name] = json.loads(response.read()).get("id")
    return results
