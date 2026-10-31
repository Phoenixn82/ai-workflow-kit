"""Tier and provider policy."""

TIERS = ("public", "business", "business-pii", "private")


def provider(route, catalog=()):
    platform, _, model = (route or "//").partition("/")
    if platform != "custom":
        return platform
    matches = [
        x
        for x in catalog
        if x.get("platform") == "custom" and x.get("modelId") == model
    ]
    # Key labels as set on the router's Keys page: name these two keys to match.
    labels = {str(x.get("keyLabel", "")).lower() for x in matches}
    if "thor local ollama" in labels:
        return "thor-local"
    if "openrouter extra free models" in labels:
        # That key also lists paid OpenRouter ids; only ":free" ids cost $0.
        return "openrouter" if model.endswith(":free") else "openrouter-paid"
    return "unknown"


def allowed(name, tier, providers):
    if name in {"cloudflare", "aihorde", "cohere", "openrouter-paid"}:
        return False
    if name == "unknown":
        return tier == "public"
    data = providers.get(name, {})
    if tier == "private":
        return name == "thor-local"
    if tier == "business-pii":
        return name == "thor-local" or bool(
            data.get("pii_ok") and data.get("pii_ok_approved")
        )
    if tier == "business":
        return (
            not data.get("relay", False)
            and data.get("audit", {}).get("status") == "acceptable"
        )
    return True


def presend(models, tier, providers, catalog=()):
    return all(
        allowed(
            provider(
                f"{item.get('platform', '')}/{item.get('modelId', item.get('model_id', ''))}",
                catalog,
            ),
            tier,
            providers,
        )
        for item in models
        if item.get("enabled", True)
    )


def postroute(route, tier, providers, catalog=()):
    return allowed(provider(route, catalog), tier, providers)
