from main.services.stateful.bank_data.providers.base import BankingProvider
from main.services.stateful.bank_data.providers.enable_banking.provider import EnableBankingProvider
from main.services.stateful.bank_data.providers.gocardless.provider import GoCardlessProvider

_PROVIDER_META: dict[str, dict] = {
    "gocardless": {
        "label": "GoCardless Bank Account Data API",
        "description": "Connect via the GoCardless Open Banking network. Available across the UK and Europe.",
        "badge_classes": "bg-(--first-accent-color) text-white",
        "setup_notice": None,
    },
    "enable_banking": {
        "label": "Enable Banking API",
        "description": "Connect via Enable Banking. Supports a wide range of European banks.",
        "badge_classes": "bg-cyan-600 text-white",
        "setup_notice": (
            "Notice: Enable Finance requires you to link bank accounts in the Enable Banking developer portal first: "
            "add a new Production application and link every account you want to connect to Visualfolio. "
            "Note: 'general availability' / 'unrestriction' is not required."
        ),
    },
}

_ALL_PROVIDERS: list[BankingProvider] = [GoCardlessProvider(), EnableBankingProvider()]


def get_provider(provider_type: str) -> BankingProvider:
    for p in _ALL_PROVIDERS:
        if p.provider_type == provider_type:
            return p
    raise ValueError(f"Unknown provider type: {provider_type}")


def get_all_providers() -> list[dict]:
    """Return all providers with their enabled status and metadata."""
    result = []
    for p in _ALL_PROVIDERS:
        meta = _PROVIDER_META.get(p.provider_type, {})
        result.append({
            "type": p.provider_type,
            "label": meta.get("label", p.provider_type),
            "description": meta.get("description", ""),
            "badge_classes": meta.get("badge_classes", "bg-(--third-bg-color) text-(--third-text-color)"),
            "setup_notice": meta.get("setup_notice"),
            "enabled": p.is_enabled,
            "disabled_reason": p.disabled_reason,
        })
    return result


def get_enabled_providers() -> list[dict]:
    return [p for p in get_all_providers() if p["enabled"]]
