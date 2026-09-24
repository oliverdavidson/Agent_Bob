"""Claude on Microsoft Foundry."""

from anthropic import AnthropicFoundry

from bob.config import Settings

FOUNDRY_SCOPE = "https://ai.azure.com/.default"


def make_client(settings: Settings) -> AnthropicFoundry:
    if not settings.foundry_resource:
        raise RuntimeError("BOB_FOUNDRY_RESOURCE is not set")
    if settings.foundry_api_key:
        return AnthropicFoundry(
            api_key=settings.foundry_api_key, resource=settings.foundry_resource
        )

    from azure.identity import DefaultAzureCredential, get_bearer_token_provider

    token_provider = get_bearer_token_provider(DefaultAzureCredential(), FOUNDRY_SCOPE)
    return AnthropicFoundry(
        azure_ad_token_provider=token_provider, resource=settings.foundry_resource
    )
