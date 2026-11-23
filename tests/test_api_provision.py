from __future__ import annotations

from core.api_provision import APIProvisioner, APIKeys, Provider, ProviderHealth


def _health(provider: Provider, *, reachable: bool, api_ready: bool) -> ProviderHealth:
    return ProviderHealth(
        provider=provider,
        reachable=reachable,
        api_ready=api_ready,
    )


def test_local_falls_back_to_nexos_when_local_lanes_are_down() -> None:
    provisioner = APIProvisioner(APIKeys(nexos="configured"))
    provisioner._health = {
        Provider.LOCAL: _health(Provider.LOCAL, reachable=False, api_ready=False),
        Provider.LOCAL_INTEL: _health(
            Provider.LOCAL_INTEL, reachable=False, api_ready=False
        ),
        Provider.NEXOS: _health(Provider.NEXOS, reachable=True, api_ready=True),
    }

    assert provisioner._pick_provider(Provider.LOCAL) is Provider.NEXOS


def test_nexos_is_not_selected_without_valid_credentials() -> None:
    provisioner = APIProvisioner(APIKeys())
    provisioner._health = {
        Provider.LOCAL: _health(Provider.LOCAL, reachable=False, api_ready=False),
        Provider.LOCAL_INTEL: _health(
            Provider.LOCAL_INTEL, reachable=False, api_ready=False
        ),
        Provider.NEXOS: _health(Provider.NEXOS, reachable=True, api_ready=False),
        Provider.XAI: _health(Provider.XAI, reachable=False, api_ready=False),
    }

    assert provisioner._pick_provider(Provider.LOCAL) is Provider.LOCAL
